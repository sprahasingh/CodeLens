import app.services.synthesizer as synthesizer


SIMILAR_COMMENTS = [
    {"similarity": 0.8, "body": "Handle the None case here", "path": "a.py"},
    {"similarity": 0.6, "body": "Rename this variable", "path": "b.py"},
]


async def test_synthesize_feedback_returns_empty_list_when_no_similar_comments():
    result = await synthesizer.synthesize_feedback("some hunk", [])
    assert result == []


async def test_synthesize_feedback_drops_concerns_with_empty_concern_text(monkeypatch):
    async def fake_call_groq_json(prompt, model=None):
        return {
            "concerns": [
                {"concern": "", "source_indices": [0]},
                {"concern": "   ", "source_indices": [0]},
                {"concern": "Real issue found", "source_indices": [0]},
            ]
        }

    monkeypatch.setattr(synthesizer, "call_groq_json", fake_call_groq_json)

    result = await synthesizer.synthesize_feedback("some hunk", SIMILAR_COMMENTS)

    assert len(result) == 1
    assert result[0]["concern"] == "Real issue found"


async def test_synthesize_feedback_rejects_negative_and_out_of_range_source_indices(monkeypatch):
    async def fake_call_groq_json(prompt, model=None):
        return {
            "concerns": [
                {"concern": "Uses a negative index", "source_indices": [-1, 0, 99]},
            ]
        }

    monkeypatch.setattr(synthesizer, "call_groq_json", fake_call_groq_json)

    result = await synthesizer.synthesize_feedback("some hunk", SIMILAR_COMMENTS)

    assert len(result) == 1
    # only index 0 is valid; -1 (negative) and 99 (out of range) must be excluded
    assert result[0]["source_comments"] == [SIMILAR_COMMENTS[0]]


async def test_synthesize_feedback_returns_empty_list_when_groq_call_fails(monkeypatch):
    async def failing_call_groq_json(prompt, model=None):
        return None

    monkeypatch.setattr(synthesizer, "call_groq_json", failing_call_groq_json)

    result = await synthesizer.synthesize_feedback("some hunk", SIMILAR_COMMENTS)
    assert result == []
