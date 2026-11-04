"""Select review findings that GitHub can anchor to added diff lines."""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Set

import requests

from graph.state import ReviewComment, SecurityIssue

INLINE_MARKER = "<!-- codeinspector-ai-inline -->"
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
logger = logging.getLogger(__name__)


def added_lines(patch: str) -> Set[int]:
    """Return new-file line numbers introduced by a valid unified diff patch."""
    result: Set[int] = set()
    line_number = None
    for line in (patch or "").splitlines():
        match = _HUNK.match(line)
        if match:
            line_number = int(match.group(1))
        elif line_number is not None:
            if line.startswith("+") and not line.startswith("+++"):
                result.add(line_number)
                line_number += 1
            elif line.startswith(" "):
                line_number += 1
            elif line.startswith("-") or line.startswith("\\"):
                pass
            else:
                line_number = None
    return result


def post_inline_comments(pr: Any, findings: List[Dict[str, Any]],
                         commit_sha: str, token: str) -> tuple[int, List[str]]:
    """Post new inline comments once per head commit, retaining summary fallback."""
    if not findings:
        return 0, []
    if not commit_sha:
        return 0, ["inline: missing PR head commit SHA"]
    try:
        existing = {
            (comment.path, getattr(comment, "line", None)
             if hasattr(comment, "line") else comment.raw_data.get("line"),
             comment.commit_id): comment
            for comment in pr.get_review_comments()
            if INLINE_MARKER in (comment.body or "")
        }
    except Exception as exc:
        return 0, [f"inline: could not check existing comments: {exc}"]

    posted = 0
    errors: List[str] = []
    for finding in findings:
        key = (finding["path"], finding["line"], commit_sha)
        previous = existing.get(key)
        if previous:
            if previous.body == finding["body"]:
                continue
            try:
                previous.edit(finding["body"])
            except Exception as exc:
                errors.append(f"inline: could not update {finding['path']}:{finding['line']}: {exc}")
            continue
        response = None
        try:
            response = requests.post(
                pr.url.rstrip("/") + "/comments",
                headers={"Accept": "application/vnd.github+json",
                         "Authorization": "Bearer " + token},
                json={**finding, "commit_id": commit_sha},
                timeout=15,
            )
            if not 200 <= response.status_code < 300:
                raise requests.HTTPError(f"HTTP {response.status_code}")
            posted += 1
            existing[key] = SimpleNamespace(body=finding["body"])
        except requests.RequestException as exc:
            logger.warning("Could not post inline finding at %s:%s: %s",
                           finding["path"], finding["line"], exc)
            errors.append(f"inline: {finding['path']}:{finding['line']}: {exc}")
            if getattr(response, "status_code", None) in (403, 429):
                break
    return posted, errors


def inline_findings(
    files: Iterable[Any],
    comments: Iterable[ReviewComment],
    issues: Iterable[SecurityIssue],
) -> List[Dict[str, Any]]:
    """Build one-line GitHub review comments for findings on added lines."""
    anchors = {file.filename: added_lines(file.patch) for file in files}
    grouped: Dict[tuple[str, int], List[str]] = {}
    for comment in comments:
        if comment.line is None or comment.line not in anchors.get(comment.file, set()):
            continue
        body = f"**{comment.severity.title()} — Code review:** {comment.message}"
        if comment.suggestion:
            body += f"\n\n**Suggestion:** {comment.suggestion}"
        grouped.setdefault((comment.file, comment.line), []).append(body)
    for issue in issues:
        if issue.line is None or issue.line not in anchors.get(issue.file, set()):
            continue
        body = (f"**{issue.severity.title()} — Security:** {issue.issue}"
                f"\n\n**Recommendation:** {issue.recommendation}")
        grouped.setdefault((issue.file, issue.line), []).append(body)
    return [
        {"path": path, "line": line, "side": "RIGHT",
         "body": INLINE_MARKER + "\n" + "\n\n---\n\n".join(bodies)}
        for (path, line), bodies in grouped.items()
    ]
