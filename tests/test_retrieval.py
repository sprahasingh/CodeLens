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
