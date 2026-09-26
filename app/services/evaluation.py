import structlog
import math
from typing import Dict, Any
from sqlalchemy import text
from app.core.database import AsyncSessionLocal
from app.services.embedder import embed_single, EMBEDDING_MODEL, TEXT_EMBEDDING_MODEL
from app.services.llm_client import call_groq_json
from voyageai.error import RateLimitError
import asyncio
from app.models.false_negative import FalseNegative

logger = structlog.get_logger()

POSITIONAL_LINE_WINDOW = 10

# Minimum embedding similarity worth sending to the LLM judge at all — a
# candidate referral floor, not a match decision. Calibrated via held-out
# evaluation against psf/requests and tiangolo/fastapi (Sep 2026): 0.75
# (inherited from code-to-code retrieval) yielded 0/7 candidates surviving
# at all against real independent reviewer comments; 0.45 was the best
# raw-similarity cutoff in an empirical sweep before the judge stage existed.
# The LLM judge (see llm_judge_match) now makes the actual same-concern
# decision on candidates that clear this floor. See app/scripts/held_out_eval.py.
SIMILARITY_FLOOR = 0.45

JUDGE_PROMPT = """You are checking whether a predicted code review concern and a real reviewer's comment are about the same underlying issue in the code.

Predicted concern: "{predicted}"
Real reviewer comment: "{actual}"

Do these two raise the same underlying concern, even if worded very differently? Respond with ONLY a valid JSON object, no markdown, no preamble:
{{"same_concern": true or false, "reason": "one sentence explaining why"}}"""


async def embed_with_retry(text_to_embed: str, model: str = EMBEDDING_MODEL, max_retries: int = 3) -> list:
    retry_count = 0
    while retry_count < max_retries:
        try:
            return embed_single(text_to_embed, model=model)
        except RateLimitError:
            retry_count += 1
            logger.warning(
                "voyage_rate_limited_retrying_eval",
                retry_count=retry_count,
                wait_seconds=65
            )
            await asyncio.sleep(65)
    logger.error("voyage_rate_limit_exhausted_eval")
    raise RuntimeError("Could not embed text after retries")


def cosine_similarity(v1: list, v2: list) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


async def llm_judge_match(predicted_concern: str, actual_comment: str) -> Dict[str, Any]:
    """Final confirmation stage of the matching pipeline: positional gate ->
    embedding similarity (candidate floor) -> LLM-as-judge -> label. Embedding
    similarity between two short, differently-worded sentences is a noisy
    signal on its own; the judge decides whether a candidate pair actually
    raises the same concern, and records why."""
    prompt = JUDGE_PROMPT.format(predicted=predicted_concern, actual=actual_comment)
    result = await call_groq_json(prompt)
    if result is None:
        return {"same_concern": False, "reason": "judge_error: groq call failed"}
    return {
        "same_concern": bool(result.get("same_concern", False)),
        "reason": result.get("reason", "")
    }


async def record_ground_truth(
    owner: str,
    repo: str,
    pr_number: int,
    comment: Dict[str, Any]
) -> None:
    """When a real reviewer leaves a comment, check if any of CodeLens's
    predictions for this PR match it — mark them as verified true positives."""

    comment_path = comment.get("path", "")
    comment_line = comment.get("line")
    comment_body = comment.get("body", "")
    comment_id = comment.get("id")

    if not comment_path or comment_line is None:
        logger.info("ground_truth_skipped_no_position", comment_id=comment_id)
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, path, predicted_line, concern
                FROM predictions
                WHERE repo_owner = :owner
                  AND repo_name = :repo
                  AND pr_number = :pr_number
                  AND path = :path
                  AND matched IS NULL
            """),
            {
                "owner": owner,
                "repo": repo,
                "pr_number": pr_number,
                "path": comment_path
            }
        )
        candidates = result.fetchall()

        if not candidates:
            async with AsyncSessionLocal() as session:
                fn = FalseNegative(
                    repo_owner=owner,
                    repo_name=repo,
                    pr_number=pr_number,
                    path=comment_path,
                    comment_line=comment_line,
                    comment_body=comment_body,
                    source_comment_id=comment_id
                )
                session.add(fn)
                await session.commit()

            logger.info(
                "false_negative_recorded",
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                path=comment_path,
                comment_id=comment_id
            )
            return

    positional_matches = [
        c for c in candidates
        if c.predicted_line is None or abs((c.predicted_line or 0) - comment_line) <= POSITIONAL_LINE_WINDOW
    ]

    if not positional_matches:
        async with AsyncSessionLocal() as session:
            fn = FalseNegative(
                repo_owner=owner,
                repo_name=repo,
                pr_number=pr_number,
                path=comment_path,
                comment_line=comment_line,
                comment_body=comment_body,
                source_comment_id=comment_id
            )
            session.add(fn)
            await session.commit()

        logger.info(
            "false_negative_recorded",
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            path=comment_path,
            comment_id=comment_id,
            reason="no_positional_match"
        )
        return

    # Compute every embedding up front, outside of any lock, so a slow or
    # rate-limited Voyage call never holds a row lock open. Uses the
    # general-purpose text model, not the code model, since this is a
    # natural-language comparison (concern sentence vs. reviewer comment).
    comment_vector = await embed_with_retry(comment_body, model=TEXT_EMBEDDING_MODEL)
    concern_vectors = {
        candidate.id: await embed_with_retry(candidate.concern, model=TEXT_EMBEDDING_MODEL)
        for candidate in positional_matches
    }
    concern_texts = {candidate.id: candidate.concern for candidate in positional_matches}
    candidate_ids = list(concern_vectors.keys())

    best_id = None
    best_similarity = 0.0
    for cid in candidate_ids:
        similarity = cosine_similarity(comment_vector, concern_vectors[cid])
        if similarity > best_similarity:
            best_similarity = similarity
            best_id = cid

    # LLM judge call also happens outside any lock, for the same reason as
    # the embeddings above: never hold a row lock open across a slow network call.
    judge_confirmed = False
    judge_reason = ""
    if best_id is not None and best_similarity >= SIMILARITY_FLOOR:
        judge = await llm_judge_match(concern_texts[best_id], comment_body)
        judge_confirmed = judge["same_concern"]
        judge_reason = judge["reason"]

    async with AsyncSessionLocal() as session:
        async with session.begin():
            # Re-check under lock: another concurrent ground-truth call may
            # have already claimed one of these predictions since we read
            # them above.
            result = await session.execute(
                text("""
                    SELECT id, matched
                    FROM predictions
                    WHERE id = ANY(:ids)
                    FOR UPDATE
                """),
                {"ids": candidate_ids}
            )
            still_open_ids = [row.id for row in result.fetchall() if row.matched is None]

            if not still_open_ids:
                logger.info(
                    "ground_truth_all_candidates_already_claimed",
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    path=comment_path
                )
                return

            match_confirmed = (
                best_id is not None
                and best_id in still_open_ids
                and best_similarity >= SIMILARITY_FLOOR
                and judge_confirmed
            )

            if match_confirmed:
                await session.execute(
                    text("""
                        UPDATE predictions
                        SET matched = true,
                            match_type = 'embedding+judge',
                            source_comment_id = :comment_id,
                            match_reason = :reason
                        WHERE id = :pred_id
                    """),
                    {"comment_id": comment_id, "pred_id": best_id, "reason": judge_reason}
                )
                logger.info(
                    "ground_truth_matched",
                    prediction_id=best_id,
                    similarity=round(best_similarity, 3),
                    judge_reason=judge_reason
                )
            else:
                fallback_reason = judge_reason or "below_similarity_floor"
                for cid in still_open_ids:
                    await session.execute(
                        text("""
                            UPDATE predictions
                            SET matched = false,
                                match_reason = :reason
                            WHERE id = :pred_id
                        """),
                        {"pred_id": cid, "reason": fallback_reason}
                    )
                logger.info(
                    "ground_truth_no_semantic_match",
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    best_similarity=round(best_similarity, 3) if best_id else 0,
                    judge_reason=judge_reason
                )
