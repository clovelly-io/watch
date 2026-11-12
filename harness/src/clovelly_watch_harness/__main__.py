"""clovelly-watch-harness, run by the runner inside a sandbox.

  clovelly-watch-harness run /job/job.json
      Runs the job the file describes and writes /job/result.json. Exit 0 with a result,
      3 when the job's model budget ran out, 1 otherwise.
  clovelly-watch-harness check /job/check.json
      What `clovelly-watch-runner check` needs from inside a session: one JSON line each
      for the git credential, the API credential and the model gateway.

The job kinds are ``KINDS``: the monitors job (``monitors.py``) and the alert job (``alert.py``).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import alert, git, monitors

KINDS: dict[str, Callable[[dict[str, Any]], int]] = {"monitors": monitors.run, "alert": alert.run}

API_PLACEHOLDER = "clovelly://cred/github-api"
#: What git says when the proxy refuses the connection (the destination is not in the
#: policy) or there is no route, and when GitHub refuses the credential.
UNREACHABLE = (
    "from proxy after CONNECT",
    "CONNECT tunnel failed",
    "Could not resolve host",
    "Failed to connect",
    "Connection refused",
    "Network is unreachable",
)
REFUSED = (
    "Authentication failed",
    "could not read Username",
    "terminal prompts disabled",
    "Invalid username or password",
    "returned error: 401",
    "returned error: 403",
)


def _line(check: str, ok: bool, cause: str = "") -> None:
    print(json.dumps({"check": check, "ok": ok, "cause": cause}), flush=True)


def _unreachable(host: str, detail: str) -> str:
    return f"{host} is not reachable from the sandbox; is it a destination in the sub-environment's policy? ({detail[-200:]})"


def check(spec: dict[str, Any]) -> int:
    gh = spec["github"]
    r = git.ls_remote(gh)
    err = r.stderr
    if r.returncode == 0:
        _line("github-git", True)
    elif any(s in err for s in UNREACHABLE):
        _line("github-git", False, _unreachable("github.com", err.strip()))
    elif any(s in err for s in REFUSED):
        _line(
            "github-git",
            False,
            "github.com refused the github-git credential: a wrong or expired token, or one without access to the repository",
        )
    elif "not found" in err:
        _line("github-git", False, f"github.com has no repository {gh} the token can read")
    else:
        _line("github-git", False, f"git ls-remote failed: {err.strip()[-200:]}")

    # A path under /repos/OWNER/NAME/, the policy's path rule for the repository.
    req = urllib.request.Request(
        f"https://api.github.com/repos/{gh}/branches?per_page=1",
        headers={"Authorization": f"Bearer {API_PLACEHOLDER}", "User-Agent": "clovelly-watch-harness"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            _line("github-api", True)
    except urllib.error.HTTPError as e:
        cause = {
            401: "api.github.com refused the github-api credential: a wrong or expired token",
            404: f"the token cannot read {gh} through the API",
        }
        _line("github-api", False, cause.get(e.code, f"api.github.com answered {e.code}"))
    except OSError as e:
        _line("github-api", False, _unreachable("api.github.com", str(e)))

    url = spec["gateway_url"].rstrip("/") + "/api/hello"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30):
            _line("gateway", True)
    except urllib.error.HTTPError as e:
        _line("gateway", False, f"the model gateway answered {e.code}")
    except OSError as e:
        _line("gateway", False, _unreachable(spec["gateway_url"], str(e)))
    return 0


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in ("run", "check"):
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    spec = json.loads(Path(args[1]).read_text())
    if args[0] == "check":
        sys.exit(check(spec))
    run = KINDS.get(spec.get("kind"))
    if run is None:
        print(f"this harness does not run {spec.get('kind')!r} jobs", file=sys.stderr)
        sys.exit(1)
    try:
        sys.exit(run(spec))
    except git.GitError as e:
        print(e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
