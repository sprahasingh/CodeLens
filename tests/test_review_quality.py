from app.services.review_quality import (
    changed_context_by_file,
    cluster_repeated_candidate_groups,
    deduplicate_findings,
    format_change_overview,
    historical_candidate_rank,
    remove_context_contradicted_findings,
    summarize_diff,
)


REPEATED_CONCERNS = [
    "The identifier RESPONSIVE_SMOKE_VIEWPORTS may be undefined or not imported in this test file",
    "RESPONSIVE_SMOKE_VIEWPORTS is used without an explicit import or type, causing implicit-any",
    "RESPONSIVE_SMOKE_VIEWPORTS may be undefined or lack proper type information",
]


def test_same_pr_import_and_export_contradict_repeated_missing_symbol_claims():
    contexts = changed_context_by_file([
        ("e2e/tests/responsive-a.spec.ts", """@@ -1,1 +1,5 @@
+import {
+  RESPONSIVE_SMOKE_VIEWPORTS,
+} from "./support/responsive";
+for (const viewport of RESPONSIVE_SMOKE_VIEWPORTS) {}
"""),
        ("e2e/support/responsive.ts", """@@ -0,0 +1,2 @@
+export const RESPONSIVE_SMOKE_VIEWPORTS = [{ width: 320 }] as const;
"""),
    ])
    items = [
        {
            "concern": concern,
            "is_inference": True,
            "source_comments": [{"triggered_by_file": "e2e/tests/responsive-a.spec.ts"}],
        }
        for concern in REPEATED_CONCERNS
    ]

    kept, rejected = remove_context_contradicted_findings(items, contexts)

    assert kept == []
    assert len(rejected) == 3


def test_deduplicate_findings_merges_equivalent_claims_and_keeps_provenance():
    findings = [
        {
            "concern": concern,
            "is_inference": True,
            "confidence": confidence,
            "source_comments": [{"html_url": f"https://example.test/{i}", "body": "evidence"}],
        }
        for i, (concern, confidence) in enumerate(zip(REPEATED_CONCERNS, (0.73, 0.78, 0.75)))
    ]

    result = deduplicate_findings(findings)

    assert len(result) == 2
    missing_symbol = next(item for item in result if "undefined" in item["concern"])
    implicit_any = next(item for item in result if "implicit-any" in item["concern"])
    assert len(missing_symbol["source_comments"]) == 2
    assert missing_symbol["confidence"] == 0.75
    assert len(implicit_any["source_comments"]) == 1


def test_shared_identifier_does_not_merge_distinct_issue_families():
    findings = [
        {
            "concern": "RESPONSIVE_SMOKE_VIEWPORTS is not imported in the test file",
            "source_comments": [{"html_url": "https://example.test/missing"}],
        },
        {
            "concern": "RESPONSIVE_SMOKE_VIEWPORTS needs an explicit type annotation for the test helper",
            "source_comments": [{"html_url": "https://example.test/type"}],
        },
    ]

    result = deduplicate_findings(findings)

    assert len(result) == 2


def test_candidate_ranking_uses_path_kind_and_shared_symbols_as_tie_breakers():
    target = "RESPONSIVE_SMOKE_VIEWPORTS.filter(({ width }) => width < 1920)"
    exact_context = {
        "similarity": 0.72,
        "path": "e2e/tests/responsive-old.spec.ts",
        "diff_hunk": "RESPONSIVE_SMOKE_VIEWPORTS",
        "body": "Check responsive smoke viewports",
        "repo_owner": "other",
        "repo_name": "repo",
    }
    higher_raw_similarity_but_unrelated = {
        "similarity": 0.79,
        "path": "api/tsconfig.json",
        "diff_hunk": "compilerOptions strict false",
        "body": "Enable strict configuration",
        "repo_owner": "another",
        "repo_name": "repo",
    }

    assert historical_candidate_rank(
        target, "e2e/tests/responsive-new.spec.ts", exact_context, "owner", "repo"
    ) > historical_candidate_rank(
        target, "e2e/tests/responsive-new.spec.ts", higher_raw_similarity_but_unrelated,
        "owner", "repo",
    )


def test_repeated_symbol_groups_share_one_synthesis_candidate():
    source = {"html_url": "https://example.test/review/1", "path": "api/tsconfig.json", "body": "Typecheck tests"}
    groups = [
        {"filepath": f"e2e/tests/a{i}.spec.ts", "hunk": f"RESPONSIVE_SMOKE_VIEWPORTS at {i}", "matches": [dict(source)]}
        for i in range(3)
    ]
    groups.append({"filepath": "api/routes.ts", "hunk": "new request handler", "matches": [dict(source)]})

    result = cluster_repeated_candidate_groups(groups)

    assert len(result) == 2
    repeated = next(item for item in result if item["_member_count"] == 3)
    assert repeated["hunk"].count("RESPONSIVE_SMOKE_VIEWPORTS") == 3
    assert repeated["filepaths"] == ["e2e/tests/a0.spec.ts", "e2e/tests/a1.spec.ts", "e2e/tests/a2.spec.ts"]


def test_same_text_from_different_repositories_is_not_collapsed_as_one_source():
    groups = [
        {
            "filepath": "src/a.py", "hunk": "SHARED_SYMBOL changed one behavior",
            "matches": [{"repo_owner": "one", "repo_name": "repo", "path": "src/old.py", "body": "Review this symbol"}],
        },
        {
            "filepath": "src/b.py", "hunk": "SHARED_SYMBOL changed another behavior",
            "matches": [{"repo_owner": "two", "repo_name": "repo", "path": "src/old.py", "body": "Review this symbol"}],
        },
    ]

    result = cluster_repeated_candidate_groups(groups)

    assert len(result) == 2


def test_same_symbol_with_different_historical_evidence_keeps_groups_separate():
    groups = [
        {
            "filepath": "src/a.py", "hunk": "SHARED_SYMBOL handles empty input",
            "matches": [{"html_url": "https://example.test/review/empty", "body": "Handle empty input"}],
        },
        {
            "filepath": "src/b.py", "hunk": "SHARED_SYMBOL handles concurrent updates",
            "matches": [{"html_url": "https://example.test/review/race", "body": "Protect concurrent updates"}],
        },
    ]

    assert len(cluster_repeated_candidate_groups(groups)) == 2


def test_diff_summary_and_fallback_overview_are_factual():
    diff = """diff --git a/src/view.ts b/src/view.ts
--- a/src/view.ts
+++ b/src/view.ts
@@ -1,2 +1,3 @@
 old
+new
-removed
diff --git a/tests/view.test.ts b/tests/view.test.ts
--- /dev/null
+++ b/tests/view.test.ts
@@ -0,0 +1 @@
+test()
"""

    summary = summarize_diff(diff)
    overview = format_change_overview(summary)

    assert summary["file_count"] == 2
    assert summary["added"] == 2
    assert summary["removed"] == 1
    assert summary["test_files"] == ["tests/view.test.ts"]
    assert "not historical evidence" not in overview
    assert "tests/view.test.ts" in overview
