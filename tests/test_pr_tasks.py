import app.tasks.pr_tasks as pr_tasks


async def test_fan_out_filters_before_cap_and_queues_one_task(monkeypatch):
    hunks = [
        ("package-lock.json", f"@@ -{index},1 +{index},1 @@\n+lock_{index}")
        for index in range(1, 6)
    ]
    hunks.extend(
        (f"src/file_{index}.py", f"@@ -1,1 +1,1 @@\n+source_{index}")
        for index in range(22)
    )
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

    assert result["total_hunks"] == 22
    assert len(queued) == 1
    assert queued[0][4:6] == (1, 1)
    assert queued[0][6] == 22
    assert len(queued[0][3]) == 10
    assert all(item["filepath"] != "package-lock.json" for item in queued[0][3])


async def test_fan_out_posts_status_when_only_filtered_files_changed(monkeypatch):
    posted_comments = []

    async def fake_fetch_pr_diff(owner, repo, pr_number):
        return "diff"

    async def fake_post(*args, **kwargs):
        posted_comments.append(kwargs)
        return True

    monkeypatch.setattr(pr_tasks, "fetch_pr_diff", fake_fetch_pr_diff)
    monkeypatch.setattr(
        pr_tasks,
        "split_diff_into_hunks",
        lambda diff: [("package-lock.json", "@@ -1,1 +1,2 @@\n+dependency")],
    )
    monkeypatch.setattr(pr_tasks, "post_pr_comment", fake_post)

    result = await pr_tasks._fan_out_async(17, "repo", "owner")

    assert result["status"] == "no_reviewable_hunks"
    assert result["comment_posted"] is True
    assert posted_comments[0]["review_skipped"] is True


async def test_process_batch_posts_status_if_synthesis_is_unavailable(monkeypatch):
    posted_comments = []

    async def fake_retrieve(hunks, owner, repo):
        return ([{"filepath": "src/a.py", "hunk": "code", "matches": [{"similarity": 0.8}]}], 1)

    async def fake_synthesize(groups):
        return None

    async def fake_post(*args, **kwargs):
        posted_comments.append((args, kwargs))
        return True

    monkeypatch.setattr(pr_tasks, "retrieve_for_hunks", fake_retrieve)
    monkeypatch.setattr(pr_tasks, "synthesize_feedback_for_hunks", fake_synthesize)
    monkeypatch.setattr(pr_tasks, "post_pr_comment", fake_post)

    result = await pr_tasks._process_batch_async(
        17, "repo", "owner", [{"filepath": "src/a.py", "hunk": "code"}], 1, 1, 1
    )

    assert result["status"] == "synthesis_unavailable"
    assert result["comment_posted"] is True
    assert posted_comments[0][1]["analysis_unavailable"] is True
