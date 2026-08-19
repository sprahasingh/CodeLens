# CodeLens

## Architecture

CodeLens uses a retrieval-augmented pipeline to surface historically relevant review feedback the moment a PR opens.

- **FastAPI** handles webhook ingestion and API endpoints
- **Celery + Redis** process PR diffs asynchronously
- **pgvector** stores and searches code embeddings
- **Claude** synthesizes retrieved feedback into actionable pre-review comments