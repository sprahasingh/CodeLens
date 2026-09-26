import asyncio
import json
import structlog
from datetime import datetime
from pathlib import Path
from sqlalchemy import text
from app.core.database import AsyncSessionLocal
from app.services.retrieval import find_similar_comments_for_eval
from app.services.synthesizer import synthesize_feedback
from app.services.evaluation import embed_with_retry, llm_judge_match, cosine_similarity
from app.services.embedder import TEXT_EMBEDDING_MODEL

logger = structlog.get_logger()

SPLIT_DATE = datetime(2026, 7, 10, 23, 53, 59)
REPO_POOL = [
    ("psf", "requests"),
    ("tiangolo", "fastapi"),
    ("pallets", "flask"),
    ("encode", "httpx"),
    ("pydantic", "pydantic"),
]
# sprahasingh/CodeLens is deliberately excluded from the eval pool — its
# reviewers know about CodeLens, so including it would undermine the
# "tested against independent maintainers" claim the eval is built on.

# Methodology: the held-out pool is split into a TUNE set (used only to pick
# a similarity floor via threshold sweep) and a separate, non-overlapping
# TEST set (used only to report the final metric, with the LLM judge as the
# actual match decision). No sample is ever in both sets, so the reported
# number is never computed on data that influenced the floor choice.
#
# TEST_SIZE_MAX is a target, not a requirement: the actual test set uses
# whatever's left in the pool after tuning, up to this cap, so the script
# degrades gracefully on a small corpus and automatically uses more samples
# (a tighter confidence interval) as the corpus grows.
TUNE_SIZE = 15
TEST_SIZE_MAX = 35
MIN_TEST_SIZE = 10
THRESHOLD_CANDIDATES = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]

EVAL_RUNS_DIR = Path(__file__).parent / "eval_runs"


async def fetch_held_out_pool_by_repo() -> dict:
    """Fetch every eligible held-out comment (after SPLIT_DATE) for each repo
    in REPO_POOL separately, ordered chronologically ascending within each
    repo. Kept per-repo (not pooled together) so the tune/test split can be
    stratified — see split_tune_test for why."""
    pools = {}
    for owner, name in REPO_POOL:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT id, repo_owner, repo_name, path, diff_hunk, body, comment_created_at
                    FROM review_comments
                    WHERE repo_owner = :owner AND repo_name = :name
                    AND comment_created_at >= :split_date
                    AND author NOT LIKE '%[bot]'
                    ORDER BY comment_created_at ASC
                """),
                {"owner": owner, "name": name, "split_date": SPLIT_DATE}
            )
            pools[(owner, name)] = result.fetchall()
    return pools


def _even_subsample(rows: list, k: int) -> list:
    if len(rows) <= k:
        return rows
    step = len(rows) / k
    return [rows[int(i * step)] for i in range(k)]


def split_tune_test(pools_by_repo: dict, tune_size: int, test_size_max: int, min_test_size: int) -> tuple:
    """Stratified, non-overlapping split: within each repo's own timeline,
    the earlier half is eligible for tuning and the later half for the final
    test report — then an equal quota is drawn from each repo independently,
    instead of chronologically across all repos pooled together.

    This matters because pooling chronologically before splitting lets one
    very-active repo dominate: a 2026-09-26 run found pydantic/pydantic (whose
    ingested comments skew very recent) ate 29 of 35 test samples on its own,
    against a thin pre-cutoff retrieval corpus for that repo, driving cold
    start to 91%. Stratifying by repo prevents any single repo's activity
    level or ingestion recency from skewing which code the eval actually
    tests against."""
    num_repos = len(pools_by_repo)
    per_repo_tune = max(1, tune_size // num_repos)
    per_repo_test = max(1, test_size_max // num_repos)

    tune_rows = []
    test_rows = []
    for rows in pools_by_repo.values():
        midpoint = len(rows) // 2
        tune_half = rows[:midpoint]
        test_half = rows[midpoint:]
        tune_rows.extend(_even_subsample(tune_half, min(per_repo_tune, len(tune_half))))
        test_rows.extend(_even_subsample(test_half, min(per_repo_test, len(test_half))))

    if len(tune_rows) + len(test_rows) < tune_size + min_test_size:
        raise ValueError(
            f"Not enough held-out samples across repos: got {len(tune_rows)} tune-eligible "
            f"and {len(test_rows)} test-eligible after stratifying by repo, need at least "
            f"{tune_size} + {min_test_size}"
        )

    return tune_rows, test_rows


async def evaluate_sample(sample, floor: float = None, use_judge: bool = False) -> dict:
    """Run one held-out sample through retrieval + synthesis, and return the
    raw best-match similarity plus how many concerns were generated.

    When use_judge is True and the best-similarity concern clears `floor`,
    the LLM judge is asked whether it actually raises the same concern as
    the real reviewer comment — this is the final match decision, not the
    raw similarity. The judge call happens after retrieval/synthesis, same
    as the embedding calls elsewhere: no locks or retries are held open
    across it."""

    hunk = sample.diff_hunk
    real_comment_body = sample.body

    similar = await find_similar_comments_for_eval(
        hunk=hunk,
        before_date=SPLIT_DATE,
        repo_owners_and_names=REPO_POOL
    )

    if not similar:
        logger.info("eval_sample_no_retrieval_matches", sample_id=sample.id)
        return {
            "sample_id": sample.id, "had_retrieval": False, "num_concerns": 0,
            "best_similarity": None, "best_concern": None, "real_comment": real_comment_body,
            "judge_confirmed": None, "judge_reason": None
        }

    for s in similar:
        s["triggered_by_hunk"] = hunk

    feedback = await synthesize_feedback(hunk, similar)

    if not feedback:
        logger.info("eval_sample_no_synthesis_output", sample_id=sample.id)
        return {
            "sample_id": sample.id, "had_retrieval": True, "num_concerns": 0,
            "best_similarity": None, "best_concern": None, "real_comment": real_comment_body,
            "judge_confirmed": None, "judge_reason": None
        }

    # Natural-language matching (concern sentence vs. reviewer comment) uses
    # the general-purpose text model, not the code model used for retrieval.
    real_vector = await embed_with_retry(real_comment_body, model=TEXT_EMBEDDING_MODEL)

    best_similarity = 0.0
    best_concern = None

    for item in feedback:
        concern_vector = await embed_with_retry(item.get("concern", ""), model=TEXT_EMBEDDING_MODEL)
        similarity = cosine_similarity(real_vector, concern_vector)

        if similarity > best_similarity:
            best_similarity = similarity
            best_concern = item.get("concern", "")

    judge_confirmed = None
    judge_reason = None
    if use_judge and best_concern is not None and floor is not None and best_similarity >= floor:
        judge = await llm_judge_match(best_concern, real_comment_body)
        judge_confirmed = judge["same_concern"]
        judge_reason = judge["reason"]

    logger.info(
        "eval_sample_scored",
        sample_id=sample.id,
        concerns_generated=len(feedback),
        best_similarity=round(best_similarity, 3),
        judge_confirmed=judge_confirmed
    )

    return {
        "sample_id": sample.id,
        "had_retrieval": True,
        "num_concerns": len(feedback),
        "best_similarity": round(best_similarity, 3),
        "best_concern": best_concern,
        "real_comment": real_comment_body,
        "judge_confirmed": judge_confirmed,
        "judge_reason": judge_reason
    }


def score_at_threshold(sample_results: list, threshold: float) -> dict:
    """Tuning-pass scorer: pure similarity threshold, no judge. Used only to
    pick a similarity floor on the TUNE set — never used to compute the
    final reported metric."""
    tp = fp = fn = 0

    for r in sample_results:
        if not r["had_retrieval"] or r["num_concerns"] == 0:
            fn += 1
            continue

        if r["best_similarity"] is not None and r["best_similarity"] >= threshold:
            tp += 1
            fp += r["num_concerns"] - 1
        else:
            fn += 1
            fp += r["num_concerns"]

    return _precision_recall_f1(tp, fp, fn, threshold=threshold)


def score_final(sample_results: list, floor: float) -> dict:
    """Test-pass scorer: a sample only counts as a true positive if its
    best-similarity concern cleared the floor AND the LLM judge confirmed it
    raises the same concern as the real reviewer comment."""
    tp = fp = fn = 0

    for r in sample_results:
        if not r["had_retrieval"] or r["num_concerns"] == 0:
            fn += 1
            continue

        matched = (
            r["best_similarity"] is not None
            and r["best_similarity"] >= floor
            and r.get("judge_confirmed") is True
        )

        if matched:
            tp += 1
            fp += r["num_concerns"] - 1
        else:
            fn += 1
            fp += r["num_concerns"]

    return _precision_recall_f1(tp, fp, fn, floor=floor)


def _precision_recall_f1(tp: int, fp: int, fn: int, **extra) -> dict:
    precision = round(tp / (tp + fp), 3) if (tp + fp) > 0 else None
    recall = round(tp / (tp + fn), 3) if (tp + fn) > 0 else None
    f1 = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = round(2 * precision * recall / (precision + recall), 3)

    return {
        **extra,
        "tp": tp, "fp": fp, "fn": fn,
        "precision": precision, "recall": recall, "f1": f1
    }


async def main():
    pools_by_repo = await fetch_held_out_pool_by_repo()
    pool_sizes = {f"{owner}/{name}": len(rows) for (owner, name), rows in pools_by_repo.items()}
    logger.info("held_out_pool_fetched_by_repo", pool_sizes=pool_sizes, total=sum(pool_sizes.values()))

    tune_rows, test_rows = split_tune_test(pools_by_repo, TUNE_SIZE, TEST_SIZE_MAX, MIN_TEST_SIZE)
    logger.info("tune_test_split", tune_size=len(tune_rows), test_size=len(test_rows))

    # --- Tuning pass: pick a similarity floor. Cheap, no judge calls. ---
    tune_results = []
    for i, sample in enumerate(tune_rows):
        if i > 0:
            await asyncio.sleep(30)
        result = await evaluate_sample(sample)
        tune_results.append(result)

    tuning_sweep = []
    best_floor = THRESHOLD_CANDIDATES[0]
    best_tune_f1 = -1
    for threshold in THRESHOLD_CANDIDATES:
        metrics = score_at_threshold(tune_results, threshold)
        tuning_sweep.append(metrics)
        logger.info("tuning_threshold_sweep", **metrics)
        if metrics["f1"] is not None and metrics["f1"] > best_tune_f1:
            best_tune_f1 = metrics["f1"]
            best_floor = threshold

    logger.info("floor_selected_from_tuning_set", floor=best_floor, tuning_f1=best_tune_f1)

    # --- Test pass: completely separate samples, judge is the final decision. ---
    test_results = []
    for i, sample in enumerate(test_rows):
        if i > 0:
            await asyncio.sleep(30)
        result = await evaluate_sample(sample, floor=best_floor, use_judge=True)
        test_results.append(result)

    no_retrieval_count = sum(1 for r in test_results if not r["had_retrieval"])
    final_metrics = score_final(test_results, best_floor)

    logger.info(
        "final_test_summary",
        test_size=len(test_results),
        no_retrieval_match_count=no_retrieval_count,
        no_retrieval_match_pct=round(no_retrieval_count / len(test_results) * 100, 1),
        **final_metrics
    )

    run_record = {
        "run_at": datetime.utcnow().isoformat(),
        "split_date": SPLIT_DATE.isoformat(),
        "repo_pool": REPO_POOL,
        "pool_sizes_by_repo": pool_sizes,
        "tune_size": len(tune_rows),
        "test_size": len(test_results),
        "tuning_sweep": tuning_sweep,
        "floor_selected": best_floor,
        "final_metrics": final_metrics,
        "no_retrieval_match_count": no_retrieval_count,
        "no_retrieval_match_pct": round(no_retrieval_count / len(test_results) * 100, 1),
        "test_sample_results": test_results,
    }

    EVAL_RUNS_DIR.mkdir(exist_ok=True)
    out_path = EVAL_RUNS_DIR / f"held_out_eval_{datetime.utcnow():%Y%m%d_%H%M%S}.json"
    out_path.write_text(json.dumps(run_record, indent=2, default=str))
    logger.info("eval_run_persisted", path=str(out_path))


if __name__ == "__main__":
    asyncio.run(main())
