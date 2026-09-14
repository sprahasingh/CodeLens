import structlog
from fastapi import APIRouter
from sqlalchemy import text
from app.core.database import AsyncSessionLocal

logger = structlog.get_logger()

router = APIRouter()


@router.get("/metrics/accuracy")
async def get_accuracy_metrics(owner: str = None, repo: str = None):
    """Compute precision, recall, and F1 from evaluated predictions
    and recorded false negatives.

    True Positive  = prediction that matched a real review comment
    False Positive = prediction that was evaluated but did NOT match
    False Negative = real review comment with no matching prediction nearby
    """

    where_clause = ""
    params = {}
    if owner and repo:
        where_clause = "WHERE repo_owner = :owner AND repo_name = :repo"
        params = {"owner": owner, "repo": repo}

    async with AsyncSessionLocal() as session:
        pred_result = await session.execute(
            text(f"""
                SELECT
                    COUNT(*) AS total_predictions,
                    COUNT(*) FILTER (WHERE matched IS TRUE) AS true_positives,
                    COUNT(*) FILTER (WHERE matched IS FALSE) AS false_positives,
                    COUNT(*) FILTER (WHERE matched IS NULL) AS unevaluated
                FROM predictions
                {where_clause}
            """),
            params
        )
        pred_row = pred_result.fetchone()

        fn_result = await session.execute(
            text(f"""
                SELECT COUNT(*) AS false_negatives
                FROM false_negatives
                {where_clause}
            """),
            params
        )
        fn_row = fn_result.fetchone()

    tp = pred_row.true_positives
    fp = pred_row.false_positives
    fn = fn_row.false_negatives
    unevaluated = pred_row.unevaluated
    total = pred_row.total_predictions

    evaluated = tp + fp
    precision = round(tp / evaluated, 3) if evaluated > 0 else None
    recall = round(tp / (tp + fn), 3) if (tp + fn) > 0 else None
    f1 = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = round(2 * precision * recall / (precision + recall), 3)

    logger.info(
        "accuracy_metrics_computed",
        owner=owner,
        repo=repo,
        total_predictions=total,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        unevaluated=unevaluated,
        precision=precision,
        recall=recall,
        f1=f1
    )

    return {
        "total_predictions": total,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "unevaluated": unevaluated,
        "precision": precision,
        "recall": recall,
        "f1": f1
    }