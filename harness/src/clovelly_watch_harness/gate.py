"""The gate (``SPEC-watch.md``, "The alert job", step 4): what the harness itself runs
before a fix is pushed. The model's account of having run anything counts for nothing.

The session and the harness run as the same user, so the gate does not trust what the
session left behind: it ends every process the session left running (and refuses if any
remains), takes only the files the model named (regular files, inside the repository, none
under ``.github/``, ``.clovelly/`` or another CI configuration), and runs them in a fresh
clone made for each run, after the sweep:

1. the deployed commit with the new tests, each file on its own: each exits 0 or the
   runner's "a test failed" status (1, or the repository's ``test_failed_exit``), and at
   least one fails. Any other status (pytest's 2 to 5 for collection and usage errors, 124
   for a timeout, 127 for a missing command, a signal) shows nothing. Where the repository
   names a machine report (``test_report``, Jest's or Vitest's JSON), a failure counts only
   if the report shows a failed assertion and no test file that did not load; where it does
   not and the runner is not pytest, the gate cannot tell a failed assertion from a test that
   did not load, and the pull request says so (:func:`strength`);
2. the default branch's head with the tests and the fix: each file passes; then the
   repository's whole ``test`` command, in another fresh clone.

The tests must be new files, absent at the deployed commit and the head, and the fix may not
add, change or delete a test file or a file that decides how tests run (:func:`runner_config`):
otherwise "passed with the patch" could mean the patch changed what passing means. A patch
touching a forbidden path is refused before anything runs, and nothing is pushed.

Every run gets an empty home and temporary directory of its own, ``PYTHONNOUSERSITE=1``, no
npm user configuration and the harness's ``PATH`` without directories the session can write,
so nothing the session left in its home (a ``.pth`` file, ``.npmrc``, a shim) runs in it. The
session and the harness are still one user, and a fix can still patch the test framework from
code the test imports: the gate is the harness's report of what it ran, a guard against
mistakes, not a proof.

Nothing the harness sends (files, commit messages, titles, bodies) may copy a payload it
opened (:func:`payload_matches`, the rule of 2026-11-11): tests use made-up inputs. The
rule catches copies that survive normalising (case, whitespace, common escapes, a number's
separators), not an encoded or split one.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clovelly_watch_runner.schema import test_path

#: Paths a fix may never touch: CI and Watch's own configuration (prefixes end in ``/``).
FORBIDDEN = (
    ".github/",
    ".clovelly/",
    ".circleci/",
    ".buildkite/",
    ".gitlab/",
    ".gitlab-ci.yml",
    ".travis.yml",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
    "appveyor.yml",
    ".drone.yml",
    "Jenkinsfile",
    ".pre-commit-config.yaml",
    ".gitmodules",
)
#: How long the whole suite may take, seconds.
TEST_TIMEOUT = 600
#: How long one named test file may take, seconds.
FILE_TIMEOUT = 300
#: A test runner's "a test failed" status: pytest's, jest's, vitest's and ``node --test``'s.
FAILED_EXIT = 1
#: Files that decide how tests run, by name at any depth: a fix may not touch them.
RUNNER_CONFIG = frozenset(
    {
        ".npmrc",
        ".yarnrc",
        ".yarnrc.yml",
        "bunfig.toml",
        "conftest.py",
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "noxfile.py",
        "sitecustomize.py",
        "usercustomize.py",
        "package.json",
        "Makefile",
        "makefile",
        "GNUmakefile",
        ".gitattributes",
        ".babelrc",
        ".swcrc",
    }
)
#: Name prefixes of the same (``jest.config.ts``, ``vitest.config.mjs``, ``.mocharc.yml``), the
#: files that run before every test (``jest.setup.ts``, ``setupTests.js``, ``vitest.setup.ts``)
#: and what decides what tests compile to (``tsconfig.json``, ``tsconfig.build.json``,
#: ``babel.config.js``). A setup file a config names under another name is not caught.
RUNNER_CONFIG_PREFIXES = (
    "jest.config.",
    "vitest.config.",
    "vite.config.",
    ".mocharc",
    "jest.setup.",
    "setupTests.",
    "vitest.setup.",
    "tsconfig",
    "babel.config.",
    ".babelrc.",
    "vitest.workspace.",
)
#: Directory names that hold tests.
TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec"})
#: Output kept from a run, characters (its end).
TAIL = 4000
#: Variables tests never see: the gateway's token and Claude Code's settings.
_DROPPED = ("ANTHROPIC_", "CLAUDE_")


class Refused(ValueError):
    """A patch the gate will not run or push."""


#: What never leaves in anything the harness sends (files, commit messages, pull request and
#: issue titles and bodies): a gateway token's prefix and the credential placeholders'.
SECRET_MARKS = ("cwg_", "clovelly://cred/")


#: The payload rule (of 2026-11-11, as the fix round's A13 shaped it). Payload text is
#: normalised (case folded, whitespace collapsed, the common escapes undone: ``\\uXXXX``,
#: ``\\n``, ``%XX``, HTML entities); ISO timestamps and UUIDs in it are cut out, unless they hold
#: an ``@``; and a run of this many characters of what is left, found in anything the harness
#: sends, normalised the same way, is a match.
PAYLOAD_RUN = 24
#: Shorter whole values matched too when they look like someone's data: 12 to 23 characters
#: with an ``@`` or at least 6 digits (an email address, a card, phone or account number).
SHORT_VALUE = 12
#: Numbers (JSON numbers, or strings of digits and separators) of at least this many digits
#: are matched by their digits alone, so a card or phone number with its dashes removed is
#: found; values that read as a Unix time (2000 to 2100, in s, ms, µs or ns) are not.
DIGITS = 8
#: A run with fewer distinct characters than this is padding (spaces, dashes, zeros): exempt.
DISTINCT = 6
#: A match counts only if it holds a word (letters, at least this long, or 6 digits or more)
#: that neither the records' cleartext metadata nor the repository holds (``alert``): so a
#: route's path, a field name or a span name the code makes is no copy.
WORD = 4

_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\([ntr\"\\/])|%([0-9a-fA-F]{2})|&(amp|lt|gt|quot|apos|#39);")
_PLAIN = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
_ENTITY = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "#39": "'"}
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:z|[+-]\d{2}:?\d{2})?)?")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: A word: letters and digits (any script), as :func:`words` takes them.
TOKEN = re.compile(r"[^\W_]+")


def _unescape(m: re.Match[str]) -> str:
    u, plain, pct, ent = m.groups()
    if u:
        return chr(int(u, 16))
    if plain:
        return _PLAIN[plain]
    if pct:
        return chr(int(pct, 16))
    return _ENTITY[ent]


def normalise(text: str) -> str:
    """``text`` as the payload rule compares it: the common escapes undone (twice, for one
    layer of double escaping), case folded, and every run of whitespace one space."""
    for _ in range(2):
        text = _ESCAPE.sub(_unescape, text)
    return " ".join(text.casefold().split())


def _leaves(v: Any) -> Iterator[str]:
    """Every string, number and key in a JSON value."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, bool) or v is None:
        return
    elif isinstance(v, int | float):
        yield str(v)
    elif isinstance(v, dict):
        for k, x in v.items():
            yield str(k)
            yield from _leaves(x)
    elif isinstance(v, list):
        for x in v:
            yield from _leaves(x)


def _epoch(digits: str) -> bool:
    """Whether a run of digits reads as a Unix time from 2000 to 2100, in s, ms, µs or ns."""
    n = int(digits)
    return any(946_684_800 * k <= n <= 4_102_444_800 * k for k in (1, 1_000, 1_000_000, 1_000_000_000))


def _digits(text: str) -> str:
    """``text`` with the separators between digits (spaces, dots, dashes) removed."""
    return re.sub(r"(?<=\d)[ .-]+(?=\d)", "", text)


@dataclass(frozen=True)
class Payloads:
    """What the payload rule looks for, from the opened payloads of an alert's records."""

    runs: frozenset[str]
    values: frozenset[str]
    numbers: frozenset[str]


def payloads(records: list[dict[str, Any]]) -> Payloads:
    runs: set[str] = set()
    values: set[str] = set()
    numbers: set[str] = set()
    for r in records:
        for leaf in _leaves(r.get("payload")):
            text = normalise(leaf)
            digits = _digits(text)
            if digits.isdigit() and len(digits) >= DIGITS:
                if not _epoch(digits):
                    numbers.add(digits)
                continue
            if "@" not in text:
                text = _UUID.sub("\0", _ISO.sub("\0", text))
            for piece in text.split("\0"):
                if len(piece) >= PAYLOAD_RUN:
                    runs.update(
                        w
                        for w in (piece[i : i + PAYLOAD_RUN] for i in range(len(piece) - PAYLOAD_RUN + 1))
                        if len(set(w)) >= DISTINCT
                    )
                elif len(piece) >= SHORT_VALUE and ("@" in piece or sum(c.isdigit() for c in piece) >= 6):
                    values.add(piece)
    return Payloads(frozenset(runs), frozenset(values), frozenset(numbers))


@dataclass(frozen=True)
class Match:
    """A payload's text found in an outgoing text: a window of :data:`PAYLOAD_RUN` characters
    (whose first and last words may be cut), or a whole value or number."""

    text: str
    window: bool


def payload_matches(records: list[dict[str, Any]], *texts: str | bytes | None) -> list[Match]:
    """What ``texts`` hold of ``records``' payloads (:func:`payloads`), after normalising both;
    the caller keeps only those with a word nobody else holds (:func:`words`)."""
    p = payloads(records)
    if not (p.runs or p.values or p.numbers):
        return []
    found: set[Match] = set()
    for t in texts:
        if t is None:
            continue
        text = normalise(t.decode(errors="replace") if isinstance(t, bytes) else t)
        found.update(
            Match(w, True) for w in (text[i : i + PAYLOAD_RUN] for i in range(len(text) - PAYLOAD_RUN + 1)) if w in p.runs
        )
        found.update(Match(v, False) for v in p.values if v in text)
        digits = _digits(text)
        found.update(Match(n, False) for n in p.numbers if n in digits)
    return sorted(found, key=lambda m: m.text)


def words(m: Match) -> list[str]:
    """The words of a match that could identify someone: letters of at least :data:`WORD`, or
    6 digits or more; a window's first and last words aside, as they may be cut."""
    found = TOKEN.findall(m.text)
    if m.window:
        found = found[1:-1]
    return [w for w in found if (len(w) >= WORD and not w.isdigit()) or (w.isdigit() and len(w) >= 6)]


def screen_out(token: str, *texts: str | bytes | None) -> None:
    """Refuses, before anything is sent, a text holding the job's gateway token or one of
    :data:`SECRET_MARKS` (the job file the session can read holds both)."""
    marks = [m.encode() for m in (*SECRET_MARKS, token) if m]
    for t in texts:
        if t is None:
            continue
        b = t.encode() if isinstance(t, str) else t
        if any(m in b for m in marks):
            raise Refused("an outgoing file or text holds the job's token or a credential placeholder: nothing is sent")


def forbidden(path: str) -> bool:
    """Whether a fix may not touch ``path`` (CI or Watch configuration)."""
    return any(path.startswith(f) if f.endswith("/") else path == f for f in FORBIDDEN)


def failure_status(n: int) -> bool:
    """Whether ``n`` may be a repository's "a test failed" status: 1, or 6 to 123. Never
    pytest's 2 to 5, the gate's 124 to 127, or a signal's 128 and up."""
    return n == 1 or 6 <= n <= 123


def runner_config(path: str) -> bool:
    """Whether ``path`` decides how tests run (:data:`RUNNER_CONFIG`)."""
    name = path.rsplit("/", 1)[-1]
    return name in RUNNER_CONFIG or name.startswith(RUNNER_CONFIG_PREFIXES) or name.endswith(".pth")


def test_file(path: str) -> bool:
    """Whether ``path`` looks like a test: under a test directory, or named as one
    (``test_x.py``, ``x_test.py``, ``x.test.ts``, ``x.spec.js``)."""
    *dirs, name = path.split("/")
    stem = name.split(".", 1)[0]
    return (
        any(d in TEST_DIRS for d in dirs)
        or name.startswith("test_")
        or stem.endswith("_test")
        or ".test." in name
        or ".spec." in name
    )


@dataclass(frozen=True)
class Patch:
    """The files of a fix, as the model named them: path to content, None for a deletion."""

    tests: dict[str, bytes | None]
    fix: dict[str, bytes | None]

    @property
    def paths(self) -> list[str]:
        return [*self.tests, *self.fix]

    @classmethod
    def take(cls, tree: Path, tests: list[str], fix: list[str]) -> Patch:
        """Reads the named files from the session's working tree, refusing a path that
        leaves it, a forbidden one, a symbolic link or anything but a regular file, a test
        that decides how tests run, and a fix file that is a test or does."""
        if not tests:
            raise Refused("a fix needs its test")
        named = [*tests, *fix]
        if len(set(named)) != len(named):
            raise Refused("a path is named twice")
        for p in named:
            if not test_path(p):
                raise Refused(f"{p!r} is not a relative path inside the repository")
            if forbidden(p):
                raise Refused(f"{p} is CI or Watch configuration, which a fix may not touch")
            if runner_config(p):
                raise Refused(f"{p} decides how tests run, which neither a test nor a fix may change")
        for p in fix:
            if test_file(p):
                raise Refused(f"{p} is a test: the fix may not add, change or delete a test")

        def read(p: str) -> bytes | None:
            f = tree / p
            if f.is_symlink():
                raise Refused(f"{p} is a symbolic link")
            if not f.exists():
                return None
            if not f.is_file():
                raise Refused(f"{p} is not a file")
            return f.read_bytes()

        patch = cls({p: read(p) for p in tests}, {p: read(p) for p in fix})
        if any(v is None for v in patch.tests.values()):
            raise Refused("a test file named is not there")
        return patch

    def write(self, tree: Path, *, tests_only: bool = False) -> None:
        for p, content in [*self.tests.items(), *([] if tests_only else self.fix.items())]:
            f = tree / p
            if content is None:
                f.unlink(missing_ok=True)
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(content)


def guarded_files(tree: Path) -> dict[str, bytes]:
    """Every file under ``tree`` a fix may not touch (:func:`forbidden`, :func:`runner_config`),
    by walking the directory, not by asking git (whose configuration the session can write),
    with its bytes; ``.git`` and symbolic links' targets aside (a link is recorded as such)."""
    out: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(tree):
        rel_dir = os.path.relpath(dirpath, tree)
        dirnames[:] = [d for d in dirnames if not (rel_dir == "." and d == ".git")]
        for name in [*filenames, *(d for d in dirnames if os.path.islink(os.path.join(dirpath, d)))]:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if not (forbidden(rel) or runner_config(rel)):
                continue
            full = os.path.join(dirpath, name)
            out[rel] = b"link:" + os.readlink(full).encode() if os.path.islink(full) else Path(full).read_bytes()
    return out


def changed_guarded(before: dict[str, bytes], tree: Path) -> list[str]:
    """The guarded paths whose bytes differ between ``before`` (held in memory) and ``tree``
    now, including ones added or removed."""
    now = guarded_files(tree)
    return sorted(p for p in before.keys() | now.keys() if before.get(p) != now.get(p))


#: Sweeps before :func:`kill_strays` gives up.
SWEEPS = 50


def kill_strays() -> int:
    """Ends every process of this user but this one, its ancestors and the sandbox's own
    first process: whatever the session or a test left running. Sweeps until a sweep finds
    nothing; :class:`Refused` if processes still appear after :data:`SWEEPS` (one that
    re-spawns faster than it is ended). The number ended."""
    if not Path("/proc/self/status").exists():
        return 0  # not the sandbox's Linux: nothing to end (the harness's own tests on a laptop)
    me, uid = os.getpid(), os.getuid()
    keep = {1, me}
    pid = me
    while pid > 1:
        try:
            pid = int(
                next(line for line in Path(f"/proc/{pid}/status").read_text().splitlines() if line.startswith("PPid:")).split()[1]
            )
        except (OSError, StopIteration, ValueError):
            break
        keep.add(pid)
    ended = 0
    for _ in range(SWEEPS):
        found = 0
        for d in Path("/proc").iterdir():
            if not d.name.isdigit() or int(d.name) in keep:
                continue
            try:
                if d.stat().st_uid != uid:
                    continue
                os.kill(int(d.name), signal.SIGKILL)
                found += 1
            except OSError:
                continue
        ended += found
        if not found:
            return ended
        time.sleep(0.05)
    raise Refused(f"processes kept appearing through {SWEEPS} sweeps: nothing is trusted from this session")


def write_result(path: Path, text: str) -> None:
    """Writes the result file as the session left nothing behind to change it: every other
    process ended first, then a new regular file made in place of whatever was there (a
    link the session planted is removed, not followed)."""
    kill_strays()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


@dataclass
class Run:
    exit: int
    tail: str


#: Variables a run never inherits: the session could have set them for its own shell, and the
#: gate sets its own.
_RESET = ("HOME", "TMPDIR", "PATH", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "NODE_OPTIONS", "NODE_PATH")


def clean_path(path: str, home: str) -> str:
    """``path`` without directories the session can write: its home, ``/tmp``, ``/job`` and
    relative entries."""
    writable = tuple(d.rstrip("/") + "/" for d in (home, "/tmp", "/var/tmp", "/job") if d)
    keep = [d for d in path.split(":") if d.startswith("/") and not (d.rstrip("/") + "/").startswith(writable)]
    return ":".join(keep) or "/usr/local/bin:/usr/bin:/bin"


def test_env(home: Path) -> dict[str, str]:
    """A run's environment: the harness's, without the gateway's token or Claude Code's
    settings, with ``home`` as an empty home and temporary directory, no user site for
    Python, no npm user configuration, and a ``PATH`` of directories the session cannot write."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_DROPPED) and k not in _RESET}
    tmp = home / "tmp"
    tmp.mkdir(exist_ok=True)
    env.update(
        HOME=str(home),
        TMPDIR=str(tmp),
        PATH=clean_path(os.environ.get("PATH", ""), os.environ.get("HOME", "")),
        PYTHONNOUSERSITE="1",
        npm_config_userconfig="/dev/null",
    )
    return env


def run(command: str, tree: Path, extra: list[str] | None = None, *, timeout: int = TEST_TIMEOUT) -> Run:
    """Runs a command of the repository's (``test`` or ``setup``, from ``.clovelly/watch.toml``)
    in ``tree``, with ``extra`` arguments and a fresh home (:func:`test_env`); a run past
    ``timeout`` seconds is exit 124."""
    argv = [*shlex.split(command), *(extra or [])]
    home = Path(tempfile.mkdtemp(prefix="watch-gate-home-"))
    try:
        r = subprocess.run(argv, cwd=tree, capture_output=True, timeout=timeout, env=test_env(home))
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"") + (e.stderr or b"")
        return Run(124, out.decode(errors="replace")[-TAIL:])
    except OSError as e:
        return Run(127, str(e))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return Run(r.returncode, (r.stdout + r.stderr).decode(errors="replace")[-TAIL:])


#: The machine reports a repository may name (``test_report``): the flags that make its test
#: command write one to a file. Vitest's JSON reporter writes Jest's format.
REPORTS = {
    "jest-json": lambda out: ["--json", f"--outputFile={out}"],
    "vitest-json": lambda out: ["--reporter=json", f"--outputFile={out}"],
}
#: A run whose report shows a test file that did not load (or no report): never a failure.
LOAD_ERROR = 2


def from_report(r: Run, report: Path, failed: int) -> Run:
    """A run of one test file, judged by its Jest-format report: ``failed`` only if it shows
    a failed assertion and no file that did not load, 0 if it passed, else :data:`LOAD_ERROR`."""
    try:
        doc = json.loads(report.read_text())
    except (OSError, ValueError):
        return Run(r.exit if r.exit not in (0, failed) else LOAD_ERROR, "no machine report was written\n" + r.tail)
    results = doc.get("testResults") or []
    not_loaded = int(doc.get("numRuntimeErrorTestSuites") or 0) + sum(
        1 for t in results if t.get("status") == "failed" and not t.get("assertionResults")
    )
    if not_loaded:
        return Run(LOAD_ERROR, "the report shows a test file that did not load (an import or compile error):\n" + r.tail)
    if int(doc.get("numFailedTests") or 0) >= 1:
        return Run(failed, r.tail)
    if r.exit == 0:
        return Run(0, r.tail)
    return Run(LOAD_ERROR if r.exit == failed else r.exit, "the runner failed with no failed test in its report:\n" + r.tail)


def run_each(command: str, tree: Path, tests: list[str], failed: int, report: str | None = None) -> Run:
    """Runs each test file on its own, :data:`FILE_TIMEOUT` each: exit ``failed`` if at least
    one failed and every other passed; 0 if all passed; else the first other status. With a
    ``report`` (:data:`REPORTS`), each run is judged by it (:func:`from_report`)."""

    def one(t: str) -> Run:
        if report is None:
            return run(command, tree, [t], timeout=FILE_TIMEOUT)
        out = Path(tempfile.mkdtemp(prefix="watch-report-")) / "report.json"
        try:
            return from_report(run(command, tree, [t, *REPORTS[report](out)], timeout=FILE_TIMEOUT), out, failed)
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)

    runs = [one(t) for t in tests]
    tail = "\n".join(f"== {t}: exit {r.exit}\n{r.tail}" for t, r in zip(tests, runs, strict=True))[-TAIL:]
    other = next((r.exit for r in runs if r.exit not in (0, failed)), None)
    if other is not None:
        return Run(other, tail)
    return Run(failed if any(r.exit == failed for r in runs) else 0, tail)


def is_pytest(command: str) -> bool:
    """Whether ``command`` runs pytest, whose exit status alone tells a failed test (1) from
    a collection or usage error (2 to 5)."""
    words = shlex.split(command)
    return any(w == "pytest" or w.endswith("/pytest") for w in words) or any(
        a == "-m" and b == "pytest" for a, b in zip(words, words[1:], strict=False)
    )


def strength(command: str, report: str | None) -> str | None:
    """What the gate cannot tell for this test command, said in the pull request: None where
    it tells a failed assertion from a test that did not load (pytest, or a machine report)."""
    if report is not None or is_pytest(command):
        return None
    return (
        "this repository's test runner reports a failed test and a test that did not load (an import or compile error) "
        "with the same exit status, and it names no machine report (`test_report` in `.clovelly/watch.toml`), so "
        "'failed on the deployed commit' may mean the test did not run there"
    )


@dataclass
class Outcome:
    """What the gate found. ``passed`` only if the tests failed before with the runner's
    "a test failed" status (``failed``) and passed after, and the whole suite passed after."""

    before: Run
    failed: int = FAILED_EXIT
    after: Run | None = None
    suite: Run | None = None
    log: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.before.exit == self.failed
            and self.after is not None
            and self.after.exit == 0
            and self.suite is not None
            and self.suite.exit == 0
        )

    def why(self) -> str:
        if self.before.exit == 0:
            return "the new tests passed on the deployed commit, so they do not show the bug:\n" + self.before.tail
        if self.before.exit != self.failed:
            return (
                f"the new tests exited {self.before.exit} on the deployed commit, not the test runner's 'a test failed' "
                f"status {self.failed}: an error (collection, an import of something the fix adds, a timeout) shows "
                "nothing:\n" + self.before.tail
            )
        if self.after is None or self.after.exit != 0:
            return "the new tests fail with the patch on the default branch's head:\n" + (self.after.tail if self.after else "")
        return "the repository's test command fails with the patch:\n" + (self.suite.tail if self.suite else "")
