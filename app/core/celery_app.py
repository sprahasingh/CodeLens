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
    broker_connection_retry=True,
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=None,
    broker_connection_timeout=10,
    broker_heartbeat=30,
    broker_transport_options={
        "visibility_timeout": 1800,
        "socket_keepalive": True,
        "socket_connect_timeout": 10,
        "health_check_interval": 30,
    },
    worker_prefetch_multiplier=1,
    worker_concurrency=1,
    worker_max_tasks_per_child=50,
    task_acks_late=True,
    # Child-loss cases are recovered from durable review leases by the
    # supervisor, avoiding Celery's unbounded worker-lost requeue loop.
    task_reject_on_worker_lost=False,
    task_acks_on_failure_or_timeout=True,
    task_soft_time_limit=900,
    task_time_limit=960,
    task_default_expires=settings.review_task_expires_seconds,
    task_publish_retry=False,
    worker_enable_remote_control=True,

    redis_backend_use_ssl={
        "ssl_cert_reqs": "required"
    }
)
