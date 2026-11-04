"""Inline comments must be anchored to actual added lines in the PR diff."""

from types import SimpleNamespace

from graph.state import ReviewComment, SecurityIssue
from app.inline import added_lines, inline_findings
from app.inline import post_inline_comments, INLINE_MARKER


def test_added_lines_across_hunks_and_context():
    patch = "@@ -2,2 +5,3 @@\n context\n+first\n-old\n+second\n@@ -20,1 +30,2 @@\n context\n+third\n"
    assert added_lines(patch) == {6, 7, 31}


def test_inline_findings_include_only_valid_added_lines():
    files = [SimpleNamespace(filename="a.py", patch="@@ -1,1 +1,2 @@\n same\n+bad()\n")]
    comments = [
        ReviewComment(file="a.py", line=2, message="Bug", suggestion="Fix it", severity="warning"),
        ReviewComment(file="a.py", line=1, message="Context", severity="info"),
        ReviewComment(file="missing.py", line=2, message="Wrong path", severity="info"),
    ]
    issues = [SecurityIssue(file="a.py", line=2, issue="Injection", recommendation="Validate", severity="high")]
    result = inline_findings(files, comments, issues)
    assert len(result) == 1
    assert all(item["path"] == "a.py" and item["line"] == 2 and item["side"] == "RIGHT" for item in result)
    assert "Fix it" in result[0]["body"]
    assert "Validate" in result[0]["body"]


def test_missing_patch_has_no_anchors():
    assert inline_findings([SimpleNamespace(filename="a.py", patch=None)],
                           [ReviewComment(file="a.py", line=1, message="Bug")], []) == []


def test_post_inline_comments_skips_existing_on_same_commit(monkeypatch, fake_pr):
    finding = {"path": "a.py", "line": 2, "side": "RIGHT", "body": INLINE_MARKER + "\nBug"}
    fake_pr.get_review_comments = lambda: [SimpleNamespace(
        path="a.py", line=2, commit_id="abc1234", body=finding["body"])]
    monkeypatch.setattr("app.inline.requests.post", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("duplicate")))
    assert post_inline_comments(fake_pr, [finding], "abc1234", "test-token") == (0, [])


def test_rerun_reads_line_from_pygithub_raw_data(monkeypatch, fake_pr):
    finding = {"path": "a.py", "line": 2, "side": "RIGHT", "body": INLINE_MARKER + "\nBug"}
    fake_pr.get_review_comments = lambda: [SimpleNamespace(
        path="a.py", commit_id="abc1234", body=finding["body"], raw_data={"line": 2})]
    monkeypatch.setattr("app.inline.requests.post", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("duplicate")))
    assert post_inline_comments(fake_pr, [finding], "abc1234", "test-token") == (0, [])


def test_post_inline_comments_edits_existing_when_findings_change(monkeypatch, fake_pr):
    edited = []
    old = SimpleNamespace(path="a.py", line=2, commit_id="abc1234",
                          body=INLINE_MARKER + "\nOld", edit=lambda body: edited.append(body))
    fake_pr.get_review_comments = lambda: [old]
    monkeypatch.setattr("app.inline.requests.post", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("duplicate")))
    finding = {"path": "a.py", "line": 2, "side": "RIGHT", "body": INLINE_MARKER + "\nNew"}
    assert post_inline_comments(fake_pr, [finding], "abc1234", "test-token") == (0, [])
    assert edited == [finding["body"]]


def test_post_inline_comments_uses_line_side_and_commit(monkeypatch, fake_pr):
    calls = []
    fake_pr.get_review_comments = lambda: []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=201, raise_for_status=lambda: None)

    monkeypatch.setattr("app.inline.requests.post", post)
    finding = {"path": "a.py", "line": 2, "side": "RIGHT", "body": INLINE_MARKER + "\nBug"}
    assert post_inline_comments(fake_pr, [finding], "abc1234", "test-token") == (1, [])
    assert calls[0][0].endswith("/pulls/7/comments")
    assert calls[0][1]["json"] == {**finding, "commit_id": "abc1234"}
    assert calls[0][1]["headers"]["Authorization"] == "Bearer test-token"


def test_rejected_anchor_reports_error_without_losing_other_findings(monkeypatch, fake_pr):
    fake_pr.get_review_comments = lambda: []
    calls = []

    def post(*_a, **_k):
        calls.append(1)
        return SimpleNamespace(status_code=422, text="invalid line")

    monkeypatch.setattr("app.inline.requests.post", post)
    finding = {"path": "a.py", "line": 2, "side": "RIGHT", "body": INLINE_MARKER + "\nBug"}
    count, errors = post_inline_comments(fake_pr, [finding], "abc1234", "test-token")
    assert count == 0 and len(errors) == 1 and "a.py:2" in errors[0]
