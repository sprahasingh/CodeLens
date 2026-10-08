import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.tasks.pr_tasks as pr_tasks
from app.core.config import settings


class FakeClaimStore:
    def __init__(self):
        self.status = "queued"
        self.task_id = None
        self.lease_owner = None
        self.lease_expires_at = None
        self.attempts = 0
        self.review_payload = None
        self.lock = asyncio.Lock()


class FakeResult:
    def __init__(self, row=None, rowcount=0):
        self.row = row
        self.rowcount = rowcount

    def first(self):
        return self.row


class FakeSession:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self):
        return None

    async def execute(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect())).lower()
        params = statement.compile(dialect=postgresql.dialect()).params
        if getattr(statement, "_returning", None):
            values = {getattr(key, "key", key): getattr(value, "value", None)
                      for key, value in statement._values.items()}
            async with self.store.lock:
                now = datetime.utcnow()
                redelivered = " and true" in sql
                can_claim = (
                    self.store.status in ("queued", "expired", "abandoned")
                    or (self.store.status == "processing" and self.store.lease_expires_at <= now)
                    or (
                        redelivered
                        and self.store.status == "processing"
                        and self.store.task_id == values["task_id"]
                    )
                )
                if not can_claim or self.store.attempts >= settings.review_max_attempts:
                    return FakeResult()
                self.store.status = "processing"
                self.store.task_id = values["task_id"]
                self.store.lease_owner = values["lease_owner"]
                self.store.lease_expires_at = now + timedelta(seconds=settings.review_claim_lease_seconds)
                self.store.attempts += 1
                row = SimpleNamespace(attempts=self.store.attempts, review_payload=self.store.review_payload)
                return FakeResult(row=row, rowcount=1)

        # Fenced status/checkpoint updates only succeed when the current lease
        # owner is present in the statement parameters.
        values = {getattr(key, "key", key): getattr(value, "value", None)
                  for key, value in statement._values.items()}
        if self.store.lease_owner not in params.values():
            return FakeResult(rowcount=0)
        if "review_payload" in values:
            self.store.review_payload = values["review_payload"]
        if "status" in values:
            self.store.status = values["status"]
        return FakeResult(rowcount=1)


@pytest.mark.asyncio
async def test_claim_is_atomic_and_expired_claim_can_be_reacquired(monkeypatch):
    store = FakeClaimStore()
    monkeypatch.setattr(pr_tasks, "AsyncSessionLocal", lambda: FakeSession(store))

    first, second = await asyncio.gather(
        pr_tasks._acquire_review_claim("o", "r", 1, "sha", "task-a", "owner-a", False),
        pr_tasks._acquire_review_claim("o", "r", 1, "sha", "task-b", "owner-b", False),
    )
    assert sum(item is not None for item in (first, second)) == 1
    current_owner = store.lease_owner
    assert store.status == "processing"

    store.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    recovered = await pr_tasks._acquire_review_claim("o", "r", 1, "sha", "task-c", "owner-c", False)
    assert recovered["attempts"] == 2
    assert store.lease_owner == "owner-c"
    assert store.lease_owner != current_owner


@pytest.mark.asyncio
async def test_old_lease_owner_cannot_finish_or_overwrite_new_owner(monkeypatch):
    store = FakeClaimStore()
    store.status = "processing"
    store.lease_owner = "new-owner"
    monkeypatch.setattr(pr_tasks, "AsyncSessionLocal", lambda: FakeSession(store))

    finished = await pr_tasks._finish_review_claim(
        "o", "r", 1, "sha", "old-owner", "completed"
    )
    assert finished is False
    assert store.status == "processing"
    assert store.lease_owner == "new-owner"


@pytest.mark.asyncio
async def test_recovery_reuses_synthesis_checkpoint_after_posting_crash(monkeypatch):
    calls = {"retrieve": 0, "synthesize": 0, "post": 0}
    checkpoint = {}

    async def current_head(*_args):
        return {"head_sha": "head", "state": "open"}

    async def diff(*_args):
        return "+++ b/a.py\n@@ -1,2 +1,3 @@\n old\n+new code line that is long enough to be a hunk\n end"

    async def retrieve(hunks, *_args):
        calls["retrieve"] += 1
        return ([{"hunk": hunks[0][1], "filepath": hunks[0][0], "matches": [{"similarity": 0.8}]}], 1)

    async def synthesize(*_args, **_kwargs):
        calls["synthesize"] += 1
        return [{"concern": "review concern"}]

    async def save(payload):
        checkpoint.update(payload)

    async def post(*_args, **_kwargs):
        calls["post"] += 1
        if calls["post"] == 1:
            raise RuntimeError("simulated worker loss after checkpoint")
        return True

    monkeypatch.setattr(pr_tasks, "fetch_pr_state", current_head)
    monkeypatch.setattr(pr_tasks, "fetch_pr_diff", diff)
    monkeypatch.setattr(pr_tasks, "retrieve_for_hunks", retrieve)
    monkeypatch.setattr(pr_tasks, "synthesize_feedback", synthesize)
    monkeypatch.setattr(pr_tasks, "post_pr_comment", post)

    with pytest.raises(RuntimeError, match="simulated worker loss"):
        await pr_tasks._process_pr_async(
            1, "r", "o", "head", save_checkpoint=save
        )

    result = await pr_tasks._process_pr_async(
        1, "r", "o", "head", cached_review=checkpoint,
    )
    assert result["status"] == "complete"
    assert calls == {"retrieve": 1, "synthesize": 1, "post": 2}


@pytest.mark.asyncio
async def test_recovery_resumes_after_last_completed_synthesis_group(monkeypatch):
    synthesis_inputs = []
    checkpoint = {}
    fail_second_group_once = True

    async def current_head(*_args):
        return {"head_sha": "head", "state": "open"}

    async def diff(*_args):
        return (
            "+++ b/a.py\n@@ -1,2 +1,3 @@\n old\n+first new line long enough to be a hunk\n end\n"
            "+++ b/b.py\n@@ -1,2 +1,3 @@\n old\n+second new line long enough to be a hunk\n end"
        )

    async def retrieve(hunks, *_args):
        return ([
            {"hunk": text, "filepath": path, "matches": [{"similarity": 0.8}]}
            for path, text in hunks
        ], len(hunks))

    async def synthesize(hunk, _matches, changed_context="", request_context=None):
        nonlocal fail_second_group_once
        synthesis_inputs.append(hunk)
        if "second new" in hunk and fail_second_group_once:
            fail_second_group_once = False
            raise RuntimeError("simulated crash during synthesis")
        return [{"concern": "finding " + ("first" if "first new" in hunk else "second")}]

    async def save(payload):
        checkpoint.clear()
        checkpoint.update(payload)

    async def post(*_args, **_kwargs):
        return True

    monkeypatch.setattr(pr_tasks, "fetch_pr_state", current_head)
    monkeypatch.setattr(pr_tasks, "fetch_pr_diff", diff)
    monkeypatch.setattr(pr_tasks, "retrieve_for_hunks", retrieve)
    monkeypatch.setattr(pr_tasks, "synthesize_feedback", synthesize)
    monkeypatch.setattr(pr_tasks, "post_pr_comment", post)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await pr_tasks._process_pr_async(1, "r", "o", "head", save_checkpoint=save)

    result = await pr_tasks._process_pr_async(
        1, "r", "o", "head", cached_review=checkpoint,
        save_checkpoint=save,
    )
    assert result["status"] == "complete"
    assert len(synthesis_inputs) == 3
    assert "finding first" in [item["concern"] for item in checkpoint["feedback"]]
    assert "finding second" in [item["concern"] for item in checkpoint["feedback"]]
