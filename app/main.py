import logging
from fastapi import FastAPI
from app.routers import repos

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

app = FastAPI(
    title="CodeLens",
    description="A retrieval-augmented code review engine",
    version="0.1.0"
)

app.include_router(repos.router)


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "codelens"}