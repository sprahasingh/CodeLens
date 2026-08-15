from fastapi import APIRouter, Depends
from app.schemas.repos import RepoListResponse
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


async def log_request():
    logger.info("request received")


@router.get("/repos", response_model=RepoListResponse)
async def list_repos(path: str = Depends(log_request)):
    return {
        "repos": [
            {"id": 1, "name": "my-project", "owner": "spraha"},
            {"id": 2, "name": "codelens", "owner": "spraha"}
        ]
    }