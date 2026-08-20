from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "CodeLens"
    app_version: str = "0.1.0"
    debug: bool = True
    database_url: str
    github_app_id: int
    github_private_key_path: str
    github_installation_id: int
    redis_url: str
    voyage_api_key: str

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()