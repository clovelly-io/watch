"""GitHub's REST API as the harness uses it: pull requests and issues on the job's own
repository. The request carries the ``github-api`` credential's placeholder as its bearer
token; the egress proxy puts the real token in its place on the way to ``api.github.com``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

API_PLACEHOLDER = "clovelly://cred/github-api"
#: What the harness writes into a title or body at most; GitHub's own limit is 65,536.
MAX_TITLE = 200
MAX_BODY = 30_000


class GitHubError(RuntimeError):
    pass


def _call(method: str, path: str, body: Any = None) -> Any:
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {API_PLACEHOLDER}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "clovelly-watch-harness",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise GitHubError(f"{method} {path}: {e.code} {e.read()[:300]!r}") from None
    except OSError as e:
        raise GitHubError(f"{method} {path}: {e}") from None


def open_pull_request(github: str, *, branch: str, base: str, title: str, body: str) -> int:
    pr = _call(
        "POST", f"/repos/{github}/pulls", {"title": title[:MAX_TITLE], "head": branch, "base": base, "body": body[:MAX_BODY]}
    )
    return int(pr["number"])


def open_issue(github: str, *, title: str, body: str) -> int:
    issue = _call("POST", f"/repos/{github}/issues", {"title": title[:MAX_TITLE], "body": body[:MAX_BODY]})
    return int(issue["number"])


def open_pull_request_for(github: str, branch: str) -> int | None:
    """The open pull request from ``branch`` of the same repository, if there is one: a
    session that finds its own branch already pushed (its runner died after the push)
    reports that pull request rather than pushing again."""
    owner = github.split("/", 1)[0]
    found = _call("GET", f"/repos/{github}/pulls?state=open&head={owner}:{branch}")
    return int(found[0]["number"]) if found else None
