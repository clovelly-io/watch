"""The repository, cloned fresh into the session: a blobless partial clone over HTTPS.

The sandbox holds no GitHub token. Git sends the placeholder of the ``github-git``
credential as its basic-auth password and the egress proxy puts the real token in its
place on the way to ``github.com`` (the basic adapter); anywhere else the placeholder is
just a string.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

GIT_CREDENTIAL = "github-git"
PLACEHOLDER = f"clovelly://cred/{GIT_CREDENTIAL}"
#: The diff handed to the agent is cut here; it reads the rest of the files itself.
MAX_DIFF = 200_000


class GitError(RuntimeError):
    pass


#: The basic-auth user name GitHub takes with a token as the password.
GIT_USER = "x-access-token"


def _auth_header() -> str:
    pair = base64.b64encode(f"{GIT_USER}:{PLACEHOLDER}".encode()).decode()
    return f"http.extraHeader=Authorization: Basic {pair}"


#: Every git command the harness runs ignores hooks and the user's and system's
#: configuration, so nothing a session left in the clone runs as the harness's git.
_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.file.allow=always")


def _env(**extra: str) -> dict[str, str]:
    return dict(
        os.environ,
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_NO_REPLACE_OBJECTS="1",
        **extra,
    )


def git(
    *args: str, cwd: Path | None = None, check: bool = True, input: bytes | None = None, env: dict[str, str] | None = None
) -> str:
    r = subprocess.run(
        ["git", "-c", _auth_header(), *_SAFE, *args],
        cwd=cwd,
        input=input,
        capture_output=True,
        env=_env(**(env or {})),
    )
    if check and r.returncode != 0:
        raise GitError(f"git {args[0]} failed ({r.returncode}): {r.stderr.decode(errors='replace').strip()[-500:]}")
    return r.stdout.decode(errors="replace")


def clone(github: str, into: Path, *, bare: bool = False) -> None:
    """A blobless clone of ``github`` (``owner/name``), no checkout yet."""
    git(
        "clone",
        "--quiet",
        "--filter=blob:none",
        "--bare" if bare else "--no-checkout",
        f"https://github.com/{github}.git",
        str(into),
    )


def checkout(repo: Path, sha: str) -> None:
    git("checkout", "--quiet", "--detach", sha, cwd=repo)


def keep_credential_placeholder(repo: Path) -> None:
    """Writes the placeholder's header into the clone's own configuration, so git run in a
    session (``git log -p``, ``git show``) can fetch the blobs a blobless clone lacks. It is
    the placeholder only; the proxy holds the token."""
    git("config", "--local", "http.https://github.com/.extraHeader", _auth_header().split("=", 1)[1], cwd=repo)


def default_head(repo: Path) -> tuple[str, str]:
    """The default branch's name and its head commit, as the clone found them."""
    ref = git("symbolic-ref", "refs/remotes/origin/HEAD", cwd=repo).strip()
    return ref.rsplit("/", 1)[1], git("rev-parse", ref, cwd=repo).strip()


def remote_branch(github: str, branch: str) -> str | None:
    """The head commit of ``branch`` on GitHub, or None if there is no such branch."""
    out = git("ls-remote", f"https://github.com/{github}.git", f"refs/heads/{branch}")
    return out.split()[0] if out.strip() else None


def worktree(repo: Path, path: Path, sha: str) -> None:
    """A clean working tree of ``sha`` at ``path``, sharing the clone's objects."""
    git("worktree", "add", "--quiet", "--force", "--detach", str(path), sha, cwd=repo)


def is_ancestor(repo: Path, ancestor: str, of: str) -> bool:
    """Whether ``ancestor`` is ``of`` or one of its ancestors."""
    r = subprocess.run(["git", *_SAFE, "merge-base", "--is-ancestor", ancestor, of], cwd=repo, capture_output=True, env=_env())
    return r.returncode == 0


def name_status(repo: Path, a: str, b: str) -> dict[str, str]:
    """The paths ``b`` changes against ``a``, each with its status letter (``A``, ``M``, ``D``;
    renames and copies as a deletion and an addition)."""
    out = git("diff", "--name-status", "--no-renames", "-z", a, b, cwd=repo).split("\0")
    return {path: status[:1] for status, path in zip(out[0::2], out[1::2], strict=False) if path}


def first_parent_position(repo: Path, sha: str) -> int:
    """The commit's position on the default branch's first-parent chain, as Watch counts it."""
    return int(git("rev-list", "--count", "--first-parent", sha, cwd=repo).strip())


def merge_diff(repo: Path, sha: str) -> tuple[str, list[str], bool]:
    """What the merge commit changed against its first parent: the diff (cut at
    ``MAX_DIFF``), the files it touched, and whether it was cut."""
    files = [f for f in git("diff", "--name-only", f"{sha}^1", sha, cwd=repo).splitlines() if f]
    diff = git("diff", "--unified=5", f"{sha}^1", sha, cwd=repo)
    return diff[:MAX_DIFF], files, len(diff) > MAX_DIFF


def ls_remote(github: str) -> subprocess.CompletedProcess[str]:
    """``git ls-remote`` of the repository, for ``check``."""
    return subprocess.run(
        ["git", "-c", _auth_header(), *_SAFE, "ls-remote", f"https://github.com/{github}.git", "HEAD"],
        text=True,
        capture_output=True,
        timeout=60,
        env=_env(),
    )


def tracked_word(tree: Path, word: str) -> bool:
    """Whether any file git tracks in ``tree``'s working tree holds ``word`` as a whole word,
    in any case (binary files aside)."""
    r = subprocess.run(
        ["git", *_SAFE, "grep", "-q", "-I", "-i", "-w", "-F", "-e", word, "--"], cwd=tree, capture_output=True, env=_env()
    )
    if r.returncode not in (0, 1):
        raise GitError(f"git grep failed ({r.returncode}): {r.stderr.decode(errors='replace').strip()[-300:]}")
    return r.returncode == 0
