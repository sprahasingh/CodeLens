import structlog
from fastapi import APIRouter, Depends
from app.schemas.repos import RepoListResponse
from app.services.github_auth import get_github_client

logger = structlog.get_logger()

router = APIRouter()


@router.get("/repos", response_model=RepoListResponse)
async def list_repos():
    logger.info("fetching_repos_from_github")
    async with await get_github_client() as client:
        response = await client.get("/installation/repositories")
        response.raise_for_status()
        data = response.json()
    repos = [
        {
            "id": repo["id"],
            "name": repo["name"],
            "owner": repo["owner"]["login"]
        }
        for repo in data["repositories"]
    ]
    logger.info("repos_fetched", count=len(repos))
    return {"repos": repos}