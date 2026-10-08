from app.services.retrieval import split_diff_into_hunks, extract_starting_line

SAMPLE_DIFF = """diff --git a/app/foo.py b/app/foo.py
index abc123..def456 100644
--- a/app/foo.py
+++ b/app/foo.py
@@ -10,6 +10,9 @@ def get_user(user_id):
     if not user_id:
         raise ValueError("missing user_id")
+    user = db.query(User).filter_by(id=user_id).first()
+    return user.email
     return None
@@ -40,3 +43,4 @@ def other_function():
     pass
+    # trailing comment
"""


def test_split_diff_into_hunks_returns_each_hunk_separately():
    hunks = split_diff_into_hunks(SAMPLE_DIFF)
    assert len(hunks) == 2
    assert hunks[0][1].startswith("@@ -10,6 +10,9 @@")
    assert hunks[1][1].startswith("@@ -40,3 +43,4 @@")


def test_split_diff_into_hunks_drops_hunks_below_length_floor():
    tiny_diff = "@@ -1,1 +1,1 @@\n+x\n"
    assert split_diff_into_hunks(tiny_diff) == []


def test_split_diff_into_hunks_empty_diff_returns_empty_list():
    assert split_diff_into_hunks("") == []


def test_extract_starting_line_parses_new_file_line_number():
    hunk = "@@ -10,6 +25,9 @@ def get_user(user_id):"
    assert extract_starting_line(hunk) == 25


def test_extract_starting_line_handles_single_line_hunk_header():
    hunk = "@@ -1 +7 @@ def foo():"
    assert extract_starting_line(hunk) == 7


def test_extract_starting_line_returns_none_when_no_header_present():
    assert extract_starting_line("just some code\nno header here") is None
