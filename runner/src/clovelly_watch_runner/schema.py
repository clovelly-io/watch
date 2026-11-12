"""The closed schemas a session's result must fit before anything of it leaves the runner.

A session is untrusted: whatever it writes was produced by a model that read the
customer's code and, in alert jobs, payloads anyone could have sent the service. The
runner reads only ``/job/result.json`` back, parses it here and passes on only the typed
value; a result that does not fit is refused whole (``Refused``, naming the field), and
nothing of it reaches Watch's API.

The monitor schema is ``docs/watch-monitors.md``; Watch's own parser (Rust,
``crates/watch/src/monitor.rs`` in the service) enforces the same rules, and both are held to
``vectors/monitors.json``. Every string is 1 to 256 bytes of printable ASCII with no
spaces, a name a slug of at most 64, so no field is free text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_TEXT = 256
MAX_NAME = 64
MAX_WINDOW = 60
MAX_P95_MS = 600_000
U32 = 2**32 - 1
#: A monitors job creates at most this many monitors (``SPEC-watch.md``, "Stale monitors").
MAX_NEW_MONITORS = 10
#: Changes and deletions in one result, at most (as Watch's API takes them).
MAX_CHANGES = 64
#: A result file larger than this is refused unread.
MAX_RESULT_BYTES = 256 * 1024

METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
_SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_SHA = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")
_MONITOR_ID = re.compile(r"^mon_[0-9a-f]{16}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


class Refused(ValueError):
    """A result that does not fit its schema: ``field`` is the dotted path, ``problem`` the
    same stable names Watch's API answers with (``unknown_field``, ``not_a_slug``, ...)."""

    def __init__(self, field_path: str, problem: str, message: str = "") -> None:
        # A field path can name a key the session chose: it is cut to 64 characters of a
        # path's alphabet before it goes anywhere (phase 18 review A12).
        field_path = _UNSAFE.sub("?", field_path)[:64]
        super().__init__(f"{field_path or '(the result)'}: {message or problem}")
        self.field = field_path
        self.problem = problem


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


class _Fields:
    """One JSON object's fields, taken one at a time; any other key is refused first."""

    def __init__(self, path: str, v: Any, known: tuple[str, ...]) -> None:
        if not isinstance(v, dict):
            raise Refused(path, "wrong_type", "must be an object")
        for k in v:
            if k not in known:
                raise Refused(_join(path, str(k)), "unknown_field", "no such field")
        self.path = path
        self.map = {k: val for k, val in v.items() if val is not None}

    def field(self, name: str) -> str:
        return _join(self.path, name)

    def take(self, name: str) -> Any:
        return self.map.pop(name, None)

    def need(self, name: str) -> Any:
        v = self.take(name)
        if v is None:
            raise Refused(self.field(name), "missing", "required")
        return v

    def string(self, name: str, v: Any) -> str:
        if not isinstance(v, str):
            raise Refused(self.field(name), "wrong_type", "must be a string")
        return v

    def name(self, name: str) -> str | None:
        v = self.take(name)
        if v is None:
            return None
        s = self.string(name, v)
        if not _is_text(s):
            raise Refused(self.field(name), "bad_text", f"must be 1 to {MAX_TEXT} bytes of printable ASCII with no spaces")
        return s

    def need_name(self, name: str) -> str:
        s = self.name(name)
        if s is None:
            raise Refused(self.field(name), "missing", "required")
        return s

    def one_of(self, name: str, allowed: tuple[str, ...]) -> str | None:
        v = self.take(name)
        if v is None:
            return None
        s = self.string(name, v)
        if s not in allowed:
            raise Refused(self.field(name), "not_one_of", "must be one of " + ", ".join(allowed))
        return s

    def integer(self, name: str, v: Any, lo: int, hi: int) -> int:
        if not _is_int(v) or v < 0:
            raise Refused(self.field(name), "wrong_type", "must be a whole number")
        if not lo <= v <= hi:
            raise Refused(self.field(name), "out_of_range", f"must be {lo} to {hi}")
        return v


def _is_text(s: str) -> bool:
    b = s.encode("utf-8")
    return 0 < len(b) <= MAX_TEXT and all(0x21 <= c <= 0x7E for c in b)


def _variant(path: str, v: Any, names: str) -> tuple[str, Any]:
    if not isinstance(v, dict):
        raise Refused(path, "wrong_type", "must be an object with one key")
    if len(v) != 1:
        raise Refused(path, "not_one_of", "must be one of " + names)
    return next(iter(v.items()))


_STATUS = ("1xx", "2xx", "3xx", "4xx", "5xx")


def _condition(v: Any) -> tuple[str, Any]:
    key, value = _variant("condition", v, "count_over, ratio_over, p95_over, absent")
    path = f"condition.{key}"
    if key == "count_over":
        if not _is_int(value) or value < 0:
            raise Refused(path, "wrong_type", "must be a whole number")
        if value > U32:
            raise Refused(path, "out_of_range", "must be 0 to 4294967295")
    elif key == "ratio_over":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Refused(path, "wrong_type", "must be a number")
        if not 0 < value < 1:
            raise Refused(path, "out_of_range", "must be between 0 and 1, exclusive")
    elif key == "p95_over":
        if not _is_int(value) or value < 0:
            raise Refused(path, "wrong_type", "must be a whole number of milliseconds")
        if not 1 <= value <= MAX_P95_MS:
            raise Refused(path, "out_of_range", "must be 1 to 600000 milliseconds")
    elif key == "absent":
        if value is not True:
            raise Refused(path, "wrong_type", "must be true")
    else:
        raise Refused(path, "unknown_field", "no such field")
    return key, value


def _does_not_fit(condition: str, signal: str) -> Refused:
    return Refused(f"condition.{condition}", "condition_does_not_fit_signal", f"{condition} does not apply to a {signal} signal")


def _signal(v: Any, condition: str) -> None:
    key, value = _variant("signal", v, "request, outbound, error, log")
    path = f"signal.{key}"
    known = {
        "request": ("route", "method", "status_class"),
        "outbound": ("host", "method", "status_class"),
        "error": ("type", "route", "file", "function"),
        "log": ("level", "logger", "file", "function"),
    }.get(key)
    if known is None:
        raise Refused(path, "unknown_field", "no such field")
    f = _Fields(path, value, known)
    if key in ("request", "outbound"):
        if key == "request":
            route = f.need_name("route")
            if not route.startswith("/"):
                raise Refused(f.field("route"), "wrong_type", "must be a route template starting with /")
        else:
            f.need_name("host")
        f.one_of("method", METHODS)
        status = f.one_of("status_class", _STATUS)
        if condition == "ratio_over" and status is None:
            raise Refused(f.field("status_class"), "required_by_condition", "required by ratio_over")
    elif key == "error":
        given = [f.name(n) for n in ("type", "route", "file", "function")]
        if all(g is None for g in given):
            raise Refused(path, "empty", "must name at least one of type, route, file, function")
        if condition != "count_over":
            raise _does_not_fit(condition, "error")
    else:
        level = f.take("level")
        if level is None:
            raise Refused(f.field("level"), "missing", "required")
        if level not in ("warning", "error"):
            raise Refused(f.field("level"), "not_one_of", "must be one of warning, error")
        for n in ("logger", "file", "function"):
            f.name(n)
        if condition not in ("count_over", "absent"):
            raise _does_not_fit(condition, "log")


@dataclass(frozen=True)
class Monitor:
    """A monitor draft that fits the schema, as the JSON object it was parsed from."""

    name: str
    pr: int
    merge_sha: str
    doc: dict[str, Any]


def parse_monitor(v: Any, path: str = "") -> Monitor:
    """Parses one monitor draft, refusing anything outside the schema; ``path`` prefixes
    the field names in refusals."""
    try:
        f = _Fields("", v, ("name", "source", "signal", "condition", "window_minutes"))
        name = f.string("name", f.need("name"))
        if len(name) > MAX_NAME or not _SLUG.match(name):
            raise Refused(
                "name", "not_a_slug", "must be a slug: lowercase letters, digits and single hyphens, 64 characters at most"
            )
        s = _Fields("source", f.need("source"), ("pr", "merge_sha", "file", "function"))
        pr = s.integer("pr", s.need("pr"), 1, U32)
        sha = s.string("merge_sha", s.need("merge_sha"))
        if not _SHA.match(sha):
            raise Refused("source.merge_sha", "wrong_type", "must be a full lowercase commit SHA")
        s.need_name("file")
        s.name("function")
        condition, _ = _condition(f.need("condition"))
        _signal(f.need("signal"), condition)
        f.integer("window_minutes", f.need("window_minutes"), 1, MAX_WINDOW)
    except Refused as r:
        if path:
            raise Refused(_join(path, r.field), r.problem, str(r).split(": ", 1)[-1]) from None
        raise
    return Monitor(name=name, pr=pr, merge_sha=sha, doc=v)


@dataclass(frozen=True)
class MonitorsResult:
    """A monitors job's result: what to create, change (by id) and delete, and the merge
    commit's first-parent position as the session's clone counted it."""

    create: list[Monitor] = field(default_factory=list)
    change: list[tuple[str, Monitor]] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    merge_position: int | None = None

    def outcome(self) -> dict[str, Any]:
        """The body Watch's API takes (``POST /v1/jobs/{id}/outcome``), the lease aside."""
        out: dict[str, Any] = {
            "outcome": "monitors",
            "create": [m.doc for m in self.create],
            "change": [{"id": i, "monitor": m.doc} for i, m in self.change],
            "delete": list(self.delete),
        }
        if self.merge_position is not None:
            out["merge_position"] = self.merge_position
        return out


def _list(f: _Fields, name: str, most: int) -> list[Any]:
    v = f.take(name)
    if v is None:
        return []
    if not isinstance(v, list):
        raise Refused(f.field(name), "wrong_type", "must be a list")
    if len(v) > most:
        raise Refused(f.field(name), "out_of_range", f"at most {most}")
    return v


def _monitor_id(path: str, v: Any) -> str:
    if not isinstance(v, str) or not _MONITOR_ID.match(v):
        raise Refused(path, "wrong_type", "must be a monitor id")
    return v


def parse_monitors_result(v: Any, *, pr: int, merge_sha: str, existing: dict[str, str]) -> MonitorsResult:
    """Parses a monitors job's ``/job/result.json`` against the job it was for.

    The result is ``{"kind": "monitors", "create": [...], "change": [{"id", "monitor"}],
    "delete": [ids], "merge_position": N}``. Besides each monitor fitting the schema:
    every created or changed monitor's source is the job's pull request and merge commit;
    changed and deleted ids are among ``existing`` (id to name) and named once; names are
    unique among the monitors that would be live; at most ten are created.
    """
    f = _Fields("", v, ("kind", "create", "change", "delete", "merge_position"))
    if f.need("kind") != "monitors":
        raise Refused("kind", "not_one_of", "must be monitors")
    create = [parse_monitor(d, f"create.{i}") for i, d in enumerate(_list(f, "create", MAX_NEW_MONITORS))]
    change: list[tuple[str, Monitor]] = []
    for i, c in enumerate(_list(f, "change", MAX_CHANGES)):
        cf = _Fields(f"change.{i}", c, ("id", "monitor"))
        change.append((_monitor_id(cf.field("id"), cf.need("id")), parse_monitor(cf.need("monitor"), cf.field("monitor"))))
    delete = [_monitor_id(f"delete.{i}", d) for i, d in enumerate(_list(f, "delete", MAX_CHANGES))]
    position = f.take("merge_position")
    if position is not None:
        position = f.integer("merge_position", position, 0, 2**62)

    live = dict(existing)
    named = set()
    for path, mid in [(f"change.{i}.id", m) for i, (m, _) in enumerate(change)] + [
        (f"delete.{i}", m) for i, m in enumerate(delete)
    ]:
        if mid in named:
            raise Refused(path, "repeated", "names a monitor twice")
        if mid not in live:
            raise Refused(path, "not_found", "not one of this repository's monitors")
        named.add(mid)
        live.pop(mid)
    names = set(live.values())
    for path, m in [(f"create.{i}", m) for i, m in enumerate(create)] + [
        (f"change.{i}.monitor", m) for i, (_, m) in enumerate(change)
    ]:
        if m.pr != pr or m.merge_sha != merge_sha:
            raise Refused(f"{path}.source", "wrong_source", f"must be pull request {pr} at its merge commit")
        if m.name in names:
            raise Refused(f"{path}.name", "name_taken", "another live monitor has this name")
        names.add(m.name)
    return MonitorsResult(create=create, change=change, delete=delete, merge_position=position)


#: An alert job's outcomes a session may report; ``failed`` and ``budget_spent`` are the
#: runner's own, never taken from a result file.
ALERT_OUTCOMES = ("noise", "already_fixed", "issue", "pr")
NOISE_ACTIONS = ("keep", "mute", "delete")
#: A fix's gate names at most this many test files.
MAX_GATE_TESTS = 16


def _number(f: _Fields, name: str) -> int:
    v = f.take(name)
    if v is None:
        raise Refused(f.field(name), "missing", "required")
    return f.integer(name, v, 1, U32)


def _sha(f: _Fields, name: str) -> str:
    v = f.take(name)
    if not isinstance(v, str) or not _SHA.match(v):
        raise Refused(f.field(name), "wrong_type" if v is not None else "missing", "a full lowercase commit SHA")
    return v


def test_path(v: Any) -> bool:
    """A test file's path as a gate names it: text (``_is_text``), relative, no ``.`` or
    ``..`` component, outside ``.github/`` and ``.clovelly/``."""
    return (
        isinstance(v, str)
        and _is_text(v)
        and not v.startswith(("/", ".github/", ".clovelly/"))
        and all(c not in ("", ".", "..") for c in v.split("/"))
    )


def _gate(v: Any) -> dict[str, Any]:
    g = _Fields("gate", v, ("deployed_sha", "head_sha", "tests", "failed_exit"))
    deployed, head = _sha(g, "deployed_sha"), _sha(g, "head_sha")
    tests = g.take("tests")
    if not isinstance(tests, list) or not 1 <= len(tests) <= MAX_GATE_TESTS:
        raise Refused("gate.tests", "out_of_range", f"a list of 1 to {MAX_GATE_TESTS} test paths")
    for i, t in enumerate(tests):
        if not test_path(t):
            raise Refused(f"gate.tests.{i}", "bad_path", "a relative path of printable ASCII, outside .github/ and .clovelly/")
        if t in tests[:i]:
            raise Refused(f"gate.tests.{i}", "repeated", "named twice")
    failed = g.take("failed_exit")
    if failed is None:
        raise Refused("gate.failed_exit", "missing", "required")
    g.integer("failed_exit", failed, 1, 123)
    if 2 <= failed <= 5:
        raise Refused("gate.failed_exit", "out_of_range", "a test runner's 'a test failed' status: 1, or 6 to 123")
    return {"deployed_sha": deployed, "head_sha": head, "tests": list(tests), "failed_exit": failed}


@dataclass(frozen=True)
class AlertResult:
    """An alert job's result: one of :data:`ALERT_OUTCOMES` and its fields, exactly the
    outcome body Watch's API takes (``vectors/alert-outcomes.json``)."""

    body: dict[str, Any]

    def outcome(self) -> dict[str, Any]:
        return dict(self.body)


def parse_alert_result(v: Any, *, monitor: dict[str, Any] | None = None) -> AlertResult:
    """Parses an alert job's ``/job/result.json``: ``{"kind": "alert", "outcome": ...}`` and
    the outcome's fields, nothing else.

    - ``noise``: ``"monitor"`` is ``keep`` (the default), ``mute``, ``delete`` or
      ``{"replace": draft}``; a replacement's source must be ``monitor``'s own, when given.
    - ``already_fixed``: ``head_sha`` and ``head_position``.
    - ``issue``: ``number``.
    - ``pr``: ``number`` and its ``gate`` (``deployed_sha``, ``head_sha``, ``tests``,
      ``failed_exit`` the test runner's "a test failed" status, 1 or 6 to 123: a gate whose tests
      passed, errored, timed out or were killed on the deployed commit has no form).
    """
    if not isinstance(v, dict):
        raise Refused("", "wrong_type", "must be an object")
    if v.get("kind") != "alert":
        raise Refused("kind", "not_one_of", "must be alert")
    kind = v.get("outcome")
    if kind not in ALERT_OUTCOMES:
        raise Refused("outcome", "not_one_of", "must be one of " + ", ".join(ALERT_OUTCOMES))
    known = {
        "noise": ("monitor",),
        "already_fixed": ("head_sha", "head_position"),
        "issue": ("number",),
        "pr": ("number", "gate"),
    }[kind]
    f = _Fields("", {k: x for k, x in v.items() if k != "kind"}, ("outcome", *known))
    f.take("outcome")
    body: dict[str, Any] = {"outcome": kind}
    if kind == "noise":
        action = f.take("monitor")
        if action is None or action in NOISE_ACTIONS:
            body["monitor"] = action or "keep"
        elif isinstance(action, dict) and len(action) == 1 and "replace" in action:
            m = parse_monitor(action["replace"], "monitor.replace")
            src = (monitor or {}).get("source") or {}
            if monitor is not None and (m.pr, m.merge_sha) != (src.get("pr"), src.get("merge_sha")):
                raise Refused("monitor.replace.source", "wrong_source", "a replacement keeps the monitor's own source")
            body["monitor"] = {"replace": m.doc}
        else:
            raise Refused("monitor", "not_one_of", 'keep, mute, delete or {"replace": monitor}')
    elif kind == "already_fixed":
        body["head_sha"] = _sha(f, "head_sha")
        position = f.take("head_position")
        if position is None:
            raise Refused("head_position", "missing", "required")
        body["head_position"] = f.integer("head_position", position, 0, 2**62)
    elif kind == "issue":
        body["number"] = _number(f, "number")
    else:
        body["number"] = _number(f, "number")
        g = f.take("gate")
        if g is None:
            raise Refused("gate", "missing", "required")
        body["gate"] = _gate(g)
    return AlertResult(body)
