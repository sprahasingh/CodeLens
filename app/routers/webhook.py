import hmac
import hashlib
import structlog
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from app.tasks.pr_tasks import process_pr
from app.services.ingestion import ingest_repository
from app.core.config import settings

logger = structlog.get_logger()

router = APIRouter()


def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    if not settings.webhook_secret:
        return True
    expected = "sha256=" + hmac.new(
        settings.webhook_secret.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/webhook")
async def handle_webhook(request: Request):
    payload_bytes = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not verify_webhook_signature(payload_bytes, signature):
        logger.warning("webhook_signature_invalid")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    import json
    payload = json.loads(payload_bytes)
    gh_event = request.headers.get("X-GitHub-Event")
    logger.info("webhook_received", gh_event=gh_event)

    if gh_event == "ping":
        return {"status": "pong"}

    if gh_event == "pull_request":
        action = payload.get("action")
        if action in ("opened", "reopened"):
            pr_number = payload["pull_request"]["number"]
            repo_name = payload["repository"]["name"]
            owner = payload["repository"]["owner"]["login"]
            process_pr.delay(pr_number, repo_name, owner)
            logger.info("pr_job_queued", pr_number=pr_number, repo=repo_name)
            return {"status": "queued"}

    return {"status": "ignored"}


@router.post("/ingest/{owner}/{repo}")
async def trigger_ingestion(owner: str, repo: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(ingest_repository, owner, repo)
    logger.info("ingestion_triggered", owner=owner, repo=repo)
    return {"status": "ingestion started", "owner": owner, "repo": repo}