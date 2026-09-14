import structlog
from typing import Dict, Any
from sqlalchemy import text
from app.core.database import AsyncSessionLocal
from app.services.embedder import embed_single
from voyageai.error import RateLimitError
import asyncio
from app.models.false_negative import FalseNegative

logger = structlog.get_logger()

POSITIONAL_LINE_WINDOW = 10
EMBEDDING_MATCH_THRESHOLD = 0.75


async def embed_with_retry(text_to_embed: str, max_retries: int = 3) -> list:
    retry_count = 0
    while retry_count < max_retries:
        try:
            return embed_single(text_to_embed)
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

    comment_vector = await embed_with_retry(comment_body)
    comment_vector_str = "[" + ",".join(str(x) for x in comment_vector) + "]"

    best_match = None
    best_similarity = 0.0

    async with AsyncSessionLocal() as session:
        for candidate in positional_matches:
            concern_vector = await embed_with_retry(candidate.concern)
            concern_vector_str = "[" + ",".join(str(x) for x in concern_vector) + "]"

            result = await session.execute(
                text("""
                    SELECT 1 - (CAST(:v1 AS vector) <=> CAST(:v2 AS vector)) AS similarity
                """),
                {"v1": comment_vector_str, "v2": concern_vector_str}
            )
            row = result.fetchone()
            similarity = row.similarity if row else 0.0

            if similarity > best_similarity:
                best_similarity = similarity
                best_match = candidate

    async with AsyncSessionLocal() as session:
        if best_match and best_similarity >= EMBEDDING_MATCH_THRESHOLD:
            await session.execute(
                text("""
                    UPDATE predictions
                    SET matched = true,
                        match_type = 'embedding',
                        source_comment_id = :comment_id
                    WHERE id = :pred_id
                """),
                {"comment_id": comment_id, "pred_id": best_match.id}
            )
            logger.info(
                "ground_truth_matched",
                prediction_id=best_match.id,
                similarity=round(best_similarity, 3)
            )
        else:
            for candidate in positional_matches:
                await session.execute(
                    text("""
                        UPDATE predictions
                        SET matched = false
                        WHERE id = :pred_id AND matched IS NULL
                    """),
                    {"pred_id": candidate.id}
                )
            logger.info(
                "ground_truth_no_semantic_match",
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                best_similarity=round(best_similarity, 3) if best_match else 0
            )
        await session.commit()