from pydantic import Field
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
    groq_max_concurrency: int = Field(default=1, ge=1, le=4)
    groq_max_retries: int = Field(default=2, ge=0, le=5)
    groq_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    groq_retry_base_seconds: float = Field(default=1.0, ge=0, le=30)
    groq_retry_max_seconds: float = Field(default=20.0, ge=1, le=120)
    groq_rate_limit_cooldown_seconds: float = Field(default=60.0, ge=0, le=3600)
    groq_max_calls_per_pr: int = Field(default=12, ge=1, le=100)
    max_pr_hunks: int = Field(default=50, ge=1, le=500)
    review_task_expires_seconds: int = Field(default=1800, ge=60, le=86400)
    review_task_max_retries: int = Field(default=2, ge=0, le=5)
    github_comment_max_retries: int = Field(default=2, ge=0, le=5)
    github_comment_retry_base_seconds: float = Field(default=1.0, ge=0, le=30)
    github_comment_retry_max_seconds: float = Field(default=20.0, ge=1, le=120)
    worker_health_check_seconds: int = Field(default=30, ge=5, le=300)
    worker_health_failures_before_restart: int = Field(default=5, ge=2, le=10)
    worker_health_check_timeout_seconds: int = Field(default=5, ge=1, le=30)
    worker_restart_stability_seconds: int = Field(default=600, ge=60, le=3600)
    worker_disk_warning_percent: int = Field(default=85, ge=50, le=99)
    worker_shutdown_grace_seconds: int = Field(default=970, ge=30, le=1200)
    ntfy_topic: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
