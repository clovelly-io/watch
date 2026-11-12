"""The monitors job (``SPEC-watch.md``, "The monitors job"): a merged pull request's diff
in, monitors for the new code out.

The harness, not the model, clones the repository, finds the merge's diff and its
first-parent position, fills each monitor's ``source`` with the job's pull request and
merge commit, and checks the result against the runner's closed schema before writing
``/job/result.json``. The model reads the diff and the code around it with read-only tools
and answers in a fixed JSON shape (the Agent SDK's structured output). A refusal by the
schema is sent back to it, at most twice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clovelly_watch_runner import schema

from . import agent, gate, git

REPO_DIR = Path("/job/repo")
RESULT = Path("/job/result.json")
POSITION = Path("/job/position.json")
RETRIES = 2

_MONITOR = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "file", "signal", "condition", "window_minutes"],
    "properties": {
        "name": {"type": "string", "maxLength": 64},
        "file": {"type": "string", "maxLength": 256},
        "function": {"type": "string", "maxLength": 256},
        "signal": {"type": "object"},
        "condition": {"type": "object"},
        "window_minutes": {"type": "integer", "minimum": 1, "maximum": 60},
    },
}
#: What the model answers with.
OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["create", "change", "delete"],
    "properties": {
        "create": {"type": "array", "maxItems": schema.MAX_NEW_MONITORS, "items": _MONITOR},
        "change": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "monitor"],
                "properties": {"id": {"type": "string"}, "monitor": _MONITOR},
            },
        },
        "delete": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = """You write monitors for Clovelly Watch. A pull request has just merged into a \
service's default branch. Your job: read what it changed and write monitors that would \
catch the new code failing in production, and update or delete existing monitors whose \
code this pull request changed or removed.

Monitors are rules over metadata the service's SDK already sends; you cannot add \
instrumentation. The schema, exactly (anything else is refused):

- name: a slug of lowercase letters, digits and single hyphens, at most 64 characters, \
unique in the repository.
- file (and optionally function): where the new code is.
- signal: exactly one of
  - {"request": {"route": "/path/{param}", "method"?: "GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS", \
"status_class"?: "1xx".."5xx"}}: the service's own HTTP routes, by route template as the \
framework declares it.
  - {"outbound": {"host": "api.example.com", "method"?, "status_class"?}}: HTTP calls the \
service makes, by the peer's host name.
  - {"error": {"type"?: "ExceptionClass", "route"?, "file"?, "function"?}} (at least one): \
exceptions, counted from exception events and logged exceptions.
  - {"log": {"level": "warning"|"error", "logger"?, "file"?, "function"?}}: log records at \
or above the level; the logger is the logger's name.
- condition: exactly one of {"count_over": N}, {"ratio_over": R} (0 < R < 1, needs a \
status_class; the share of the route's or host's requests in that class), {"p95_over": MS} \
(1 to 600000), {"absent": true}. request and outbound take all four; error takes \
count_over only; log takes count_over and absent.
- window_minutes: 1 to 60.

Every string is 1 to 256 printable ASCII characters with no spaces: no prose anywhere.

Rules:
- Monitor only what this pull request added or changed. Existing code gets no new monitors.
- At most 10 new monitors; most pull requests need one to four. A pull request that \
changes nothing observable (docs, tests, refactors with the same routes) needs none.
- When the pull request removes a route, a call or a code path an existing monitor watches, \
delete that monitor. When it renames or changes one, change the monitor (its new source is \
this pull request). Leave every other existing monitor alone.
- Prefer conditions that mean something is broken: 5xx ratios on new routes, errors in new \
code, failing calls to a new dependency. Avoid thresholds that normal traffic crosses.
- Read the code with your tools when the diff is not enough. Never modify files.

Answer with the JSON object only: {"create": [...], "change": [{"id", "monitor"}], \
"delete": [ids]}. Each monitor is {"name", "file", "function"?, "signal", "condition", \
"window_minutes"}; the harness adds its source."""


def _prompt(job: dict[str, Any], diff: str, files: list[str], cut: bool) -> str:
    pr = job["pull_request"]
    existing = [
        {k: m.get(k) for k in ("id", "name", "source", "signal", "condition", "window_minutes", "state")} for m in job["monitors"]
    ]
    parts = [
        f"Repository: {job['github']}. Pull request #{pr['number']} merged as {pr['merge_sha']}.",
        "The working directory is the repository at that merge commit.",
        f"Files changed: {', '.join(files) or '(none)'}.",
        "Existing monitors (JSON):",
        json.dumps(existing, indent=1),
        "The merge's diff against its first parent" + (" (cut; read the files for the rest):" if cut else ":"),
        diff or "(empty)",
    ]
    return "\n\n".join(parts)


def _drafts(answer: dict[str, Any], pr: int, merge_sha: str) -> dict[str, Any]:
    """The model's answer as the result file: each monitor given its source."""

    def draft(m: Any) -> Any:
        if not isinstance(m, dict):
            return m
        source: dict[str, Any] = {"pr": pr, "merge_sha": merge_sha, "file": m.get("file")}
        if m.get("function"):
            source["function"] = m["function"]
        d = {k: v for k, v in m.items() if k not in ("file", "function")}
        d["source"] = source
        return d

    change = [
        {"id": c.get("id"), "monitor": draft(c.get("monitor"))} if isinstance(c, dict) else c for c in answer.get("change", [])
    ]
    return {
        "kind": "monitors",
        "create": [draft(m) for m in answer.get("create", [])],
        "change": change,
        "delete": answer.get("delete", []),
    }


def run(job: dict[str, Any]) -> int:
    """Runs the job; 0 with ``/job/result.json`` written, 3 when the budget ran out, 1 when
    no valid result came."""
    pr, merge_sha = job["pull_request"]["number"], job["pull_request"]["merge_sha"]
    git.clone(job["github"], REPO_DIR)
    git.checkout(REPO_DIR, merge_sha)
    position = git.first_parent_position(REPO_DIR, merge_sha)
    # Before the model is called, so the runner reports it whatever the job's outcome (review A8).
    POSITION.write_text(json.dumps({"position": position}))
    diff, files, cut = git.merge_diff(REPO_DIR, merge_sha)
    existing = {m["id"]: m["name"] for m in job["monitors"]}

    session = agent.Session(job["gateway"], cwd=REPO_DIR, system=SYSTEM, output_schema=OUTPUT_SCHEMA)
    reply = session.ask(_prompt(job, diff, files, cut))
    for attempt in range(RETRIES + 1):
        if reply.budget_spent:
            print("the job's model budget is spent: ending the session")
            return 3
        if reply.answer is None:
            print(f"no structured answer: {reply.error}")
            return 1
        result = _drafts(reply.answer, pr, merge_sha)
        result["merge_position"] = position
        try:
            schema.parse_monitors_result(result, pr=pr, merge_sha=merge_sha, existing=existing)
        except schema.Refused as r:
            print(f"attempt {attempt + 1}: refused at {r.field}: {r.problem}")
            if attempt == RETRIES:
                return 1
            reply = session.ask(f"Watch's schema refused that answer at {r.field}: {r} . Send the whole corrected answer.")
            continue
        gate.write_result(RESULT, json.dumps(result))
        print(
            f"result: {len(result['create'])} created, {len(result['change'])} changed, "
            f"{len(result['delete'])} deleted; {reply.turns} turns"
        )
        return 0
    return 1
