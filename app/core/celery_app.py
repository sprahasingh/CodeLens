from celery import Celery
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from app.core.config import settings


def _with_ssl_cert_reqs(url: str) -> str:
    """Merge ssl_cert_reqs=CERT_REQUIRED into a rediss:// URL's query string
    without clobbering any query params the URL already has."""
    parts = urlsplit(url)
    if parts.scheme != "rediss":
        return url
    query = dict(parse_qsl(parts.query))
    query["ssl_cert_reqs"] = "CERT_REQUIRED"
    return urlunsplit(parts._replace(query=urlencode(query)))


redis_url = _with_ssl_cert_reqs(settings.redis_url)

celery_app = Celery(
    "codelens",
    broker=redis_url,
    backend=redis_url,
    include=["app.tasks.pr_tasks"]
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    redis_backend_use_ssl={
        "ssl_cert_reqs": "required"
    }
)