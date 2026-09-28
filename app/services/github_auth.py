import time
import jwt
import httpx
from pathlib import Path
from app.core.config import settings

GITHUB_API_BASE = "https://api.github.com"


def load_private_key() -> str:
    if settings.github_private_key:
        return settings.github_private_key
    if settings.github_private_key_path:
        return Path(settings.github_private_key_path).read_text()
    raise RuntimeError(
        "Set either GITHUB_PRIVATE_KEY (raw PEM content) or "
        "GITHUB_PRIVATE_KEY_PATH (a file path)."
    )


def generate_jwt() -> str:
    private_key = load_private_key()
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": str(settings.github_app_id)
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


async def get_installation_token(installation_id: int | None = None) -> str:
    app_jwt = generate_jwt()
    iid = installation_id or settings.github_installation_id
    url = f"{GITHUB_API_BASE}/app/installations/{iid}/access_tokens"
    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"
            }
        )
        response.raise_for_status()
        return response.json()["token"]


async def get_installation_id_for_repo(owner: str, repo: str) -> int:
    app_jwt = generate_jwt()
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/installation",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"
            }
        )
        response.raise_for_status()
        return response.json()["id"]


async def get_github_client(owner: str | None = None, repo: str | None = None) -> httpx.AsyncClient:
    if owner and repo:
        installation_id = await get_installation_id_for_repo(owner, repo)
        token = await get_installation_token(installation_id)
    else:
        token = await get_installation_token()
    return httpx.AsyncClient(
        base_url=GITHUB_API_BASE,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
    )