import asyncio
import structlog
from typing import Optional
from httpx import AsyncClient, Response
from app.services.github_auth import get_github_client

logger = structlog.get_logger()


def parse_next_page_url(link_header: Optional[str]) -> Optional[str]:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            url = part.split(";")[0].strip().strip("<>")
            return url
    return None


async def check_rate_limit(response: Response) -> None:
    remaining = int(response.headers.get("X-RateLimit-Remaining", 1))
    reset_at = int(response.headers.get("X-RateLimit-Reset", 0))
    if remaining < 10:
        import time
        wait_seconds = max(0, reset_at - int(time.time())) + 5
        logger.warning(
            "rate_limit_low",
            remaining=remaining,
            waiting_seconds=wait_seconds
        )
        await asyncio.sleep(wait_seconds)


async def paginate(client: AsyncClient, url: str, params: dict = {}) -> list:
    results = []
    current_url = url
    while current_url:
        response = await client.get(current_url, params=params if current_url == url else {})
        response.raise_for_status()
        await check_rate_limit(response)
        results.extend(response.json())
        current_url = parse_next_page_url(response.headers.get("Link"))
        params = {}
    return results


async def fetch_pr_diff(owner: str, repo: str, pr_number: int) -> str:
    async with await get_github_client() as client:
        response = await client.get(
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            headers={"Accept": "application/vnd.github.v3.diff"}
        )
        response.raise_for_status()
        await check_rate_limit(response)
        logger.info("pr_diff_fetched", owner=owner, repo=repo, pr_number=pr_number)
        return response.text


async def fetch_pr_review_comments(owner: str, repo: str, pr_number: int) -> list:
    async with await get_github_client() as client:
        comments = await paginate(
            client,
            f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
            params={"per_page": 100}
        )
        logger.info(
            "pr_review_comments_fetched",
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            count=len(comments)
        )
        return comments


async def fetch_all_review_comments(owner: str, repo: str) -> list:
    async with await get_github_client() as client:
        comments = await paginate(
            client,
            f"/repos/{owner}/{repo}/pulls/comments",
            params={"per_page": 100, "sort": "created", "direction": "desc"}
        )
        logger.info(
            "all_review_comments_fetched",
            owner=owner,
            repo=repo,
            count=len(comments)
        )
        return comments