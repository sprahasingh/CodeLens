#!/usr/bin/env python3
"""Manage a short-lived, runner-specific SSH ingress rule for CodeLens.

All AWS errors are deliberately reduced to fixed messages. This helper only
operates on the configured CodeLens security group and only revokes rule IDs
that it can verify are tagged as its own temporary rules.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


AWS_REGION = "eu-north-1"
SECURITY_GROUP_ID = "sg-05c1462a133b8d00f"
SSH_PORT = 22
RULE_TTL_SECONDS = 2 * 60 * 60
MANAGED_TAG_KEY = "CodeLensManagedBy"
MANAGED_TAG_VALUE = "CodeLensGitHubActions"
RUN_TAG_KEY = "CodeLensRun"
EXPIRY_TAG_KEY = "CodeLensExpiresAt"
RULE_DESCRIPTION_PREFIX = "codelens-gha"
PROTECTED_RULE_IDS = {"sgr-070a3d2767818f890"}
PROTECTED_CIDRS = {"49.43.161.185/32"}
PUBLIC_IP_ENDPOINT = "https://checkip.amazonaws.com/"


class SafeFailure(Exception):
    """An error with a fixed, safe message suitable for Actions output."""


def get_runner_ipv4(opener: Callable[..., Any] = urlopen) -> str:
    """Return one globally routable IPv4 from AWS's HTTPS checkip endpoint."""
    request = Request(PUBLIC_IP_ENDPOINT, headers={"User-Agent": "CodeLens-deploy"})
    try:
        with opener(request, timeout=10) as response:
            final_url = urlsplit(response.geturl())
            if final_url.scheme != "https" or final_url.hostname != "checkip.amazonaws.com":
                raise SafeFailure("Runner IPv4 endpoint was not trusted.")
            if getattr(response, "status", 200) != 200:
                raise SafeFailure("Runner IPv4 lookup failed.")
            body = response.read(65)
            if len(body) > 64:
                raise SafeFailure("Runner IPv4 response was too long.")
            raw = body.decode("ascii")
    except SafeFailure:
        raise
    except Exception as exc:
        raise SafeFailure("Runner IPv4 lookup failed.") from exc

    candidate = raw.rstrip("\r\n")
    if raw not in (candidate, candidate + "\n", candidate + "\r\n"):
        raise SafeFailure("Runner IPv4 response was not a single address.")
    try:
        address = ipaddress.IPv4Address(candidate)
    except ipaddress.AddressValueError as exc:
        raise SafeFailure("Runner IPv4 response was invalid.") from exc
    if not address.is_global:
        raise SafeFailure("Runner IPv4 response was not public.")
    return str(address)


def _aws_json(*arguments: str) -> dict[str, Any]:
    command = ["aws", "ec2", *arguments, "--region", AWS_REGION, "--output", "json", "--no-cli-pager"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=25, check=False)
    except Exception as exc:
        raise SafeFailure("AWS security-group operation failed.") from exc
    if result.returncode != 0:
        raise SafeFailure("AWS security-group operation failed.")
    try:
        payload = json.loads(result.stdout)
    except Exception as exc:
        raise SafeFailure("AWS security-group response was invalid.") from exc
    if not isinstance(payload, dict):
        raise SafeFailure("AWS security-group response was invalid.")
    return payload


def _tags(rule: dict[str, Any]) -> dict[str, str]:
    return {
        str(tag.get("Key")): str(tag.get("Value"))
        for tag in rule.get("Tags", [])
        if isinstance(tag, dict) and tag.get("Key") is not None
    }


def _safe_rule(rule: dict[str, Any], run_key: str | None = None) -> bool:
    if rule.get("GroupId") != SECURITY_GROUP_ID:
        return False
    rule_id = rule.get("SecurityGroupRuleId")
    if not isinstance(rule_id, str) or not re.fullmatch(r"sgr-[0-9a-f]+", rule_id):
        return False
    if rule_id in PROTECTED_RULE_IDS:
        return False
    if rule.get("IsEgress") is not False:
        return False
    if rule.get("IpProtocol") != "tcp" or rule.get("FromPort") != SSH_PORT or rule.get("ToPort") != SSH_PORT:
        return False
    cidr = rule.get("CidrIpv4")
    if cidr in PROTECTED_CIDRS:
        return False
    try:
        network = ipaddress.IPv4Network(cidr, strict=True)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, TypeError):
        return False
    if network.prefixlen != 32 or not network.network_address.is_global:
        return False
    tags = _tags(rule)
    if tags.get(MANAGED_TAG_KEY) != MANAGED_TAG_VALUE:
        return False
    if run_key is not None and tags.get(RUN_TAG_KEY) != run_key:
        return False
    return bool(re.fullmatch(r"codelens-gha:[0-9]+-[0-9]+", str(rule.get("Description", ""))))


def _describe_rule(rule_id: str) -> list[dict[str, Any]]:
    result = _aws_json(
        "describe-security-group-rules",
        "--filters",
        f"Name=group-id,Values={SECURITY_GROUP_ID}",
        f"Name=security-group-rule-id,Values={rule_id}",
    )
    rules = result.get("SecurityGroupRules")
    if not isinstance(rules, list):
        raise SafeFailure("AWS security-group response was invalid.")
    return [rule for rule in rules if isinstance(rule, dict)]


def _revoke_verified(rule_id: str, run_key: str | None = None) -> bool:
    if rule_id in PROTECTED_RULE_IDS:
        raise SafeFailure("Refusing to revoke a protected SSH rule.")
    rules = _describe_rule(rule_id)
    if not rules:
        return False
    if len(rules) != 1 or not _safe_rule(rules[0], run_key=run_key):
        raise SafeFailure("Refusing to revoke an unverified SSH rule.")
    _aws_json(
        "revoke-security-group-ingress",
        "--group-id",
        SECURITY_GROUP_ID,
        "--security-group-rule-ids",
        rule_id,
    )
    if _describe_rule(rule_id):
        raise SafeFailure("Temporary SSH rule cleanup could not be verified.")
    return True


def _recovery_identity_is_valid(rule: dict[str, Any]) -> bool:
    """Validate ownership metadata for an expired rule without trusting shape."""
    rule_id = rule.get("SecurityGroupRuleId")
    if rule.get("GroupId") != SECURITY_GROUP_ID:
        return False
    if not isinstance(rule_id, str) or not re.fullmatch(r"sgr-[0-9a-f]+", rule_id):
        return False
    if rule_id in PROTECTED_RULE_IDS or rule.get("CidrIpv4") in PROTECTED_CIDRS:
        return False
    tags = _tags(rule)
    run_key = tags.get(RUN_TAG_KEY, "")
    if tags.get(MANAGED_TAG_KEY) != MANAGED_TAG_VALUE:
        return False
    if not re.fullmatch(r"[0-9]+-[0-9]+", run_key):
        return False
    if not re.fullmatch(r"[0-9]+", tags.get(EXPIRY_TAG_KEY, "")):
        return False
    return rule.get("Description") == f"{RULE_DESCRIPTION_PREFIX}:{run_key}"


def _revoke_expired_owned_rule(rule_id: str) -> bool:
    if rule_id in PROTECTED_RULE_IDS:
        return False
    rules = _describe_rule(rule_id)
    if not rules:
        return False
    if len(rules) != 1 or not _recovery_identity_is_valid(rules[0]):
        raise SafeFailure("Refusing to revoke an unverified expired SSH rule.")
    _aws_json(
        "revoke-security-group-ingress",
        "--group-id",
        SECURITY_GROUP_ID,
        "--security-group-rule-ids",
        rule_id,
    )
    if _describe_rule(rule_id):
        raise SafeFailure("Temporary SSH recovery could not be verified.")
    return True


def authorize(
    run_id: str,
    attempt: str,
    *,
    now: int | None = None,
    opener: Callable[..., Any] | None = None,
) -> str:
    if not re.fullmatch(r"[0-9]+", run_id) or not re.fullmatch(r"[0-9]+", attempt):
        raise SafeFailure("Workflow run identity is invalid.")
    ip = get_runner_ipv4() if opener is None else get_runner_ipv4(opener=opener)
    run_key = f"{run_id}-{attempt}"
    expiry = (int(time.time()) if now is None else now) + RULE_TTL_SECONDS
    description = f"{RULE_DESCRIPTION_PREFIX}:{run_key}"
    ip_permissions = [{
        "IpProtocol": "tcp",
        "FromPort": SSH_PORT,
        "ToPort": SSH_PORT,
        "IpRanges": [{"CidrIp": f"{ip}/32", "Description": description}],
    }]
    tags = [{"Key": MANAGED_TAG_KEY, "Value": MANAGED_TAG_VALUE},
            {"Key": RUN_TAG_KEY, "Value": run_key},
            {"Key": EXPIRY_TAG_KEY, "Value": str(expiry)}]
    response = _aws_json(
        "authorize-security-group-ingress",
        "--group-id",
        SECURITY_GROUP_ID,
        "--ip-permissions",
        json.dumps(ip_permissions, separators=(",", ":")),
        "--tag-specifications",
        json.dumps([{"ResourceType": "security-group-rule", "Tags": tags}], separators=(",", ":")),
    )
    rules = response.get("SecurityGroupRules")
    if not isinstance(rules, list) or len(rules) != 1 or not isinstance(rules[0], dict):
        raise SafeFailure("AWS did not return one temporary SSH rule.")
    rule = rules[0]
    if not _safe_rule(rule, run_key=run_key):
        raise SafeFailure("AWS returned an unexpected temporary SSH rule.")
    if rule.get("CidrIpv4") != f"{ip}/32" or _tags(rule).get(EXPIRY_TAG_KEY) != str(expiry):
        raise SafeFailure("AWS returned an unexpected temporary SSH rule.")
    rule_id = str(rule["SecurityGroupRuleId"])
    _write_output("ssh_rule_id", rule_id)
    print(f"Temporary SSH rule authorized for this runner; rule_id={rule_id}")
    return rule_id


def revoke(rule_id: str, run_id: str, attempt: str) -> bool:
    if not re.fullmatch(r"sgr-[0-9a-f]+", rule_id):
        raise SafeFailure("Temporary SSH rule ID is invalid.")
    if not re.fullmatch(r"[0-9]+", run_id) or not re.fullmatch(r"[0-9]+", attempt):
        raise SafeFailure("Workflow run identity is invalid.")
    removed = _revoke_verified(rule_id, run_key=f"{run_id}-{attempt}")
    print("Temporary SSH rule cleanup: " + ("PASS" if removed else "already absent"))
    return removed


def recover_expired(*, now: int | None = None) -> int:
    now = int(time.time()) if now is None else now
    response = _aws_json(
        "describe-security-group-rules",
        "--filters",
        f"Name=group-id,Values={SECURITY_GROUP_ID}",
        f"Name=tag:{MANAGED_TAG_KEY},Values={MANAGED_TAG_VALUE}",
    )
    rules = response.get("SecurityGroupRules")
    if not isinstance(rules, list):
        raise SafeFailure("AWS security-group response was invalid.")
    candidates: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise SafeFailure("AWS returned an invalid managed SSH rule.")
        tags = _tags(rule)
        rule_id = rule.get("SecurityGroupRuleId")
        if rule_id in PROTECTED_RULE_IDS:
            continue
        if not _recovery_identity_is_valid(rule):
            raise SafeFailure("A managed SSH rule has invalid recovery metadata.")
        if int(tags[EXPIRY_TAG_KEY]) <= now:
            candidates.append(rule_id)

    removed = 0
    for rule_id in candidates:
        if _revoke_expired_owned_rule(rule_id):
            removed += 1
    print(f"Expired temporary SSH rules removed: {removed}")
    return removed


def _write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("authorize")
    auth.add_argument("--run-id", required=True)
    auth.add_argument("--attempt", required=True)
    clean = commands.add_parser("revoke")
    clean.add_argument("--rule-id", required=True)
    clean.add_argument("--run-id", required=True)
    clean.add_argument("--attempt", required=True)
    commands.add_parser("recover-expired")
    args = parser.parse_args(argv)
    try:
        if args.command == "authorize":
            authorize(args.run_id, args.attempt)
        elif args.command == "revoke":
            revoke(args.rule_id, args.run_id, args.attempt)
        else:
            recover_expired()
    except SafeFailure:
        # Exception text is intentionally never rendered: even a custom
        # SafeFailure created by a caller could contain sensitive data.
        print("SSH access operation failed safely.", file=sys.stderr)
        return 1
    except Exception:
        print("SSH access operation failed safely.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
