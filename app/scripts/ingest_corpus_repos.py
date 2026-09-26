import asyncio
import structlog
from app.services.ingestion import ingest_public_repository

logger = structlog.get_logger()

REPOS_TO_INGEST = [
    ("pallets", "flask"),
    ("encode", "httpx"),
    ("pydantic", "pydantic"),
]


async def main():
    results = {}
    for i, (owner, repo) in enumerate(REPOS_TO_INGEST):
        if i > 0:
            await asyncio.sleep(65)
        result = await ingest_public_repository(owner, repo)
        results[f"{owner}/{repo}"] = result

    logger.info("corpus_growth_complete", results=results)


if __name__ == "__main__":
    asyncio.run(main())
