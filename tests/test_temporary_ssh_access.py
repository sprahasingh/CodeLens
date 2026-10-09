"""Regression coverage for temporary runner-only EC2 SSH access."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
import textwrap
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import manage_temporary_ssh as ssh_access


RUNNER_IP = "8.8.8.8"
RUNNER_CIDR = "8.8.8.8/32"
RULE_ID = "sgr-0123456789abcdef0"
SECRET = "never-log-this-aws-secret"


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.status = 200

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return self.body

    def geturl(self) -> str:
        return ssh_access.PUBLIC_IP_ENDPOINT


def managed_rule(
    *,
    rule_id: str = RULE_ID,
    run_key: str = "12345-1",
    expiry: int = 2000,
    cidr: str = RUNNER_CIDR,
    description: str | None = None,
) -> dict[str, object]:
    return {
        "GroupId": ssh_access.SECURITY_GROUP_ID,
        "SecurityGroupRuleId": rule_id,
        "IsEgress": False,
        "IpProtocol": "tcp",
        "FromPort": 22,
        "ToPort": 22,
        "CidrIpv4": cidr,
        "Description": description or f"codelens-gha:{run_key}",
        "Tags": [
            {"Key": ssh_access.MANAGED_TAG_KEY, "Value": ssh_access.MANAGED_TAG_VALUE},
            {"Key": ssh_access.RUN_TAG_KEY, "Value": run_key},
            {"Key": ssh_access.EXPIRY_TAG_KEY, "Value": str(expiry)},
        ],
    }


def test_valid_runner_ipv4_is_accepted() -> None:
    assert ssh_access.get_runner_ipv4(lambda *_args, **_kwargs: FakeResponse(b"8.8.8.8\n")) == RUNNER_IP


def test_ip_response_requires_the_trusted_https_host_and_bounded_body() -> None:
    response = FakeResponse(b"8.8.8.8\n")
    response.geturl = lambda: "http://checkip.amazonaws.com/"  # type: ignore[method-assign]
    with pytest.raises(ssh_access.SafeFailure):
        ssh_access.get_runner_ipv4(lambda *_args, **_kwargs: response)
    with pytest.raises(ssh_access.SafeFailure):
        ssh_access.get_runner_ipv4(lambda *_args, **_kwargs: FakeResponse(b"8.8.8.8\n" + b" " * 58))


@pytest.mark.parametrize("body", [b"10.0.0.1\n", b"127.0.0.1\n", b"8.8.8.8 1.1.1.1\n", b"not-an-ip\n"])
def test_private_or_invalid_runner_ip_is_rejected(body: bytes) -> None:
    with pytest.raises(ssh_access.SafeFailure):
        ssh_access.get_runner_ipv4(lambda *_args, **_kwargs: FakeResponse(body))


def test_authorization_is_exact_runner_ipv4_32_on_fixed_security_group(monkeypatch: pytest.MonkeyPatch) -> None:
    rule = managed_rule(expiry=ssh_access.RULE_TTL_SECONDS + 100)
    aws_call = Mock(return_value={"SecurityGroupRules": [rule]})
    monkeypatch.setattr(ssh_access, "_aws_json", aws_call)
    monkeypatch.setattr(ssh_access, "get_runner_ipv4", lambda: RUNNER_IP)
    monkeypatch.setattr(ssh_access, "_write_output", lambda *_: None)

    assert ssh_access.authorize("12345", "1", now=100) == RULE_ID
    args = aws_call.call_args.args
    assert args[:3] == ("authorize-security-group-ingress", "--group-id", ssh_access.SECURITY_GROUP_ID)
    permission = json.loads(args[4])
    assert permission == [{
        "IpProtocol": "tcp",
        "FromPort": 22,
        "ToPort": 22,
        "IpRanges": [{"CidrIp": RUNNER_CIDR, "Description": "codelens-gha:12345-1"}],
    }]
    tag_spec = json.loads(args[6])
    assert tag_spec[0]["ResourceType"] == "security-group-rule"
    assert {tag["Key"] for tag in tag_spec[0]["Tags"]} == {
        ssh_access.MANAGED_TAG_KEY,
        ssh_access.RUN_TAG_KEY,
        ssh_access.EXPIRY_TAG_KEY,
    }


def test_authorize_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(ssh_access, "get_runner_ipv4", lambda: RUNNER_IP)
    monkeypatch.setattr(
        ssh_access.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=SECRET, stderr=SECRET),
    )
    assert ssh_access.main(["authorize", "--run-id", "12345", "--attempt", "1"]) == 1
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert "SSH access operation failed safely" in output.err


def test_revoke_uses_only_the_exact_verified_rule_id_and_preserves_mac_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule = managed_rule()
    calls: list[tuple[str, ...]] = []

    def aws_call(*args: str) -> dict[str, object]:
        calls.append(args)
        if args[0] == "describe-security-group-rules":
            return {"SecurityGroupRules": [rule] if sum(x[0] == args[0] for x in calls) == 1 else []}
        return {}

    monkeypatch.setattr(ssh_access, "_aws_json", aws_call)
    assert ssh_access.revoke(RULE_ID, "12345", "1") is True
    revoke_call = next(call for call in calls if call[0] == "revoke-security-group-ingress")
    assert revoke_call == (
        "revoke-security-group-ingress",
        "--group-id",
        ssh_access.SECURITY_GROUP_ID,
        "--security-group-rule-ids",
        RULE_ID,
    )
    assert ssh_access.PROTECTED_RULE_IDS == {"sgr-070a3d2767818f890"}
    assert "sgr-070a3d2767818f890" not in revoke_call


def test_revoke_refuses_mac_rule_before_any_aws_call(monkeypatch: pytest.MonkeyPatch) -> None:
    aws_call = Mock()
    monkeypatch.setattr(ssh_access, "_aws_json", aws_call)
    with pytest.raises(ssh_access.SafeFailure):
        ssh_access.revoke("sgr-070a3d2767818f890", "12345", "1")
    aws_call.assert_not_called()


def test_revoke_refuses_protected_mac_cidr_even_if_rule_id_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    mac_rule = managed_rule(cidr="49.43.161.185/32")
    aws_call = Mock(side_effect=[{"SecurityGroupRules": [mac_rule]}])
    monkeypatch.setattr(ssh_access, "_aws_json", aws_call)
    with pytest.raises(ssh_access.SafeFailure):
        ssh_access.revoke(RULE_ID, "12345", "1")
    assert not any(call.args[0] == "revoke-security-group-ingress" for call in aws_call.call_args_list)


def test_expired_rule_recovery_removes_only_managed_rule_not_mac_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = managed_rule(expiry=900)
    mac = managed_rule(rule_id="sgr-070a3d2767818f890", expiry=900)
    calls: list[tuple[str, ...]] = []

    def aws_call(*args: str) -> dict[str, object]:
        calls.append(args)
        if args[0] == "describe-security-group-rules":
            if "--filters" in args and any(arg.startswith("Name=tag:") for arg in args):
                return {"SecurityGroupRules": [stale, mac]}
            return {"SecurityGroupRules": [stale] if sum(x[0] == args[0] for x in calls) == 2 else []}
        return {}

    monkeypatch.setattr(ssh_access, "_aws_json", aws_call)
    assert ssh_access.recover_expired(now=1000) == 1
    revoke_calls = [call for call in calls if call[0] == "revoke-security-group-ingress"]
    assert len(revoke_calls) == 1
    assert revoke_calls[0][-1] == RULE_ID
    assert "sgr-070a3d2767818f890" not in revoke_calls[0]


def test_recovery_removes_expired_owned_rule_even_if_creation_response_was_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = managed_rule(expiry=900, cidr="0.0.0.0/0")
    calls: list[tuple[str, ...]] = []

    def aws_call(*args: str) -> dict[str, object]:
        calls.append(args)
        if args[0] == "describe-security-group-rules":
            if any(arg.startswith("Name=tag:") for arg in args):
                return {"SecurityGroupRules": [malformed]}
            return {"SecurityGroupRules": [malformed] if len(calls) == 2 else []}
        return {}

    monkeypatch.setattr(ssh_access, "_aws_json", aws_call)
    assert ssh_access.recover_expired(now=1000) == 1
    revoke = next(call for call in calls if call[0] == "revoke-security-group-ingress")
    assert revoke[-1] == RULE_ID


def test_failed_cleanup_can_be_recovered_after_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = managed_rule(expiry=900)
    calls: list[tuple[str, ...]] = []
    fail_first_revoke = True
    successful_revoke = False

    def aws_call(*args: str) -> dict[str, object]:
        nonlocal fail_first_revoke, successful_revoke
        calls.append(args)
        if args[0] == "describe-security-group-rules":
            if any(arg.startswith("Name=tag:") for arg in args):
                return {"SecurityGroupRules": [stale]}
            # The rule is present until a later exact-ID revoke succeeds.
            return {"SecurityGroupRules": [] if successful_revoke else [stale]}
        if args[0] == "revoke-security-group-ingress":
                if fail_first_revoke:
                    fail_first_revoke = False
                    raise ssh_access.SafeFailure("AWS security-group operation failed.")
                successful_revoke = True
        return {}

    monkeypatch.setattr(ssh_access, "_aws_json", aws_call)
    with pytest.raises(ssh_access.SafeFailure):
        ssh_access.revoke(RULE_ID, "12345", "1")
    # The scheduled expiry reaper retries the same exact managed ID later.
    assert ssh_access.recover_expired(now=1000) == 1
    assert all(call[-1] == RULE_ID for call in calls if call[0] == "revoke-security-group-ingress")


def test_unique_run_ids_keep_concurrent_runs_from_reclaiming_each_others_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, ...]] = []

    def aws_call(*args: str) -> dict[str, object]:
        captured.append(args)
        ip_permissions = json.loads(args[4])
        run_key = ip_permissions[0]["IpRanges"][0]["Description"].split(":", 1)[1]
        run_id, attempt = run_key.split("-")
        rule_id = f"sgr-{int(run_id):016x}{int(attempt):x}"
        expiry = json.loads(args[6])[0]["Tags"][2]["Value"]
        return {"SecurityGroupRules": [managed_rule(rule_id=rule_id, run_key=run_key, expiry=int(expiry))]}

    monkeypatch.setattr(ssh_access, "_aws_json", aws_call)
    monkeypatch.setattr(ssh_access, "get_runner_ipv4", lambda: RUNNER_IP)
    monkeypatch.setattr(ssh_access, "_write_output", lambda *_: None)
    first = ssh_access.authorize("12345", "1", now=100)
    second = ssh_access.authorize("12346", "1", now=100)
    assert first != second
    assert json.loads(captured[0][4])[0]["IpRanges"][0]["Description"] != json.loads(captured[1][4])[0]["IpRanges"][0]["Description"]


def test_workflow_limits_aws_oidc_to_deployment_jobs_and_has_recovery() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow_path = Path(
        os.environ.get("CODELENS_WORKFLOW_FILE", root / ".github/workflows/production.yml")
    )
    workflow = workflow_path.read_text()
    assert "aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd" in workflow
    assert "schedule:" in workflow and "recover_ssh:" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "if: always()" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    test_job = workflow.split("  test:\n", 1)[1].split("\n  image_test:", 1)[0]
    assert "id-token: write" not in test_job
    image_test_job = workflow.split("  image_test:", 1)[1].split("\n  publish:", 1)[0]
    assert "id-token: write" not in image_test_job
    publish_job = workflow.split("  publish:\n", 1)[1].split("\n  verify_existing:", 1)[0]
    assert "id-token: write" not in publish_job
    verify_job = workflow.split("  verify_existing:\n", 1)[1].split("\n  deploy:", 1)[0]
    assert "id-token: write" not in verify_job
    deploy_job = workflow.split("  deploy:\n", 1)[1].split("\n  rollback:", 1)[0]
    assert "github.event_name == 'pull_request'" not in deploy_job
    assert "github.ref == 'refs/heads/main'" in deploy_job
    assert deploy_job.count("if: always()") == 2
    rollback_job = workflow.split("  rollback:\n", 1)[1].split("\n  recover_ssh:", 1)[0]
    assert "github.event_name == 'workflow_dispatch'" in rollback_job
    assert "if: always()" in rollback_job
    assert "recover-expired" in workflow
    assert "deploy_existing" in workflow
    for job_name, next_job in (("deploy", "rollback"), ("rollback", "recover_ssh"), ("recover_ssh", None)):
        job = workflow.split(f"  {job_name}:\n", 1)[1]
        if next_job:
            job = job.split(f"\n  {next_job}:\n", 1)[0]
        assert "id-token: write" in job
        assert "role-to-assume: ${{ vars.CODELENS_AWS_ROLE_ARN }}" in job
        assert "aws-region: eu-north-1" in job
        assert "audience: sts.amazonaws.com" in job
    for job_name, next_job in (("deploy", "rollback"), ("rollback", "recover_ssh")):
        job = workflow.split(f"  {job_name}:\n", 1)[1].split(f"\n  {next_job}:\n", 1)[0]
        assert job.index("Authorize this runner's temporary SSH rule") < job.index("scp ")
    recovery_job = workflow.split("  recover_ssh:\n", 1)[1]
    assert "secrets." not in recovery_job
    assert "ssh " not in recovery_job and "scp " not in recovery_job


def test_documented_iam_policy_matches_helper_aws_calls() -> None:
    root = Path(__file__).resolve().parents[1]
    documentation_path = Path(
        os.environ.get("CODELENS_DEPLOYMENT_DOC", root / "docs/ghcr-deployment.md")
    )
    documentation = documentation_path.read_text()
    policy_section = documentation.split("### IAM permissions policy", 1)[1].split(
        "AWS authorizes ingress", 1
    )[0]
    match = re.search(r"(?ms)^    (\{\n.*?^    \})\s*$", policy_section)
    assert match is not None
    policy = json.loads(textwrap.dedent(match.group(1)))
    statements = policy["Statement"]
    assert [statement["Action"] for statement in statements] == [
        "ec2:DescribeSecurityGroupRules",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:CreateTags",
        "ec2:RevokeSecurityGroupIngress",
    ]

    group_arn = "arn:aws:ec2:eu-north-1:510155707736:security-group/sg-05c1462a133b8d00f"
    rule_arn = "arn:aws:ec2:eu-north-1:510155707736:security-group-rule/*"
    describe, authorize_group, authorize_rule, create_tags, revoke = statements
    assert describe["Resource"] == "*"
    assert describe["Condition"]["StringEquals"]["ec2:Region"] == "eu-north-1"
    assert authorize_group["Resource"] == group_arn
    assert authorize_group["Condition"]["StringEquals"]["ec2:SecurityGroupID"] == "sg-05c1462a133b8d00f"
    assert authorize_rule["Resource"] == rule_arn
    assert "ec2:SecurityGroupID" not in authorize_rule["Condition"]["StringEquals"]
    assert authorize_rule["Condition"]["StringEquals"]["aws:RequestTag/CodeLensManagedBy"] == "CodeLensGitHubActions"
    assert create_tags["Resource"] == rule_arn
    assert create_tags["Condition"]["StringEquals"]["ec2:CreateAction"] == "AuthorizeSecurityGroupIngress"
    assert revoke["Resource"] == group_arn
    assert revoke["Condition"]["StringEquals"]["ec2:SecurityGroupID"] == "sg-05c1462a133b8d00f"

    helper = (root / "scripts/manage_temporary_ssh.py").read_text()
    assert 'AWS_REGION = "eu-north-1"' in helper
    assert '"--region", AWS_REGION' in helper
    assert helper.count("subprocess.run(") == 1
    assert helper.count("_aws_json(") >= 5
    assert '"--tag-specifications"' in helper
    assert '"revoke-security-group-ingress"' in helper


def test_runner_ip_or_aws_failure_never_echoes_exception_text(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        ssh_access,
        "get_runner_ipv4",
        lambda: (_ for _ in ()).throw(ssh_access.SafeFailure(SECRET)),
    )
    assert ssh_access.main(["authorize", "--run-id", "12345", "--attempt", "1"]) == 1
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
