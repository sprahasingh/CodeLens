from app.services.github_comment import (
    _no_concerns_body,
    extract_relevant_lines,
    format_feedback_as_markdown,
)


def test_extract_relevant_lines_keeps_only_added_lines():
    hunk = (
        "@@ -1,3 +1,5 @@\n"
        "+import os\n"
        "+user = get_user(user_id)\n"
        "+return user.email\n"
        " unchanged_line\n"
        "-removed_line\n"
    )
    result = extract_relevant_lines(hunk)
    assert "import os" not in result  # import lines are filtered out
    assert "user = get_user(user_id)" in result
    assert "return user.email" in result
    assert "unchanged_line" not in result
    assert "removed_line" not in result


def test_extract_relevant_lines_truncates_and_notes_remaining_count():
    hunk = "@@ -1,1 +1,10 @@\n" + "\n".join(f"+line_{i}" for i in range(10))
    result = extract_relevant_lines(hunk, max_lines=3)
    assert "line_0" in result
    assert "line_2" in result
    assert "line_3" not in result
    assert "(7 more lines)" in result


def test_extract_relevant_lines_returns_empty_string_when_nothing_relevant():
    hunk = "@@ -1,2 +1,2 @@\n+import sys\n+from foo import bar\n"
    assert extract_relevant_lines(hunk) == ""


def test_format_feedback_as_markdown_empty_feedback_returns_empty_string():
    assert format_feedback_as_markdown([]) == ""


def test_format_feedback_as_markdown_includes_concern_confidence_and_provenance_link():
    feedback = [
        {
            "concern": "Missing None check",
            "confidence": 0.85,
            "suggested_check": "Verify None is handled",
            "evidence": "Past reviewers flagged this pattern",
            "is_inference": False,
            "source_comments": [
                {
                    "path": "app/foo.py",
                    "repo_owner": "org",
                    "repo_name": "repo",
                    "line": 12,
                    "diff_hunk": "@@ -1,1 +1,2 @@\n+x = get()\n",
                    "body": "This can return None",
                    "similarity": 0.72,
                    "html_url": "https://github.com/org/repo/pull/1#discussion_r1",
                    "triggered_by_hunk": "@@ -1,1 +1,2 @@\n+y = get_new()\n+return y.value\n",
                }
            ],
        }
    ]
    markdown = format_feedback_as_markdown(feedback)
    assert "Missing None check" in markdown
    assert "85%" in markdown
    assert "app/foo.py" in markdown
    assert "`org/repo`" in markdown
    assert "72%" in markdown
    assert "[view](https://github.com/org/repo/pull/1#discussion_r1)" in markdown
    assert "*(inferred)*" not in markdown


def test_format_feedback_as_markdown_tags_inferred_concerns():
    feedback = [{"concern": "Guessed issue", "confidence": 0.5, "is_inference": True, "source_comments": []}]
    markdown = format_feedback_as_markdown(feedback)
    assert "Suggested checks from historical patterns" in markdown
    assert "speculative checks, not asserted defects" in markdown


def test_cold_start_fallback_is_visible_and_explicit_about_history():
    body = _no_concerns_body(
        similar_count=0,
        outcome="general_analysis_only",
        review_summary="**Changed areas (1 files; +1/-0 lines):**\n- `src/a.py` (+1/-0)",
    )
    assert "General analysis only" in body
    assert "No relevant indexed historical comments" in body
    assert "not historical evidence" in body
    assert "src/a.py" in body
