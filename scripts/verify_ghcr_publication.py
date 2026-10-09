#!/usr/bin/env python3
"""Verify a prior trusted CodeLens GHCR publication without pulling its image."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REPOSITORY = "sprahasingh/CodeLens"
IMAGE_REPOSITORY = "sprahasingh/codelens"
WORKFLOW_PATH = ".github/workflows/production.yml"
ACCEPT_MANIFESTS = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class VerificationError(Exception):
    """A verification failed; details are deliberately not emitted to logs."""


def _request(url: str, headers: dict[str, str]) -> tuple[dict[str, str], bytes]:
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read(5_000_001)
            if len(body) > 5_000_000:
                raise VerificationError
            return dict(response.headers.items()), body
    except VerificationError:
        raise
    except (URLError, OSError, ValueError, TimeoutError) as exc:
        raise VerificationError from exc


def _json_body(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError from exc
    if not isinstance(value, dict):
        raise VerificationError
    return value


def _github_json(url: str, token: str) -> dict[str, Any]:
    _, body = _request(
        url,
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "CodeLens-existing-image-verifier",
        },
    )
    return _json_body(body)


def _digest_matches(raw: bytes, expected: str, headers: dict[str, str]) -> bool:
    actual = "sha256:" + hashlib.sha256(raw).hexdigest()
    header_digest = next(
        (value for key, value in headers.items() if key.lower() == "docker-content-digest"),
        actual,
    )
    return actual == expected and header_digest == expected


def _registry_headers(token: str, *, accept: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if accept:
        headers["Accept"] = ACCEPT_MANIFESTS
    return headers


def _registry_token(username: str, github_token: str) -> str:
    basic = base64.b64encode(f"{username}:{github_token}".encode()).decode()
    url = "https://ghcr.io/token?" + urlencode(
        {"service": "ghcr.io", "scope": f"repository:{IMAGE_REPOSITORY}:pull"}
    )
    _, body = _request(url, {"Authorization": f"Basic {basic}"})
    payload = _json_body(body)
    token = payload.get("token") or payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise VerificationError
    return token


def _fetch_manifest(reference: str, registry_token: str) -> tuple[dict[str, Any], dict[str, str], bytes]:
    url = f"https://ghcr.io/v2/{IMAGE_REPOSITORY}/manifests/{reference}"
    headers, body = _request(url, _registry_headers(registry_token, accept=True))
    return _json_body(body), headers, body


def _verify_config(config_digest: str, registry_token: str, source_commit: str) -> None:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", config_digest):
        raise VerificationError
    url = f"https://ghcr.io/v2/{IMAGE_REPOSITORY}/blobs/{config_digest}"
    headers, body = _request(url, _registry_headers(registry_token))
    if not _digest_matches(body, config_digest, headers):
        raise VerificationError
    config = _json_body(body)
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        raise VerificationError
    labels = config.get("config", {}).get("Labels", {})
    if not isinstance(labels, dict) or labels.get("org.opencontainers.image.revision") != source_commit:
        raise VerificationError


def _verify_registry_image(source_commit: str, expected_digest: str, github_token: str, username: str) -> None:
    registry_token = _registry_token(username, github_token)
    index, headers, raw = _fetch_manifest(source_commit, registry_token)
    if not _digest_matches(raw, expected_digest, headers):
        raise VerificationError

    media_type = index.get("mediaType", "")
    if media_type in {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }:
        descriptors = index.get("manifests")
        if not isinstance(descriptors, list):
            raise VerificationError
        amd64 = [
            item
            for item in descriptors
            if isinstance(item, dict)
            and isinstance(item.get("platform"), dict)
            and item["platform"].get("architecture") == "amd64"
            and item["platform"].get("os") == "linux"
        ]
        if len(amd64) != 1:
            raise VerificationError
        child_digest = amd64[0].get("digest")
        if not isinstance(child_digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", child_digest):
            raise VerificationError
        child, child_headers, child_raw = _fetch_manifest(child_digest, registry_token)
        if not _digest_matches(child_raw, child_digest, child_headers):
            raise VerificationError
        manifest = child
    elif media_type in {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }:
        manifest = index
    else:
        raise VerificationError

    config_digest = manifest.get("config", {}).get("digest")
    if not isinstance(config_digest, str):
        raise VerificationError
    _verify_config(config_digest, registry_token, source_commit)


def verify_publication(
    *, publication_run_id: str, source_commit: str, image_digest: str, github_token: str, username: str
) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,19}", publication_run_id):
        raise VerificationError
    if not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        raise VerificationError
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_digest):
        raise VerificationError
    if not github_token or not username or "\n" in username:
        raise VerificationError

    api = f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{publication_run_id}"
    run = _github_json(api, github_token)
    path = str(run.get("path", "")).split("@", 1)[0]
    repo = run.get("repository", {}).get("full_name")
    if (
        path != WORKFLOW_PATH
        or repo != REPOSITORY
        or run.get("head_branch") != "main"
        or run.get("head_sha") != source_commit
        or run.get("event") not in {"push", "workflow_dispatch"}
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
    ):
        raise VerificationError

    jobs_url = f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{publication_run_id}/jobs?per_page=100"
    jobs = _github_json(jobs_url, github_token).get("jobs")
    if not isinstance(jobs, list):
        raise VerificationError
    job_by_name = {job.get("name"): job for job in jobs if isinstance(job, dict)}
    test_job = job_by_name.get("test")
    publish_job = job_by_name.get("publish")
    if not isinstance(test_job, dict) or test_job.get("conclusion") != "success":
        raise VerificationError
    if not isinstance(publish_job, dict) or publish_job.get("conclusion") != "success":
        raise VerificationError
    successful_steps = {
        step.get("name")
        for step in publish_job.get("steps", [])
        if isinstance(step, dict) and step.get("conclusion") == "success"
    }
    if not {
        "Run full suite inside the exact production image",
        "Push the exact tested image to GHCR",
    }.issubset(successful_steps):
        raise VerificationError

    _verify_registry_image(source_commit, image_digest, github_token, username)
    return f"ghcr.io/{IMAGE_REPOSITORY}@{image_digest}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publication-run-id", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--image-digest", required=True)
    args = parser.parse_args(argv)
    try:
        image = verify_publication(
            publication_run_id=args.publication_run_id,
            source_commit=args.source_commit,
            image_digest=args.image_digest,
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            username=os.environ.get("GITHUB_ACTOR", ""),
        )
        output_file = os.environ.get("GITHUB_OUTPUT")
        if not output_file:
            raise VerificationError
        with open(output_file, "a", encoding="utf-8") as output:
            output.write(f"image={image}\nsource_commit={args.source_commit}\n")
        print("EXISTING_IMAGE_VERIFICATION: PASS")
        return 0
    except Exception:
        print("EXISTING_IMAGE_VERIFICATION: FAIL", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
