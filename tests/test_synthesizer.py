import pytest

import app.services.synthesizer as synthesizer


SIMILAR_COMMENTS = [
    {"similarity": 0.8, "body": "Handle the None case here", "path": "a.py"},
    {"similarity": 0.6, "body": "Rename this variable", "path": "b.py"},
]


async def test_synthesize_feedback_returns_empty_list_when_no_similar_comments():
    result = await synthesizer.synthesize_feedback("some hunk", [])
    assert result == []


async def test_synthesize_feedback_drops_concerns_with_empty_concern_text(monkeypatch):
    async def fake_call_groq_json(prompt, model=None, log_context=None):
        return {
            "concerns": [
                {"concern": "", "source_indices": [0]},
                {"concern": "   ", "source_indices": [0]},
                {
                    "concern": "Real issue found",
                    "evidence": "The cited past review describes this failure mode.",
                    "source_indices": [0],
                },
            ]
        }

    monkeypatch.setattr(synthesizer, "call_groq_json", fake_call_groq_json)

    result = await synthesizer.synthesize_feedback("some hunk", SIMILAR_COMMENTS)

    assert len(result) == 1
    assert result[0]["concern"] == "Real issue found"


async def test_synthesize_feedback_rejects_negative_and_out_of_range_source_indices(monkeypatch):
    async def fake_call_groq_json(prompt, model=None, log_context=None):
        return {
            "concerns": [
                {
                    "concern": "Uses a negative index",
                    "evidence": "A past review raised this same concern.",
                    "source_indices": [-1, 0, 99],
                },
            ]
        }

    monkeypatch.setattr(synthesizer, "call_groq_json", fake_call_groq_json)

    result = await synthesizer.synthesize_feedback("some hunk", SIMILAR_COMMENTS)

    assert len(result) == 1
    # only index 0 is valid; -1 (negative) and 99 (out of range) must be excluded
    assert result[0]["source_comments"] == [SIMILAR_COMMENTS[0]]


async def test_synthesize_feedback_surfaces_groq_failure(monkeypatch):
    async def failing_call_groq_json(prompt, model=None, log_context=None):
        return None

    monkeypatch.setattr(synthesizer, "call_groq_json", failing_call_groq_json)

    with pytest.raises(synthesizer.GroqSynthesisUnavailable):
        await synthesizer.synthesize_feedback("some hunk", SIMILAR_COMMENTS)


async def test_prompt_evidence_budget_keeps_relevance_order_and_bounds_context(monkeypatch):
    captured = {}

    async def fake_call_groq_json(prompt, model=None, log_context=None):
        captured["prompt"] = prompt
        return {"concerns": []}

    monkeypatch.setattr(synthesizer, "call_groq_json", fake_call_groq_json)
    monkeypatch.setattr(synthesizer.settings, "groq_max_evidence_comments_per_group", 1)
    monkeypatch.setattr(synthesizer.settings, "groq_max_evidence_chars_per_group", 300)
    monkeypatch.setattr(synthesizer.settings, "groq_max_changed_context_chars", 40)
    comments = [
        {"similarity": .99, "body": "TOP_RELEVANT evidence " + "x" * 500, "path": "top.py"},
        {"similarity": .1, "body": "LOW_RELEVANCE_SENTINEL", "path": "low.py"},
    ]
    await synthesizer.synthesize_feedback("CRITICAL_HUNK", comments, "CONTEXT_SENTINEL" * 20)

    prompt = captured["prompt"]
    assert "CRITICAL_HUNK" in prompt
    assert "TOP_RELEVANT" in prompt
    assert "LOW_RELEVANCE_SENTINEL" not in prompt
    assert "CONTEXT_SENTINEL" in prompt
    assert "CONTEXT_SENTINEL" * 20 not in prompt
