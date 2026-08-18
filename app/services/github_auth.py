import time
import jwt
import httpx
from pathlib import Path
from app.core.config import settings

GITHUB_API_BASE = "https://api.github.com"


def load_private_key() -> str:
    key_path = Path(settings.github_private_key_path)
    return key_path.read_text()


def generate_jwt() -> str:
    private_key = load_private_key()
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": str(settings.github_app_id)
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


async def get_installation_token() -> str:
    app_jwt = generate_jwt()
    url = f"{GITHUB_API_BASE}/app/installations/{settings.github_installation_id}/access_tokens"
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


async def get_github_client() -> httpx.AsyncClient:
    token = await get_installation_token()
    return httpx.AsyncClient(
        base_url=GITHUB_API_BASE,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
    )