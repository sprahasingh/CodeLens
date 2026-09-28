import hmac
import hashlib
import structlog
import httpx
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from sqlalchemy.dialects.postgresql import insert
from app.tasks.pr_tasks import process_pr
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.services.ingestion import ingest_repository, ingest_public_repository
from app.services.evaluation import record_ground_truth
from app.models.processed_pr import ProcessedPR

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


async def claim_pr_processing(owner: str, repo_name: str, pr_number: int, head_sha: str) -> bool:
    """Atomically claim (repo, pr_number, head_sha) for processing.

    Returns True the first time this exact PR at this exact commit is seen,
    False if it was already claimed (duplicate webhook delivery, or a retry
    of the same delivery) so the caller can skip re-queuing/re-posting.
    """
    stmt = insert(ProcessedPR).values(
        repo_owner=owner,
        repo_name=repo_name,
        pr_number=pr_number,
        head_sha=head_sha
    ).on_conflict_do_nothing(
        index_elements=["repo_owner", "repo_name", "pr_number", "head_sha"]
    )
    async with AsyncSessionLocal() as session:
        result = await session.execute(stmt)
        await session.commit()
    return result.rowcount > 0


@router.post("/webhook")
async def handle_webhook(request: Request, background_tasks: BackgroundTasks):
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
        if action in ("opened", "reopened", "synchronize"):
            pr_number = payload["pull_request"]["number"]
            repo_name = payload["repository"]["name"]
            owner = payload["repository"]["owner"]["login"]
            head_sha = payload["pull_request"]["head"]["sha"]

            claimed = await claim_pr_processing(owner, repo_name, pr_number, head_sha)
            if not claimed:
                logger.info(
                    "duplicate_webhook_skipped",
                    pr_number=pr_number,
                    repo=repo_name,
                    head_sha=head_sha
                )
                return {"status": "duplicate_skipped"}

            if action == "synchronize":
                before_sha = payload.get("before")
                after_sha = payload.get("after")
                process_pr.delay(pr_number, repo_name, owner, before_sha=before_sha, after_sha=after_sha)
            else:
                process_pr.delay(pr_number, repo_name, owner)

            logger.info("pr_job_queued", pr_number=pr_number, repo=repo_name, head_sha=head_sha, action=action)
            return {"status": "queued"}

    if gh_event == "pull_request_review_comment":
        action = payload.get("action")
        if action == "created":
            comment = payload["comment"]
            repo_name = payload["repository"]["name"]
            owner = payload["repository"]["owner"]["login"]
            pr_number = payload["pull_request"]["number"]

            background_tasks.add_task(
                record_ground_truth,
                owner,
                repo_name,
                pr_number,
                comment
            )
            logger.info(
                "ground_truth_comment_received",
                owner=owner,
                repo=repo_name,
                pr_number=pr_number,
                comment_id=comment["id"]
            )
            return {"status": "ground_truth_recorded"}

    if gh_event == "installation":
        action = payload.get("action")
        if action == "created":
            account = payload.get("installation", {}).get("account", {})
            login = account.get("login", "unknown")
            account_type = account.get("type", "unknown")
            repos_selected = payload.get("installation", {}).get("repository_selection", "unknown")
            repo_count = len(payload.get("repositories", []))
            logger.info("app_installed", account=login, type=account_type, repos=repos_selected)
            background_tasks.add_task(_notify_installation, login, account_type, repos_selected, repo_count)
        return {"status": "ok"}

    return {"status": "ignored"}


async def _notify_installation(login: str, account_type: str, repos_selected: str, repo_count: int):
    if not settings.ntfy_topic:
        return
    repo_info = f"{repo_count} repo{'s' if repo_count != 1 else ''}" if repos_selected == "selected" else "all repos"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://ntfy.sh/{settings.ntfy_topic}",
                content=f"{login} ({account_type}) installed CodeLens on {repo_info}",
                headers={
                    "Title": "New CodeLens Installation",
                    "Priority": "high",
                    "Tags": "rocket",
                },
                timeout=10,
            )
    except Exception as e:
        logger.warning("ntfy_notification_failed", error=str(e))


@router.post("/ingest/{owner}/{repo}")
async def trigger_ingestion(owner: str, repo: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(ingest_repository, owner, repo)
    logger.info("ingestion_triggered", owner=owner, repo=repo)
    return {"status": "ingestion started", "owner": owner, "repo": repo}

@router.post("/ingest-public/{owner}/{repo}")
async def trigger_public_ingestion(owner: str, repo: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(ingest_public_repository, owner, repo)
    logger.info("public_ingestion_triggered", owner=owner, repo=repo)
    return {"status": "public ingestion started", "owner": owner, "repo": repo}