from pydantic import BaseModel


class Repo(BaseModel):
    id: int
    name: str
    owner: str


class RepoListResponse(BaseModel):
    repos: list[Repo]