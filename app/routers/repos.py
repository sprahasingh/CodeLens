import structlog
from fastapi import APIRouter, Depends
from app.schemas.repos import RepoListResponse

logger = structlog.get_logger()

router = APIRouter()


async def log_request():
    logger.info("repos_request_received")


@router.get("/repos", response_model=RepoListResponse)
async def list_repos(path: str = Depends(log_request)):
    return {
        "repos": [
            {"id": 1, "name": "my-project", "owner": "spraha"},
            {"id": 2, "name": "codelens", "owner": "spraha"}
        ]
    }