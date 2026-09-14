import asyncio
import structlog
import httpx
from typing import Optional
from httpx import AsyncClient, Response
from app.services.github_auth import get_github_client
from app.core.config import settings

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


async def paginate(client, url, params, max_items: int = None):
    results = []
    current_url = url
    page_num = 0
    per_page = params.get("per_page", 30) if params else 30

    while current_url:
        page_num += 1

        response = await client.get(
            current_url,
            params=params if current_url == url else {}
        )

        await check_rate_limit(response)
        response.raise_for_status()

        page = response.json()

        if not page:
            break

        if not isinstance(page, list):
            logger.error(
                "github_unexpected_response",
                status_code=response.status_code,
                response=page
            )
            raise RuntimeError(
                f"Expected GitHub list response, got {type(page).__name__}"
            )

        results.extend(page)

        logger.info(
            "paginate_debug",
            page_num=page_num,
            page_size=len(page),
            total_so_far=len(results)
        )

        if max_items and len(results) >= max_items:
            results = results[:max_items]
            break

        if len(page) < per_page:
            break

        next_url = parse_next_page_url(response.headers.get("Link"))

        if next_url == current_url:
            break

        current_url = next_url

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


def get_public_client() -> httpx.AsyncClient:
    """HTTP client authenticated with a PAT for reading public repositories."""
    return httpx.AsyncClient(
    base_url="https://api.github.com",
    headers={
        "Authorization": f"Bearer {settings.github_pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    },
    timeout=30.0,
    follow_redirects=True
)


async def fetch_public_review_comments(
    owner: str,
    repo: str,
    max_comments: int = 2000
) -> list:
    """Fetch review comments from a public repository using PAT authentication."""

    if not settings.github_pat:
        logger.error("github_pat_not_configured")
        return []

    async with get_public_client() as client:
        comments = await paginate(
            client,
            f"/repos/{owner}/{repo}/pulls/comments",
            params={"per_page": 100, "sort": "created", "direction": "desc"},
            max_items=max_comments
        )

    logger.info(
        "public_review_comments_fetched",
        owner=owner,
        repo=repo,
        count=len(comments)
    )
    return comments