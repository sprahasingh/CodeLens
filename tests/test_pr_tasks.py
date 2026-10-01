import app.tasks.pr_tasks as pr_tasks


async def test_fan_out_filters_before_cap_and_queues_one_task(monkeypatch):
    hunks = [("package-lock.json", "lock hunk " * 5) for _ in range(5)]
    hunks.extend((f"src/file_{index}.py", f"source hunk {index} " * 5) for index in range(22))
    queued = []

    async def fake_fetch_pr_diff(owner, repo, pr_number):
        return "diff"

    class FakeTask:
        @staticmethod
        def delay(*args):
            queued.append(args)

    monkeypatch.setattr(pr_tasks, "fetch_pr_diff", fake_fetch_pr_diff)
    monkeypatch.setattr(pr_tasks, "split_diff_into_hunks", lambda diff: hunks)
    monkeypatch.setattr(pr_tasks, "process_pr_batch", FakeTask)

    result = await pr_tasks._fan_out_async(17, "repo", "owner")

    assert result["total_hunks"] == 27
    assert len(queued) == 1
    assert queued[0][4:6] == (1, 1)
    assert queued[0][6] == 27
    assert len(queued[0][3]) == 20
    assert all(item["filepath"] != "package-lock.json" for item in queued[0][3])


async def test_process_batch_skips_comment_if_synthesis_is_unavailable(monkeypatch):
    async def fake_retrieve(hunks, owner, repo):
        return ([{"filepath": "src/a.py", "hunk": "code", "matches": [{"similarity": 0.8}]}], 1)

    async def fake_synthesize(groups):
        return None

    async def unexpected_post(*args, **kwargs):
        raise AssertionError("must not post an empty review after synthesis failure")

    monkeypatch.setattr(pr_tasks, "retrieve_for_hunks", fake_retrieve)
    monkeypatch.setattr(pr_tasks, "synthesize_feedback_for_hunks", fake_synthesize)
    monkeypatch.setattr(pr_tasks, "post_pr_comment", unexpected_post)

    result = await pr_tasks._process_batch_async(
        17, "repo", "owner", [{"filepath": "src/a.py", "hunk": "code"}], 1, 1, 1
    )

    assert result["status"] == "synthesis_unavailable"
    assert result["comment_posted"] is False
