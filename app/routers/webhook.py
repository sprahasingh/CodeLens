import structlog
from fastapi import APIRouter, Request, HTTPException
from app.tasks.pr_tasks import process_pr

logger = structlog.get_logger()

router = APIRouter()


@router.post("/webhook")
async def handle_webhook(request: Request):
    event = request.headers.get("X-GitHub-Event")
    payload = await request.json()

    logger.info("webhook_received", event=event)

    if event == "ping":
        return {"status": "pong"}

    if event == "pull_request":
        action = payload.get("action")
        if action == "opened":
            pr_number = payload["pull_request"]["number"]
            repo_name = payload["repository"]["name"]
            owner = payload["repository"]["owner"]["login"]
            process_pr.delay(pr_number, repo_name, owner)
            logger.info("pr_job_queued", pr_number=pr_number, repo=repo_name)
            return {"status": "queued"}

    return {"status": "ignored"}