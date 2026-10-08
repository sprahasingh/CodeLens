"""Small, deterministic safeguards for retrieval-grounded review output."""
import re
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from typing import Any


_MISSING_SYMBOL_WORDS = re.compile(
    r"\b(undefined|not defined|unimported|not imported|missing import|implicit.any)\b",
    re.IGNORECASE,
)
_TYPE_CLAIM_WORDS = re.compile(
    r"\b(implicit.any|type information|type annotation|explicit type)\b", re.IGNORECASE
)
_IDENTIFIER = re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b")
_TOKEN = re.compile(r"[a-z0-9_]+")
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "is", "it", "may", "might", "of", "on", "or", "that", "the",
    "this", "to", "with", "without", "which", "could", "should", "would",
    "issue", "check", "verify", "ensure", "file", "code", "concern",
}


def changed_context_by_file(hunks: list[tuple[str, str]]) -> dict[str, str]:
    """Join all diff hunks for each file so synthesis sees imports and definitions."""
    result: dict[str, list[str]] = {}
    for path, hunk in hunks:
        result.setdefault(path, []).append(hunk)
    return {path: "\n...\n".join(parts) for path, parts in result.items()}


def summarize_diff(diff: str) -> dict[str, Any]:
    """Return a factual, model-free change overview for transparent fallbacks."""
    files: dict[str, dict[str, int]] = {}
    current_path = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            files.setdefault(current_path, {"added": 0, "removed": 0})
        elif current_path and line.startswith("+") and not line.startswith("+++"):
            files[current_path]["added"] += 1
        elif current_path and line.startswith("-") and not line.startswith("---"):
            files[current_path]["removed"] += 1

    changed_files = [
        {"path": path, **counts}
        for path, counts in files.items()
    ]
    test_files = [
        item["path"] for item in changed_files
        if re.search(r"(^|/)(tests?|specs?)(/|\.)|\.(test|spec)\.", item["path"], re.I)
    ]
    return {
        "files": changed_files,
        "file_count": len(changed_files),
        "added": sum(item["added"] for item in changed_files),
        "removed": sum(item["removed"] for item in changed_files),
        "test_files": test_files,
    }


def missing_symbol_claim_contradicted(item: dict, contexts: dict[str, str]) -> bool:
    """Reject a missing-import/type claim only when the changed PR proves it false.

    This is deliberately narrow: it never decides whether behavior is correct,
    and it does not filter general findings or claims without code-symbol context.
    """
    concern = item.get("concern", "")
    if not (_MISSING_SYMBOL_WORDS.search(concern) or _TYPE_CLAIM_WORDS.search(concern)):
        return False
    file_paths = {
        source.get("triggered_by_file")
        for source in item.get("source_comments", [])
        if source.get("triggered_by_file")
    }
    identifiers = set(_IDENTIFIER.findall(concern))
    if not file_paths or not identifiers:
        return False

    file_context = "\n".join(contexts.get(path, "") for path in file_paths)
    imported = set()
    import_blocks = re.findall(
        r"\bimport\b(?:(?!;).){0,1000}?\bfrom\b",
        file_context,
        flags=re.DOTALL,
    )
    for block in import_blocks:
        imported.update(_IDENTIFIER.findall(block))
    exported = set()
    for context in contexts.values():
        for line in context.splitlines():
            if re.search(r"\b(export|declare|const|let|var|type|interface|class|enum)\b", line):
                exported.update(_IDENTIFIER.findall(line))
    return bool(identifiers & imported & exported)


def remove_context_contradicted_findings(
    findings: list[dict], contexts: dict[str, str]
) -> tuple[list[dict], list[dict]]:
    kept, rejected = [], []
    for item in findings:
        (rejected if missing_symbol_claim_contradicted(item, contexts) else kept).append(item)
    return kept, rejected


def _issue_families(text: str) -> set[str]:
    lowered = text.lower()
    families = set()
    if re.search(r"undefined|not defined|unimported|not imported|missing import", lowered):
        families.add("missing_symbol")
    if re.search(r"implicit.any|implicit-any|type information|type annotation|explicit type|typing", lowered):
        families.add("type_information")
    return families


def _source_key(source: dict) -> tuple:
    return (
        source.get("html_url")
        or (
            source.get("repo_owner"), source.get("repo_name"),
            source.get("path"), source.get("body"),
        )
    )


def _duplicate_pair(left: dict, right: dict) -> bool:
    if bool(left.get("is_inference")) != bool(right.get("is_inference")):
        return False
    left_raw = left.get("concern", "").strip()
    right_raw = right.get("concern", "").strip()
    if not left_raw or not right_raw:
        return False
    left_text, right_text = left_raw.lower(), right_raw.lower()
    if left_text == right_text:
        return True

    left_ids, right_ids = set(_IDENTIFIER.findall(left_raw)), set(_IDENTIFIER.findall(right_raw))
    shared_ids = left_ids & right_ids
    left_families, right_families = _issue_families(left_text), _issue_families(right_text)
    # A shared identifier is only a clue: merge by it when both claims name
    # the same issue family. Distinct concerns about one symbol must survive.
    if shared_ids and left_families & right_families:
        return True

    left_tokens = {t for t in _TOKEN.findall(left_text) if t not in _STOP_WORDS}
    right_tokens = {t for t in _TOKEN.findall(right_text) if t not in _STOP_WORDS}
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return overlap >= 0.82 or SequenceMatcher(None, left_text, right_text).ratio() >= 0.88


def deduplicate_findings(findings: list[dict]) -> list[dict]:
    """Merge only near-identical claims; preserve evidence from each occurrence."""
    result: list[dict] = []
    for finding in findings:
        duplicate = next((old for old in result if _duplicate_pair(old, finding)), None)
        if duplicate is None:
            result.append(dict(finding))
            continue
        sources = duplicate.setdefault("source_comments", [])
        seen = {_source_key(source) for source in sources}
        for source in finding.get("source_comments", []):
            key = _source_key(source)
            if key not in seen:
                sources.append(source)
                seen.add(key)
        duplicate["confidence"] = max(
            float(duplicate.get("confidence", 0) or 0),
            float(finding.get("confidence", 0) or 0),
        )
    return result


def historical_candidate_rank(
    hunk: str,
    target_path: str,
    candidate: dict,
    repo_owner: str,
    repo_name: str,
) -> float:
    """A transparent, small tie-breaker for vector similarity candidates."""
    score = float(candidate.get("similarity", 0) or 0)
    target_ids = set(_IDENTIFIER.findall(hunk))
    historical_ids = set(_IDENTIFIER.findall(
        f"{candidate.get('diff_hunk', '')}\n{candidate.get('body', '')}"
    ))
    if target_ids & historical_ids:
        score += 0.08
    target_suffix = PurePosixPath(target_path).suffix.lower()
    source_suffix = PurePosixPath(candidate.get("path", "")).suffix.lower()
    if target_suffix and target_suffix == source_suffix:
        score += 0.025
    target_test = bool(re.search(r"(^|/)(tests?|specs?)(/|\.)|\.(test|spec)\.", target_path, re.I))
    source_test = bool(re.search(
        r"(^|/)(tests?|specs?)(/|\.)|\.(test|spec)\.",
        candidate.get("path", ""), re.I
    ))
    if target_test and source_test:
        score += 0.025
    if (candidate.get("repo_owner"), candidate.get("repo_name")) == (repo_owner, repo_name):
        score += 0.02
    return score


def cluster_repeated_candidate_groups(groups: list[dict]) -> list[dict]:
    """Synthesize repeated evidence/code-symbol clusters once, with all hunks visible."""
    clustered: list[dict] = []
    for group in groups:
        symbols = set(_IDENTIFIER.findall(group.get("hunk", "")))
        evidence = {_source_key(match) for match in group.get("matches", [])}
        duplicate = None
        if symbols and evidence:
            for candidate in clustered:
                if symbols & candidate["_symbols"] and evidence & candidate["_evidence"]:
                    duplicate = candidate
                    break
        if duplicate is None:
            item = dict(group)
            item["filepaths"] = [group["filepath"]]
            item["_symbols"] = symbols
            item["_evidence"] = evidence
            item["_member_count"] = 1
            clustered.append(item)
            continue

        duplicate["hunk"] += f"\n\n--- Related changed hunk: {group['filepath']} ---\n{group['hunk']}"
        if group["filepath"] not in duplicate["filepaths"]:
            duplicate["filepaths"].append(group["filepath"])
        duplicate["_symbols"].update(symbols)
        duplicate["_evidence"].update(evidence)
        duplicate["_member_count"] += 1
        duplicate.setdefault("_member_indices", []).extend(group.get("_member_indices", []))
        known_sources = {
            match.get("html_url") or (match.get("path"), match.get("body"))
            for match in duplicate["matches"]
        }
        for match in group.get("matches", []):
            key = _source_key(match)
            if key not in known_sources:
                duplicate["matches"].append(match)
                known_sources.add(key)
    return clustered


def format_change_overview(summary: dict) -> str:
    """Format a factual fallback overview; it does not make defect claims."""
    files = summary.get("files", [])
    if not files:
        return "The PR diff did not contain reviewable file changes."
    lines = [
        f"{item['path']} (+{item['added']}/-{item['removed']})"
        for item in files[:12]
    ]
    if len(files) > 12:
        lines.append(f"…and {len(files) - 12} more changed files")
    detail = [
        f"**Changed areas ({summary['file_count']} files; +{summary['added']}/-{summary['removed']} lines):**",
        *[f"- `{line}`" for line in lines],
    ]
    tests = summary.get("test_files", [])
    if tests:
        detail.append("**Test files changed:** " + ", ".join(f"`{path}`" for path in tests[:8]))
    else:
        detail.append(
            "**Validation:** No test file is present in this diff. Run the existing tests "
            "that cover the changed areas before merging."
        )
    return "\n".join(detail)
