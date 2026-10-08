import pytest

import app.tasks.pr_tasks as pr_tasks


@pytest.mark.asyncio
async def test_queued_review_for_old_head_is_skipped(monkeypatch):
    async def current_head(*args):
        return {"head_sha": "new-head", "state": "open"}

    async def should_not_fetch(*args):
        raise AssertionError("obsolete review should not fetch the diff")

    monkeypatch.setattr(pr_tasks, "fetch_pr_state", current_head)
    monkeypatch.setattr(pr_tasks, "fetch_pr_diff", should_not_fetch)
    result = await pr_tasks._process_pr_async(
        23, "repo", "owner", expected_head_sha="old-head"
    )
    assert result["status"] == "obsolete"


@pytest.mark.asyncio
async def test_groq_failure_posts_explicit_incomplete_result_and_finishes(monkeypatch):
    sha_calls = 0

    async def current_head(*args):
        nonlocal sha_calls
        sha_calls += 1
        return {"head_sha": "current-head", "state": "open"}

    async def diff(*args):
        return "+++ b/a.py\n@@ -1,2 +1,3 @@\n old\n+new code line that is long enough to be a hunk\n end"

    async def retrieve(hunks, *_args):
        return ([{"hunk": hunks[0][1], "filepath": hunks[0][0], "matches": [{"similarity": 0.8}]}], 1)

    async def fail_synthesis(*_args):
        raise pr_tasks.GroqSynthesisUnavailable("bounded retries exhausted")

    posted = []

    async def post(*args, **kwargs):
        posted.append((args, kwargs))
        return True

    monkeypatch.setattr(pr_tasks, "fetch_pr_state", current_head)
    monkeypatch.setattr(pr_tasks, "fetch_pr_diff", diff)
    monkeypatch.setattr(pr_tasks, "retrieve_for_hunks", retrieve)
    monkeypatch.setattr(pr_tasks, "synthesize_feedback", fail_synthesis)
    monkeypatch.setattr(pr_tasks, "post_pr_comment", post)

    result = await pr_tasks._process_pr_async(
        23, "repo", "owner", expected_head_sha="current-head"
    )

    assert result["status"] == "partial"
    assert result["comment_posted"] is True
    assert sha_calls == 2
    assert len(posted) == 1
    assert "does not mean that no issues exist" in posted[0][1]["analysis_note"]


@pytest.mark.asyncio
async def test_closed_pr_is_skipped_before_diff_or_retrieval(monkeypatch):
    async def closed(*_args):
        return {"head_sha": "head", "state": "closed"}

    async def should_not_fetch(*_args):
        raise AssertionError("closed PR should not be reviewed")

    monkeypatch.setattr(pr_tasks, "fetch_pr_state", closed)
    monkeypatch.setattr(pr_tasks, "fetch_pr_diff", should_not_fetch)
    released = []

    async def release(*args):
        released.append(args)

    monkeypatch.setattr(pr_tasks, "_release_closed_pr_claim", release)
    result = await pr_tasks._process_pr_async(23, "repo", "owner", expected_head_sha="head")
    assert result["status"] == "closed"
    assert released == [("owner", "repo", 23, "head")]


@pytest.mark.asyncio
async def test_failed_github_comment_is_not_reported_as_review_success(monkeypatch):
    calls = 0

    async def current_head(*_args):
        nonlocal calls
        calls += 1
        return {"head_sha": "head", "state": "open"}

    async def diff(*_args):
        return ""

    async def post(*_args, **_kwargs):
        return False

    monkeypatch.setattr(pr_tasks, "fetch_pr_state", current_head)
    monkeypatch.setattr(pr_tasks, "fetch_pr_diff", diff)
    monkeypatch.setattr(pr_tasks, "post_pr_comment", post)
    result = await pr_tasks._process_pr_async(23, "repo", "owner", expected_head_sha="head")
    assert result["status"] == "github_delivery_failed"
    assert calls == 1


def test_review_task_uses_bounded_retries_and_records_terminal_failure(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise RuntimeError("temporary GitHub failure")

    monkeypatch.setattr(pr_tasks, "_process_pr_async", fail)
    retry_delays = []

    class RetrySentinel(Exception):
        pass

    def capture_retry(**kwargs):
        retry_delays.append(kwargs["countdown"])
        raise RetrySentinel

    monkeypatch.setattr(pr_tasks.process_pr, "retry", capture_retry)
    pr_tasks.process_pr.push_request(retries=0)
    try:
        with pytest.raises(RetrySentinel):
            pr_tasks.process_pr.run(23, "repo", "owner")
    finally:
        pr_tasks.process_pr.pop_request()
    assert retry_delays == [30]

    terminal_records = []

    async def record_failure(owner, repo, pr_number, error, attempts):
        terminal_records.append((owner, repo, pr_number, error, attempts))

    monkeypatch.setattr(pr_tasks, "_record_failed_pr", record_failure)
    pr_tasks.process_pr.push_request(retries=pr_tasks.process_pr.max_retries)
    try:
        with pytest.raises(RuntimeError, match="temporary GitHub failure"):
            pr_tasks.process_pr.run(23, "repo", "owner")
    finally:
        pr_tasks.process_pr.pop_request()
    assert terminal_records[0][-1] == pr_tasks.process_pr.max_retries + 1
