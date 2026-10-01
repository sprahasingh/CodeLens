from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "CodeLens"
    app_version: str = "0.1.0"
    debug: bool = True
    database_url: str
    github_app_id: int
    # Either github_private_key (raw PEM content, for platforms like Fly.io
    # where mounting a local file isn't practical) or github_private_key_path
    # (a file path, for local/Docker-Compose use) must be set.
    github_private_key: str = ""
    github_private_key_path: str = ""
    github_installation_id: int
    github_pat: str = ""
    redis_url: str
    voyage_api_key: str
    webhook_secret: str = ""
    groq_api_key: str
    groq_min_request_interval_seconds: int = 3
    ntfy_topic: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
