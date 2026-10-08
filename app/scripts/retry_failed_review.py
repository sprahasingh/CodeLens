"""Manually requeue one terminal review claim by exact PR head SHA."""
import argparse
import asyncio
from datetime import datetime

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.processed_pr import ProcessedPR
from app.tasks.pr_tasks import process_pr


async def retry_review(owner: str, repo: str, pr_number: int, head_sha: str) -> None:
    async with AsyncSessionLocal() as session:
        claim = (await session.execute(select(ProcessedPR).where(
            ProcessedPR.repo_owner == owner,
            ProcessedPR.repo_name == repo,
            ProcessedPR.pr_number == pr_number,
            ProcessedPR.head_sha == head_sha,
            ProcessedPR.status == "failed",
        ).with_for_update())).scalar_one_or_none()
        if claim is None:
            raise ValueError("No failed review claim matched the supplied repository, PR, and head SHA")
        claim.status = "queued"
        claim.task_id = None
        claim.lease_owner = None
        claim.lease_expires_at = None
        claim.attempts = 0
        claim.dispatch_attempts = 1
        claim.review_payload = None
        claim.last_error = None
        claim.dispatched_at = datetime.utcnow()
        await session.commit()

    process_pr.apply_async(
        args=(pr_number, repo, owner),
        kwargs={"expected_head_sha": head_sha},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args()
    asyncio.run(retry_review(args.owner, args.repo, args.pr_number, args.head_sha))
    print("One failed review was queued for the specified PR head.")


if __name__ == "__main__":
    main()
