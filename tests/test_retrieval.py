import app.services.retrieval as retrieval


async def test_retrieve_for_hunks_batches_embeddings_and_keeps_top_three(monkeypatch):
    hunks = [
        ("src/a.py", "hunk a" * 10),
        ("src/b.py", "hunk b" * 10),
        ("src/c.py", "hunk c" * 10),
        ("src/d.py", "hunk d" * 10),
        ("package-lock.json", "lockfile hunk" * 10),
    ]
    embed_calls = []
    similarity_by_vector = {0: 0.61, 1: 0.91, 2: 0.78, 3: 0.88}

    def fake_embed_texts(texts):
        embed_calls.append(texts)
        return [[index] for index, _ in enumerate(texts)]

    async def fake_find_similar_comments(query_vector, repo_owner, repo_name, limit=5, hunk=""):
        similarity = similarity_by_vector[int(query_vector[0])]
        return [{
            "similarity": similarity,
            "body": f"comment {query_vector[0]}",
            "path": "src/previous.py",
        }]

    monkeypatch.setattr(retrieval, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(retrieval, "_find_similar_comments_by_vector", fake_find_similar_comments)

    selected, scanned = await retrieval.retrieve_for_hunks(hunks, "owner", "repo")

    assert len(embed_calls) == 1
    assert embed_calls[0] == [hunk for path, hunk in hunks if path != "package-lock.json"]
    assert scanned == 4
    assert [group["filepath"] for group in selected] == ["src/b.py", "src/d.py", "src/c.py"]


def test_prepare_review_hunks_splits_large_hunks_and_samples_late_changes():
    body = "\n".join(f"+changed_line_{index}" for index in range(5000))
    large_hunk = f"@@ -1,0 +1,5000 @@\n{body}"

    selected, total_windows = retrieval.prepare_review_hunks([("src/big.py", large_hunk)])

    assert total_windows > 10
    assert len(selected) == 10
    assert all(len(hunk.splitlines()) - 1 <= retrieval.MAX_WINDOW_LINES for _, hunk in selected)
    assert all(len(hunk) <= retrieval.MAX_WINDOW_CHARS for _, hunk in selected)
    assert retrieval.extract_starting_line(selected[-1][1]) > 4000


def test_prepare_review_hunks_distributes_candidates_across_files():
    hunks = [
        ("src/first.py", f"@@ -{index},1 +{index},1 @@\n+first_{index}")
        for index in range(1, 11)
    ]
    hunks.extend(
        ("src/second.py", f"@@ -{index},1 +{index},1 @@\n+second_{index}")
        for index in range(1, 11)
    )

    selected, total_windows = retrieval.prepare_review_hunks(hunks, limit=4)

    assert total_windows == 20
    assert len(selected) == 4
    assert [filepath for filepath, _ in selected].count("src/first.py") == 2
    assert [filepath for filepath, _ in selected].count("src/second.py") == 2
