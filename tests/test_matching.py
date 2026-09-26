import math
from types import SimpleNamespace
from app.services.evaluation import cosine_similarity
from app.services.retrieval import deduplicate_by_body
from app.services.ingestion import _is_automated_review_content
from app.scripts.held_out_eval import (
    _even_subsample,
    split_tune_test,
    score_at_threshold,
    score_final,
    _precision_recall_f1,
)


def test_cosine_similarity_identical_vectors_is_one():
    v = [1.0, 2.0, 3.0]
    assert math.isclose(cosine_similarity(v, v), 1.0, rel_tol=1e-9)


def test_cosine_similarity_orthogonal_vectors_is_zero():
    assert math.isclose(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0, abs_tol=1e-9)


def test_cosine_similarity_zero_vector_does_not_divide_by_zero():
    assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0


def test_deduplicate_by_body_keeps_first_and_drops_case_insensitive_repeat():
    results = [
        {"body": "Add error handling here"},
        {"body": "ADD ERROR HANDLING HERE  "},
        {"body": "A different comment"},
    ]
    deduped = deduplicate_by_body(results)
    assert len(deduped) == 2
    assert deduped[0]["body"] == "Add error handling here"
    assert deduped[1]["body"] == "A different comment"


def test_is_automated_review_content_flags_confidence_marker():
    body = "⚠️ **HIGH** — *test_coverage*\n**Confidence:** 80%\n\nNew logic is untested."
    assert _is_automated_review_content(body) is True


def test_is_automated_review_content_allows_normal_human_comment():
    assert _is_automated_review_content("This looks good, thanks for the fix!") is False


def _row(id_, created_at=0):
    return SimpleNamespace(id=id_, comment_created_at=created_at)


def test_even_subsample_returns_all_rows_when_fewer_than_k():
    rows = [_row(i) for i in range(3)]
    assert _even_subsample(rows, 5) == rows


def test_even_subsample_returns_exactly_k_rows_spread_across_input():
    rows = [_row(i) for i in range(10)]
    sampled = _even_subsample(rows, 5)
    assert len(sampled) == 5
    # spread, not just the first 5
    assert sampled[-1].id > sampled[0].id


def test_split_tune_test_never_overlaps_and_is_balanced_across_repos():
    pools = {
        ("owner", "a"): [_row(f"a{i}") for i in range(20)],
        ("owner", "b"): [_row(f"b{i}") for i in range(4)],
    }
    tune, test = split_tune_test(pools, tune_size=4, test_size_max=10, min_test_size=2)

    tune_ids = {r.id for r in tune}
    test_ids = {r.id for r in test}
    assert tune_ids.isdisjoint(test_ids)

    # each repo contributes to tune (2 each for tune_size=4 over 2 repos)
    assert any(i.startswith("a") for i in tune_ids)
    assert any(i.startswith("b") for i in tune_ids)


def test_split_tune_test_raises_when_pool_too_small():
    pools = {("owner", "a"): [_row(0), _row(1)]}
    try:
        split_tune_test(pools, tune_size=10, test_size_max=10, min_test_size=10)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_precision_recall_f1_basic_case():
    result = _precision_recall_f1(tp=3, fp=1, fn=1)
    assert result["precision"] == 0.75
    assert result["recall"] == 0.75
    assert result["f1"] == 0.75


def test_precision_recall_f1_no_positive_predictions_returns_none_precision():
    result = _precision_recall_f1(tp=0, fp=0, fn=5)
    assert result["precision"] is None
    assert result["recall"] == 0.0
    assert result["f1"] is None


def test_score_at_threshold_counts_tp_when_similarity_clears_threshold():
    samples = [
        {"had_retrieval": True, "num_concerns": 2, "best_similarity": 0.8},
        {"had_retrieval": True, "num_concerns": 1, "best_similarity": 0.3},
        {"had_retrieval": False, "num_concerns": 0, "best_similarity": None},
    ]
    result = score_at_threshold(samples, threshold=0.5)
    assert result["tp"] == 1
    assert result["fp"] == 1 + 1  # (2-1) from the tp sample, 1 from the below-threshold sample
    assert result["fn"] == 2


def test_score_final_requires_both_floor_and_judge_confirmation():
    samples = [
        # clears floor but judge says no -> must NOT count as a true positive
        {"had_retrieval": True, "num_concerns": 1, "best_similarity": 0.8, "judge_confirmed": False},
        # clears floor and judge confirms -> true positive
        {"had_retrieval": True, "num_concerns": 2, "best_similarity": 0.9, "judge_confirmed": True},
        {"had_retrieval": False, "num_concerns": 0, "best_similarity": None, "judge_confirmed": None},
    ]
    result = score_final(samples, floor=0.45)
    assert result["tp"] == 1
    assert result["fn"] == 2
    assert result["fp"] == 1 + 1  # 1 from rejected sample, (2-1) from the confirmed sample
