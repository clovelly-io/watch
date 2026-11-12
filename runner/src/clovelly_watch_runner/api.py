"""Watch's API as the runner calls it (the service's ``API.md``): jobs claimed by long poll
under a lease, monitors, default-branch commits and the fate of pull requests and issues.

Standard library only. Every call sends the organization's runner token (``cwn_``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

#: The longest a claim waits for a job, seconds (the API's own limit, under its load
#: balancer's 30-second timeout).
CLAIM_WAIT = 20


class ApiError(Exception):
    """An answer other than success: the status and Watch's ``error`` object."""

    def __init__(self, status: int, error: dict[str, Any]) -> None:
        super().__init__(f"{status} {error.get('problem', '')}: {error.get('message', '')}")
        self.status = status
        self.problem = str(error.get("problem", ""))
        self.field = error.get("field")


class LeaseLost(ApiError):
    """The job's lease lapsed or was taken by another claim: stop working on it."""


class WatchApi:
    def __init__(self, url: str, runner_token: str, *, name: str = "runner", timeout: float = 60.0) -> None:
        self.url = url.rstrip("/")
        self._token = runner_token
        self.name = name
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Any = None, timeout: float | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error", {})
            except (ValueError, AttributeError):
                err = {}
            cls = LeaseLost if err.get("problem") == "lease_not_held" else ApiError
            raise cls(e.code, err) from None

    def repositories(self) -> list[dict[str, Any]]:
        return self._call("GET", "/v1/repos")["repositories"]

    def monitors(self, repo: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/v1/repos/{repo}/monitors")["monitors"]

    def claim(self, wait: int = CLAIM_WAIT) -> dict[str, Any] | None:
        """A job, or None when none came within ``wait`` seconds."""
        return self._call("POST", f"/v1/jobs/claim?wait={wait}&runner={self.name}", timeout=wait + 30)

    def renew(self, job: str, lease: str) -> int:
        """The lease's new end (unix seconds); raises LeaseLost once it has lapsed."""
        return self._call("POST", f"/v1/jobs/{job}/renew", {"lease": lease})["lease_expires_unix"]

    def outcome(self, job: str, lease: str, outcome: dict[str, Any]) -> None:
        self._call("POST", f"/v1/jobs/{job}/outcome", {"lease": lease, **outcome})

    def job(self, job: str) -> dict[str, Any]:
        """A job's state and its model spend as the gateway counted it."""
        return self._call("GET", f"/v1/jobs/{job}")

    def alert_records(self, alert: str) -> list[dict[str, Any]]:
        """Up to 20 records that show an alert, their payloads still sealed."""
        return self._call("GET", f"/v1/alerts/{alert}/records")["records"]

    def commits(self, repo: str, commits: list[dict[str, Any]]) -> None:
        self._call("POST", f"/v1/repos/{repo}/commits", {"commits": commits})

    def pull_request_merged(self, repo: str, number: int, merged_sha: str) -> dict[str, Any]:
        """Reports a merge into the default branch; Watch queues its monitors job once."""
        return self._call("POST", f"/v1/repos/{repo}/pull-requests/{number}", {"merged_sha": merged_sha})

    def pull_request_closed(self, repo: str, number: int) -> dict[str, Any]:
        return self._call("POST", f"/v1/repos/{repo}/pull-requests/{number}", {"closed": True})

    def issue_closed(self, repo: str, number: int) -> dict[str, Any]:
        return self._call("POST", f"/v1/repos/{repo}/issues/{number}", {"closed": True})
