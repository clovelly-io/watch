"""GitHub's webhooks, checked and parsed at the door.

A delivery is accepted only with a valid ``X-Hub-Signature-256`` under the repository's
webhook secret and only for the repository its path names; it is then parsed into one of
the events below, and nothing else of the payload is kept.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

_SHA = re.compile(r"^[0-9a-f]{40}$")


class Rejected(ValueError):
    """A delivery the runner refuses: a bad signature, another repository, a malformed body."""


@dataclass(frozen=True)
class PullRequestMerged:
    """A pull request merged into the default branch: its monitors job is queued."""

    number: int
    merge_sha: str


@dataclass(frozen=True)
class PullRequestClosed:
    """Closed without merging: a monitor whose fix it was is muted."""

    number: int


@dataclass(frozen=True)
class IssueClosed:
    number: int


@dataclass(frozen=True)
class Ignored:
    """Anything else (an opened pull request, a merge into another branch, a ping)."""

    reason: str


Event = PullRequestMerged | PullRequestClosed | IssueClosed | Ignored


def sign(secret: bytes, body: bytes) -> str:
    """The ``X-Hub-Signature-256`` value GitHub sends for ``body``."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify(secret: bytes, body: bytes, signature: str | None) -> None:
    if not signature or not hmac.compare_digest(sign(secret, body), signature):
        raise Rejected("the signature does not match the repository's webhook secret")


def _number(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise Rejected("a number is not a positive whole number")
    return v


def parse(event: str, body: bytes, *, github: str) -> Event:
    """Parses a verified delivery of type ``event`` (the ``X-GitHub-Event`` header) for the
    repository ``github`` (``owner/name``)."""
    try:
        doc = json.loads(body)
    except ValueError:
        raise Rejected("the body is not JSON") from None
    if not isinstance(doc, dict):
        raise Rejected("the body is not a JSON object")
    repo = doc.get("repository") or {}
    if event == "ping":
        return Ignored("ping")
    if not isinstance(repo, dict) or repo.get("full_name") != github:
        raise Rejected(f"the delivery is not for {github}")
    default = repo.get("default_branch")
    action = doc.get("action")
    if event == "pull_request":
        pr = doc.get("pull_request") or {}
        if not isinstance(pr, dict):
            raise Rejected("pull_request is not an object")
        number = _number(pr.get("number"))
        if action != "closed":
            return Ignored(f"pull request {action}")
        if not pr.get("merged"):
            return PullRequestClosed(number)
        base = (pr.get("base") or {}).get("ref")
        if base != default:
            return Ignored(f"merged into {base}, not the default branch")
        sha = pr.get("merge_commit_sha")
        if not isinstance(sha, str) or not _SHA.match(sha):
            raise Rejected("merge_commit_sha is not a commit SHA")
        return PullRequestMerged(number, sha)
    if event == "issues":
        issue = doc.get("issue") or {}
        if action == "closed" and isinstance(issue, dict):
            return IssueClosed(_number(issue.get("number")))
        return Ignored(f"issue {action}")
    if event == "push":
        # Default-branch commits are reported with their first-parent positions, which a
        # push event does not carry; phase 18 takes them from the monitors job's clone.
        return Ignored("push")
    return Ignored(f"event {event}")
