import voyageai
import structlog
from typing import List
from app.core.config import settings

logger = structlog.get_logger()

client = voyageai.Client(api_key=settings.voyage_api_key)

EMBEDDING_MODEL = "voyage-code-4"
EMBEDDING_DIMENSION = 1024


def embed_texts(texts: List[str]) -> List[List[float]]:
    if not texts:
        return []
    result = client.embed(texts, model=EMBEDDING_MODEL, input_type="document")
    logger.info("texts_embedded", count=len(texts), model=EMBEDDING_MODEL)
    return result.embeddings


def embed_single(text: str) -> List[float]:
    embeddings = embed_texts([text])
    return embeddings[0]