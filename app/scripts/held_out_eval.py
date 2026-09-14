import asyncio
import structlog
from datetime import datetime
from sqlalchemy import text
from app.core.database import AsyncSessionLocal
from app.services.retrieval import find_similar_comments_for_eval
from app.services.synthesizer import synthesize_feedback
from app.services.evaluation import embed_with_retry

logger = structlog.get_logger()

SPLIT_DATE = datetime(2026, 7, 10, 23, 53, 59)
REPO_POOL = [("psf", "requests"), ("tiangolo", "fastapi")]
PILOT_SIZE = 15
THRESHOLD_CANDIDATES = [0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]


async def fetch_held_out_samples(limit: int) -> list:
    """Fetch held-out comments, spread evenly across the held-out time range."""
    repo_conditions = " OR ".join(
        [f"(repo_owner = :owner_{i} AND repo_name = :name_{i})" for i in range(len(REPO_POOL))]
    )
    repo_params = {}
    for i, (owner, name) in enumerate(REPO_POOL):
        repo_params[f"owner_{i}"] = owner
        repo_params[f"name_{i}"] = name

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(f"""
                SELECT id, repo_owner, repo_name, path, diff_hunk, body, comment_created_at
                FROM review_comments
                WHERE ({repo_conditions})
                AND comment_created_at >= :split_date
                AND author NOT LIKE '%[bot]'
                ORDER BY comment_created_at ASC
            """),
            {"split_date": SPLIT_DATE, **repo_params}
        )
        rows = result.fetchall()

    if len(rows) <= limit:
        return rows

    step = len(rows) / limit
    sampled = [rows[int(i * step)] for i in range(limit)]
    return sampled


async def evaluate_sample(sample) -> dict:
    """Run one held-out sample through retrieval + synthesis, and return the
    raw best-match similarity plus how many concerns were generated —
    NOT a pre-committed TP/FP/FN verdict, so we can calibrate the threshold
    against this same data afterward without re-running the expensive
    retrieval/synthesis steps multiple times."""

    hunk = sample.diff_hunk
    real_comment_body = sample.body

    similar = await find_similar_comments_for_eval(
        hunk=hunk,
        before_date=SPLIT_DATE,
        repo_owners_and_names=REPO_POOL
    )

    if not similar:
        logger.info("eval_sample_no_retrieval_matches", sample_id=sample.id)
        return {"sample_id": sample.id, "had_retrieval": False, "num_concerns": 0, "best_similarity": None}

    for s in similar:
        s["triggered_by_hunk"] = hunk

    feedback = await synthesize_feedback(similar)

    if not feedback:
        logger.info("eval_sample_no_synthesis_output", sample_id=sample.id)
        return {"sample_id": sample.id, "had_retrieval": True, "num_concerns": 0, "best_similarity": None}

    real_vector = await embed_with_retry(real_comment_body)
    real_vector_str = "[" + ",".join(str(x) for x in real_vector) + "]"

    best_similarity = 0.0

    async with AsyncSessionLocal() as session:
        for item in feedback:
            concern_vector = await embed_with_retry(item.get("concern", ""))
            concern_vector_str = "[" + ",".join(str(x) for x in concern_vector) + "]"

            result = await session.execute(
                text("""
                    SELECT 1 - (CAST(:v1 AS vector) <=> CAST(:v2 AS vector)) AS similarity
                """),
                {"v1": real_vector_str, "v2": concern_vector_str}
            )
            row = result.fetchone()
            similarity = row.similarity if row else 0.0

            if similarity > best_similarity:
                best_similarity = similarity

    logger.info(
        "eval_sample_scored",
        sample_id=sample.id,
        concerns_generated=len(feedback),
        best_similarity=round(best_similarity, 3)
    )

    return {
        "sample_id": sample.id,
        "had_retrieval": True,
        "num_concerns": len(feedback),
        "best_similarity": round(best_similarity, 3)
    }


def score_at_threshold(sample_results: list, threshold: float) -> dict:
    """Given raw per-sample results, compute TP/FP/FN at a specific threshold."""
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

    precision = round(tp / (tp + fp), 3) if (tp + fp) > 0 else None
    recall = round(tp / (tp + fn), 3) if (tp + fn) > 0 else None
    f1 = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = round(2 * precision * recall / (precision + recall), 3)

    return {
        "threshold": threshold,
        "tp": tp, "fp": fp, "fn": fn,
        "precision": precision, "recall": recall, "f1": f1
    }


async def main():
    samples = await fetch_held_out_samples(PILOT_SIZE)
    logger.info("held_out_eval_started", sample_count=len(samples))

    sample_results = []

    for i, sample in enumerate(samples):
        if i > 0:
            await asyncio.sleep(30)
        result = await evaluate_sample(sample)
        sample_results.append(result)

    no_retrieval_count = sum(1 for r in sample_results if not r["had_retrieval"])
    scored_similarities = [r["best_similarity"] for r in sample_results if r["best_similarity"] is not None]

    logger.info(
        "held_out_raw_data_summary",
        total_samples=len(sample_results),
        no_retrieval_match_count=no_retrieval_count,
        no_retrieval_match_pct=round(no_retrieval_count / len(sample_results) * 100, 1),
        scored_samples=len(scored_similarities),
        similarity_min=min(scored_similarities) if scored_similarities else None,
        similarity_max=max(scored_similarities) if scored_similarities else None
    )

    for threshold in THRESHOLD_CANDIDATES:
        metrics = score_at_threshold(sample_results, threshold)
        logger.info("threshold_calibration", **metrics)


if __name__ == "__main__":
    asyncio.run(main())