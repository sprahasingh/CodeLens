from fastapi import FastAPI
from pydantic import BaseModel

class Repo(BaseModel):
    id: int
    name: str
    owner: str

class RepoListResponse(BaseModel):
    repos: list[Repo]

app = FastAPI(
    title="CodeLens",
    description="A retrieval-augmented code review engine",
    version="0.1.0"
)

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "codelens"}

@app.get("/repos", response_model=RepoListResponse)
def list_repos():
    return {
        "repos": [
            {"id": 1, "name": "my-project", "owner": "spraha"},
            {"id": 2, "name": "codelens", "owner": "spraha"}
        ]
    }