import voyageai
import structlog
from typing import List
from app.core.config import settings

logger = structlog.get_logger()

client = voyageai.Client(api_key=settings.voyage_api_key)

EMBEDDING_MODEL = "voyage-code-4"       # code-to-code retrieval (diff hunks)
EMBEDDING_DIMENSION = 1024

# General-purpose text model for natural-language matching (synthesized
# concern vs. a real reviewer's comment) — voyage-code-4 is trained for code
# retrieval, not for distinguishing fine-grained claims between two prose
# sentences, so the matching step uses a separate model. Never stored in
# pgvector (only review_comments.embedding is), so its dimension doesn't
# need to match EMBEDDING_DIMENSION.
TEXT_EMBEDDING_MODEL = "voyage-4-lite"


def embed_texts(texts: List[str], model: str = EMBEDDING_MODEL) -> List[List[float]]:
    if not texts:
        return []
    result = client.embed(texts, model=model, input_type="document")
    logger.info("texts_embedded", count=len(texts), model=model)
    return result.embeddings


def embed_single(text: str, model: str = EMBEDDING_MODEL) -> List[float]:
    embeddings = embed_texts([text], model=model)
    return embeddings[0]