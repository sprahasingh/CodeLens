import pytest
from app.core.config import settings

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

    async def fail_synthesis(*_args, **_kwargs):
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

    assert result["status"] == "external_failure"
    assert result["outcome"] == "external_failure"
    assert result["comment_posted"] is True
    assert sha_calls == 2
    assert len(posted) == 1
    assert "no no-findings conclusion" in posted[0][1]["analysis_note"]


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
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "history_state",
    ["zero_history", "unrelated_history", "below_similarity_floor"],
)
async def test_cold_start_posts_general_overview_without_groq_call(monkeypatch, history_state):
    async def current_head(*_args):
        return {"head_sha": "head", "state": "open"}

    async def diff(*_args):
        return "+++ b/src/new.py\n@@ -0,0 +1 @@\n+def added_function(): return 1\n"

    async def retrieve(_hunks, *_args):
        return ([], 1)

    async def should_not_synthesize(*_args, **_kwargs):
        raise AssertionError("cold-start review must not call Groq")

    posted = []

    async def post(*_args, **kwargs):
        posted.append(kwargs)
        return True

    monkeypatch.setattr(pr_tasks, "fetch_pr_state", current_head)
    monkeypatch.setattr(pr_tasks, "fetch_pr_diff", diff)
    monkeypatch.setattr(pr_tasks, "retrieve_for_hunks", retrieve)
    monkeypatch.setattr(pr_tasks, "synthesize_feedback", should_not_synthesize)
    monkeypatch.setattr(pr_tasks, "post_pr_comment", post)

    result = await pr_tasks._process_pr_async(23, "repo", "owner", expected_head_sha="head")

    assert result["status"] == "complete"
    assert result["outcome"] == "general_analysis_only"
    assert result["synthesis_calls"] == 0
    assert posted[0]["outcome"] == "general_analysis_only"
    assert "src/new.py" in posted[0]["review_summary"]


@pytest.mark.asyncio
async def test_retrieved_but_non_actionable_review_is_completed_visibly(monkeypatch):
    async def current_head(*_args):
        return {"head_sha": "head", "state": "open"}

    async def diff(*_args):
        return "+++ b/src/change.py\n@@ -1,1 +1,2 @@\n old\n+changed line with enough context\n"

    async def retrieve(hunks, *_args):
        path, hunk = hunks[0]
        return ([{
            "hunk": hunk, "filepath": path,
            "matches": [{"similarity": 0.72, "path": "src/old.py", "body": "historical concern"}],
        }], 1)

    async def synthesize(*_args, **_kwargs):
        return []

    posted = []

    async def post(*_args, **kwargs):
        posted.append(kwargs)
        return True

    monkeypatch.setattr(pr_tasks, "fetch_pr_state", current_head)
    monkeypatch.setattr(pr_tasks, "fetch_pr_diff", diff)
    monkeypatch.setattr(pr_tasks, "retrieve_for_hunks", retrieve)
    monkeypatch.setattr(pr_tasks, "synthesize_feedback", synthesize)
    monkeypatch.setattr(pr_tasks, "post_pr_comment", post)

    result = await pr_tasks._process_pr_async(23, "repo", "owner", expected_head_sha="head")

    assert result["status"] == "complete"
    assert result["outcome"] == "completed_no_actionable_findings"
    assert posted[0]["outcome"] == "completed_no_actionable_findings"


@pytest.mark.asyncio
async def test_strong_supported_finding_survives_to_visible_review(monkeypatch):
    async def current_head(*_args):
        return {"head_sha": "head", "state": "open"}

    async def diff(*_args):
        return "+++ b/src/change.py\n@@ -1,1 +1,2 @@\n old\n+changed line with sufficient review context\n"

    async def retrieve(hunks, *_args):
        path, hunk = hunks[0]
        return ([{
            "hunk": hunk, "filepath": path,
            "matches": [{
                "similarity": 0.91, "path": "src/old.py", "body": "Handle missing return values",
                "html_url": "https://example.test/review/1", "repo_owner": "org", "repo_name": "repo",
            }],
        }], 1)

    async def synthesize(_hunk, matches, **_kwargs):
        return [{
            "concern": "The new call can return None before the value is accessed.",
            "evidence": "The cited review flags this exact unchecked return behavior.",
            "suggested_check": "Handle the None return before dereferencing the result.",
            "confidence": 0.84,
            "is_inference": False,
            "source_comments": matches,
        }]

    posted = []

    async def post(_owner, _repo, _pr, feedback, **kwargs):
        posted.append((feedback, kwargs))
        return True

    monkeypatch.setattr(pr_tasks, "fetch_pr_state", current_head)
    monkeypatch.setattr(pr_tasks, "fetch_pr_diff", diff)
    monkeypatch.setattr(pr_tasks, "retrieve_for_hunks", retrieve)
    monkeypatch.setattr(pr_tasks, "synthesize_feedback", synthesize)
    monkeypatch.setattr(pr_tasks, "post_pr_comment", post)

    result = await pr_tasks._process_pr_async(23, "repo", "owner", expected_head_sha="head")

    assert result["status"] == "complete"
    assert result["outcome"] == "completed_with_findings"
    assert result["concerns_identified"] == 1
    assert len(posted[0][0]) == 1


@pytest.mark.asyncio
async def test_groq_request_limit_posts_partial_outcome_and_stops_synthesis(monkeypatch):
    async def current_head(*_args):
        return {"head_sha": "head", "state": "open"}

    async def diff(*_args):
        return (
            "+++ b/a.py\n@@ -1,1 +1,2 @@\n old\n+first change with enough context\n"
            "+++ b/b.py\n@@ -1,1 +1,2 @@\n old\n+second distinct change with enough context\n"
        )

    async def retrieve(hunks, *_args):
        return ([{
            "hunk": hunk, "filepath": path,
            "matches": [{"similarity": 0.7, "path": "past.py", "body": path}],
        } for path, hunk in hunks], len(hunks))

    synth_calls = []

    async def synthesize(*args, **kwargs):
        synth_calls.append((args, kwargs))
        return []

    posted = []

    async def post(*_args, **kwargs):
        posted.append(kwargs)
        return True

    monkeypatch.setattr(settings, "groq_max_calls_per_pr", 1)
    monkeypatch.setattr(pr_tasks, "fetch_pr_state", current_head)
    monkeypatch.setattr(pr_tasks, "fetch_pr_diff", diff)
    monkeypatch.setattr(pr_tasks, "retrieve_for_hunks", retrieve)
    monkeypatch.setattr(pr_tasks, "synthesize_feedback", synthesize)
    monkeypatch.setattr(pr_tasks, "post_pr_comment", post)

    result = await pr_tasks._process_pr_async(23, "repo", "owner", expected_head_sha="head")

    assert result["status"] == "partial"
    assert result["outcome"] == "partial_request_limit"
    assert result["synthesis_calls"] == 1
    assert len(synth_calls) == 1
    assert posted[0]["partial"] is True
    assert "request limit" in posted[0]["analysis_note"]


def test_review_task_uses_bounded_retries_and_records_terminal_failure(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise RuntimeError("temporary GitHub failure")

    monkeypatch.setattr(pr_tasks, "_process_pr_async", fail)
    claim_attempts = iter([1, 3])

    async def acquire(*_args):
        return {"attempts": next(claim_attempts), "review_payload": None}

    async def release(*_args):
        return None

    async def current_head(*_args):
        return {"head_sha": "sha", "state": "open"}

    terminal_records = []

    async def record_failure(*args):
        terminal_records.append(args)

    monkeypatch.setattr(pr_tasks, "_acquire_review_claim", acquire)
    monkeypatch.setattr(pr_tasks, "_release_claim_for_retry", release)
    monkeypatch.setattr(pr_tasks, "fetch_pr_state", current_head)
    monkeypatch.setattr(pr_tasks, "_record_claim_failure", record_failure)
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
            pr_tasks.process_pr.run(23, "repo", "owner", expected_head_sha="sha")
    finally:
        pr_tasks.process_pr.pop_request()
    assert retry_delays == [30]

    pr_tasks.process_pr.push_request(retries=pr_tasks.process_pr.max_retries)
    try:
        with pytest.raises(RuntimeError, match="temporary GitHub failure"):
            pr_tasks.process_pr.run(23, "repo", "owner", expected_head_sha="sha")
    finally:
        pr_tasks.process_pr.pop_request()
    assert terminal_records[0][-2:] == ("temporary GitHub failure", 3)
