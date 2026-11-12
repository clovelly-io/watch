"""The runner's own checks: the closed schema against the shared vectors, webhooks, the
configuration, setup, and a job run against an environment whose sandbox is scripted."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clovelly_watch_runner import commands, github, payload, schema
from clovelly_watch_runner.config import ConfigError, load, parse
from clovelly_watch_runner.runner import JOB_FILE, POSITION_FILE, RESULT_FILE, Runner

VECTORS = Path(__file__).resolve().parents[2] / "vectors" / "monitors.json"
ALERT_VECTORS = VECTORS.parent / "alert-outcomes.json"
SHA = "c" * 40


class MonitorVectors(unittest.TestCase):
    def test_the_shared_vectors_hold(self) -> None:
        cases = json.loads(VECTORS.read_text())["cases"]
        self.assertGreater(len(cases), 50)
        for c in cases:
            with self.subTest(c["name"]):
                if c["ok"]:
                    schema.parse_monitor(c["monitor"])
                else:
                    with self.assertRaises(schema.Refused) as r:
                        schema.parse_monitor(c["monitor"])
                    self.assertEqual((r.exception.field, r.exception.problem), (c["field"], c["problem"]))


class AlertVectors(unittest.TestCase):
    def test_the_shared_vectors_hold(self) -> None:
        cases = json.loads(ALERT_VECTORS.read_text())["cases"]
        self.assertGreater(len(cases), 30)
        for c in cases:
            with self.subTest(c["name"]):
                doc = {"kind": "alert", **c["outcome"]}
                if c["ok"]:
                    self.assertEqual(
                        schema.parse_alert_result(doc).outcome(),
                        {"monitor": "keep"} | c["outcome"] if c["outcome"]["outcome"] == "noise" else c["outcome"],
                    )
                else:
                    with self.assertRaises(schema.Refused) as r:
                        schema.parse_alert_result(doc)
                    self.assertEqual(r.exception.field, c["field"])

    def test_the_runners_own_outcomes_never_come_from_a_session(self) -> None:
        for kind in ("failed", "budget_spent", "monitors"):
            with self.assertRaises(schema.Refused):
                schema.parse_alert_result({"kind": "alert", "outcome": kind})

    def test_a_replacement_keeps_the_monitors_source(self) -> None:
        monitor = {"source": {"pr": 7, "merge_sha": SHA, "file": "app.py"}}
        ok = {"kind": "alert", "outcome": "noise", "monitor": {"replace": draft("x")}}
        schema.parse_alert_result(ok, monitor=monitor)
        with self.assertRaises(schema.Refused) as r:
            schema.parse_alert_result({**ok, "monitor": {"replace": draft("x", pr=8)}}, monitor=monitor)
        self.assertEqual(r.exception.problem, "wrong_source")


def draft(name: str, pr: int = 7, sha: str = SHA, route: str = "/r") -> dict[str, Any]:
    return {
        "name": name,
        "source": {"pr": pr, "merge_sha": sha, "file": "app.py"},
        "signal": {"request": {"route": route, "status_class": "5xx"}},
        "condition": {"count_over": 0},
        "window_minutes": 5,
    }


class MonitorsResult(unittest.TestCase):
    existing = {"mon_" + "1" * 16: "old-a", "mon_" + "2" * 16: "old-b"}

    def parse(self, doc: Any) -> schema.MonitorsResult:
        return schema.parse_monitors_result(doc, pr=7, merge_sha=SHA, existing=self.existing)

    def refused(self, doc: Any) -> tuple[str, str]:
        with self.assertRaises(schema.Refused) as r:
            self.parse(doc)
        return r.exception.field, r.exception.problem

    def test_a_good_result_becomes_the_apis_outcome(self) -> None:
        doc = {
            "kind": "monitors",
            "create": [draft("new-a")],
            "change": [{"id": "mon_" + "1" * 16, "monitor": draft("old-a", route="/r2")}],
            "delete": ["mon_" + "2" * 16],
            "merge_position": 12,
        }
        out = self.parse(doc).outcome()
        self.assertEqual(out["outcome"], "monitors")
        self.assertEqual(out["merge_position"], 12)
        self.assertEqual(out["create"], [draft("new-a")])

    def test_anything_outside_the_schema_is_refused_whole(self) -> None:
        good = draft("new-a")
        cases = {
            "extra field": ({"kind": "monitors", "create": [dict(good, note="hi")]}, ("create.0.note", "unknown_field")),
            "extra top-level field": ({"kind": "monitors", "create": [], "summary": "free text"}, ("summary", "unknown_field")),
            "long name": ({"kind": "monitors", "create": [dict(good, name="a" * 65)]}, ("create.0.name", "not_a_slug")),
            "free text": (
                {"kind": "monitors", "create": [dict(good, source={"pr": 7, "merge_sha": SHA, "file": "a b"})]},
                ("create.0.source.file", "bad_text"),
            ),
            "eleven": ({"kind": "monitors", "create": [draft(f"m{i}") for i in range(11)]}, ("create", "out_of_range")),
            "another pull request": ({"kind": "monitors", "create": [draft("x", pr=8)]}, ("create.0.source", "wrong_source")),
            "another merge commit": (
                {"kind": "monitors", "create": [draft("x", sha="d" * 40)]},
                ("create.0.source", "wrong_source"),
            ),
            "a name taken": ({"kind": "monitors", "create": [draft("old-a")]}, ("create.0.name", "name_taken")),
            "two of one name": ({"kind": "monitors", "create": [draft("x"), draft("x")]}, ("create.1.name", "name_taken")),
            "not ours": ({"kind": "monitors", "delete": ["mon_" + "9" * 16]}, ("delete.0", "not_found")),
            "named twice": (
                {
                    "kind": "monitors",
                    "change": [{"id": "mon_" + "1" * 16, "monitor": draft("old-a")}],
                    "delete": ["mon_" + "1" * 16],
                },
                ("delete.0", "repeated"),
            ),
            "a bad id": ({"kind": "monitors", "delete": ["DROP TABLE"]}, ("delete.0", "wrong_type")),
            "another kind": ({"kind": "alert"}, ("kind", "not_one_of")),
            "not an object": ([], ("", "wrong_type")),
        }
        for name, (doc, want) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.refused(doc), want)

    def test_a_deleted_name_can_be_reused_and_a_changed_one_keeps_its_own(self) -> None:
        doc = {
            "kind": "monitors",
            "create": [draft("old-b")],
            "change": [{"id": "mon_" + "1" * 16, "monitor": draft("old-a")}],
            "delete": ["mon_" + "2" * 16],
        }
        self.assertEqual(len(self.parse(doc).create), 1)


class Webhooks(unittest.TestCase):
    def payload(self, **pr: Any) -> bytes:
        base = {"number": 7, "merged": True, "merge_commit_sha": SHA, "base": {"ref": "main"}}
        return json.dumps(
            {"action": "closed", "pull_request": base | pr, "repository": {"full_name": "acme/app", "default_branch": "main"}}
        ).encode()

    def test_the_server_reads_no_body_without_a_sound_length(self) -> None:
        import dataclasses
        import http.client
        import socket

        with tempfile.TemporaryDirectory() as tmp:
            cfg = dataclasses.replace(config(Path(tmp)), listen=("127.0.0.1", 0))
            server = Runner(cfg, Api(), Environment(None), record=lambda r: None).serve_webhooks()
            try:
                port = server.server_address[1]
                for length, want in ((None, 411), ("-1", 400), ("abc", 400), (str(26 * 1024 * 1024), 413)):
                    s = socket.create_connection(("127.0.0.1", port))
                    head = "POST /github/x HTTP/1.1\r\nHost: x\r\n" + (f"Content-Length: {length}\r\n" if length else "") + "\r\n"
                    s.sendall(head.encode())
                    r = http.client.HTTPResponse(s)
                    r.begin()
                    self.assertEqual(r.status, want, length)
                    s.close()
                # A load balancer's health check, and nothing else, is served to a GET.
                for path, want in (("/healthz", 200), ("/github/x", 404), ("/", 404)):
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    c.request("GET", path)
                    self.assertEqual(c.getresponse().status, want, path)
                    c.close()
            finally:
                server.shutdown()

    def test_signatures_are_checked(self) -> None:
        body = self.payload()
        github.verify(b"secret", body, github.sign(b"secret", body))
        for bad in (None, "", github.sign(b"other", body), github.sign(b"secret", body + b" ")):
            with self.assertRaises(github.Rejected):
                github.verify(b"secret", body, bad)

    def test_events_are_parsed_and_narrowed(self) -> None:
        self.assertEqual(github.parse("pull_request", self.payload(), github="acme/app"), github.PullRequestMerged(7, SHA))
        self.assertEqual(github.parse("pull_request", self.payload(merged=False), github="acme/app"), github.PullRequestClosed(7))
        self.assertIsInstance(
            github.parse("pull_request", self.payload(base={"ref": "release"}), github="acme/app"), github.Ignored
        )
        self.assertIsInstance(github.parse("push", self.payload(), github="acme/app"), github.Ignored)
        issue = json.dumps({"action": "closed", "issue": {"number": 3}, "repository": {"full_name": "acme/app"}}).encode()
        self.assertEqual(github.parse("issues", issue, github="acme/app"), github.IssueClosed(3))
        with self.assertRaises(github.Rejected):
            github.parse("pull_request", self.payload(), github="acme/other")
        with self.assertRaises(github.Rejected):
            github.parse("pull_request", self.payload(merge_commit_sha="nope"), github="acme/app")


# ----- a runner against a scripted environment ------------------------------------------------


@dataclass
class Exec:
    exit_code: int
    stdout: bytes = b""
    stderr: bytes = b""


@dataclass
class Receipt:
    final: bool = True
    exit_kind: str = "killed"


@dataclass
class Sandbox:
    result: bytes | None
    position: bytes | None = None
    files_written: dict[str, bytes] = field(default_factory=dict)
    id: str = "sb_1"
    closed: bool = False

    @property
    def files(self) -> Sandbox:
        return self

    def write(self, path: str, data: str | bytes) -> int:
        self.files_written[path] = data.encode() if isinstance(data, str) else data
        return len(self.files_written[path])

    def exec(self, command: list[str], **_: Any) -> Exec:
        if command[:2] == ["head", "-c"]:
            assert command[3] in (RESULT_FILE, POSITION_FILE)
            got = self.result if command[3] == RESULT_FILE else self.position
            if got is None:
                return Exec(1, b"", b"No such file")
            return Exec(0, got[: int(command[2])])
        assert command[:2] == ["sh", "-c"] and command[2].startswith("clovelly-watch-harness run /job/job.json >")
        return Exec(0, b"", b"")

    def close(self) -> Receipt:
        self.closed = True
        return Receipt()


@dataclass
class Environment:
    result: bytes | None
    position: bytes | None = None
    sandboxes: list[Sandbox] = field(default_factory=list)
    sealed: list[tuple[Any, ...]] = field(default_factory=list)

    def sandbox(self, subenv: str, **kw: Any) -> Sandbox:
        assert subenv == "watch" and kw["cpu"] == 2 and kw["memory"] == 4
        self.kwargs = kw
        sb = Sandbox(self.result, self.position)
        self.sandboxes.append(sb)
        return sb

    def put_credential(self, subenv: str, name: str, secret: str, *, adapter: str, destination: str) -> None:
        self.sealed.append((subenv, name, adapter, destination))


class Api:
    def __init__(self, spent: float = 0.0, records: list[dict[str, Any]] | None = None) -> None:
        self.outcomes: list[dict[str, Any]] = []
        self.spent = spent
        self.records = records or []

    def alert_records(self, alert: str) -> list[dict[str, Any]]:
        return self.records

    def renew(self, job: str, lease: str) -> int:
        return 0

    def outcome(self, job: str, lease: str, outcome: dict[str, Any]) -> None:
        self.outcomes.append(outcome)

    def commits(self, repo: str, commits: list[dict[str, Any]]) -> None:
        self.reported_commits = [*getattr(self, "reported_commits", []), *commits]

    def job(self, job: str) -> dict[str, Any]:
        return {"spent_usd": self.spent, "budget_usd": 2.0, "model_usage": []}


def config(tmp: Path) -> Any:
    commands.init(
        tmp,
        repo="repo_" + "a" * 16,
        github="acme/app",
        watch_url="http://watch",
        gateway_url="https://model.example.com",
        verifier_url="https://verifier",
        env_id="acme",
        ca_bundle="ca.pem",
    )
    return load(tmp / "config.json")


JOB = {
    "job": "job_" + "1" * 16,
    "kind": "monitors",
    "lease": "l" * 64,
    "lease_expires_unix": 4102444800,
    "claims": 1,
    "repo": "repo_" + "a" * 16,
    "github": "acme/app",
    "model": {"token": "cwg_" + "0" * 64, "model": "claude-opus-5-5", "budget_usd": 2.0, "spent_usd": 0.0},
    "pull_request": {"number": 7, "merge_sha": SHA},
    "monitors": [],
}


class Jobs(unittest.TestCase):
    def run_job(self, result: bytes | None, spent: float = 0.0) -> tuple[Any, Api, Environment]:
        with tempfile.TemporaryDirectory() as tmp:
            env, api = Environment(result), Api(spent)
            rec = Runner(config(Path(tmp)), api, env, record=lambda r: None).run_job(dict(JOB))  # type: ignore[arg-type]
            return rec, api, env

    def test_work_runs_one_claimed_job_without_waiting_and_none_when_the_queue_is_empty(self) -> None:
        doc = {"kind": "monitors", "create": [], "change": [], "delete": [], "merge_position": 3}

        class Queue(Api):
            def __init__(self, jobs: list[dict[str, Any]]) -> None:
                super().__init__()
                self.jobs, self.waits = jobs, []

            def claim(self, wait: int = 20) -> dict[str, Any] | None:
                self.waits.append(wait)
                return self.jobs.pop(0) if self.jobs else None

        with tempfile.TemporaryDirectory() as tmp:
            api = Queue([dict(JOB)])
            runner = Runner(config(Path(tmp)), api, Environment(json.dumps(doc).encode()), record=lambda r: None)  # type: ignore[arg-type]
            rec = runner.run_one()
            self.assertEqual((rec and rec.outcome, len(api.outcomes)), ("monitors", 1))
            self.assertIsNone(runner.run_one())
            self.assertEqual(api.waits, [0, 0], "an execution never waits for work")

    def test_a_valid_result_is_reported_and_the_sandbox_closed(self) -> None:
        doc = {"kind": "monitors", "create": [draft("new-a")], "change": [], "delete": [], "merge_position": 3}
        rec, api, env = self.run_job(json.dumps(doc).encode())
        self.assertEqual(api.outcomes, [schema.parse_monitors_result(doc, pr=7, merge_sha=SHA, existing={}).outcome()])
        self.assertEqual((rec.outcome, rec.receipt_final, env.sandboxes[0].closed), ("monitors", True, True))
        inputs = json.loads(env.sandboxes[0].files_written[JOB_FILE])
        self.assertEqual(
            inputs["gateway"], {"url": "https://model.example.com", "token": JOB["model"]["token"], "model": "claude-opus-5-5"}
        )
        self.assertNotIn("lease", inputs)

    def test_a_refused_result_reports_failed_with_nothing_of_it(self) -> None:
        bad = {"kind": "monitors", "create": [dict(draft("x"), note="exfiltrated text")]}
        rec, api, _ = self.run_job(json.dumps(bad).encode())
        self.assertEqual(api.outcomes, [{"outcome": "failed"}])
        self.assertEqual(rec.refused, "create.0.note: unknown_field")
        for raw in (b"not json", b"x" * (schema.MAX_RESULT_BYTES + 1)):
            _, api, _ = self.run_job(raw)
            self.assertEqual(api.outcomes, [{"outcome": "failed"}])

    def test_a_session_that_cannot_run_reports_failed(self) -> None:
        class Broken(Environment):
            def sandbox(self, subenv: str, **kw: Any) -> Sandbox:
                raise ConnectionError("no capacity")

        with tempfile.TemporaryDirectory() as tmp:
            api = Api()
            rec = Runner(config(Path(tmp)), api, Broken(None), record=lambda r: None).run_job(dict(JOB))  # type: ignore[arg-type]
        self.assertEqual((api.outcomes, rec.refused), ([{"outcome": "failed"}], "the session could not run: ConnectionError"))

    def test_the_repository_is_the_runners_own(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env, api = Environment(b"{}"), Api()
            rec = Runner(config(Path(tmp)), api, env, record=lambda r: None).run_job(dict(JOB, github="evil/other"))  # type: ignore[arg-type]
        self.assertEqual((api.outcomes, env.sandboxes), ([{"outcome": "failed"}], []))
        self.assertIn("another GitHub repository", rec.refused or "")

    def test_a_field_the_session_named_is_cut_before_it_is_recorded(self) -> None:
        bad = {"kind": "monitors", "create": [dict(draft("x"), **{"exfiltrated payload text " * 10: 1})]}
        rec, _, _ = self.run_job(json.dumps(bad).encode())
        field = (rec.refused or "").split(":")[0]
        self.assertLessEqual(len(field), 64)
        self.assertNotIn(" ", field)

    def test_a_failed_monitors_job_still_reports_the_merges_position(self) -> None:
        # Review A8 (phase 19): the merged fix's monitor re-arms whatever the job's outcome.
        with tempfile.TemporaryDirectory() as tmp:
            env, api = Environment(None, position=b'{"position": 41}'), Api(spent=2.01)
            Runner(config(Path(tmp)), api, env, record=lambda r: None).run_job(dict(JOB))  # type: ignore[arg-type]
        self.assertEqual(api.outcomes, [{"outcome": "budget_spent"}])
        self.assertEqual(api.reported_commits, [{"sha": SHA, "position": 41}])

    def test_no_result_with_the_budget_spent_is_budget_spent(self) -> None:
        _, api, _ = self.run_job(None, spent=2.01)
        self.assertEqual(api.outcomes, [{"outcome": "budget_spent"}])
        _, api, _ = self.run_job(None, spent=0.5)
        self.assertEqual(api.outcomes, [{"outcome": "failed"}])


class Setup(unittest.TestCase):
    def test_init_setup_and_the_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            repo = cfg.repository("repo_" + "a" * 16)
            assert repo is not None
            self.assertEqual(repo.payload_key_file.stat().st_mode & 0o777, 0o600)
            env = Environment(None)
            out = commands.setup(cfg, env, "github_pat_x\n", Path(tmp) / "policy.json")
            self.assertEqual(
                env.sealed, [("watch", "github-git", "basic", "github.com"), ("watch", "github-api", "bearer", "api.github.com")]
            )
            dests = json.loads(out.read_text())["destinations"]
            self.assertEqual([d["host"] for d in dests], ["github.com", "api.github.com", "model.example.com"])
            # path rules hold every destination to this repository and the Messages API
            self.assertEqual(
                [d["paths"] for d in dests], [["/acme/app.git/"], ["/repos/acme/app/"], ["/v1/messages", "/api/hello"]]
            )
            self.assertNotIn("github_pat_x", (Path(tmp) / "config.json").read_text())
            with self.assertRaises(ValueError):
                commands.setup(cfg, env, "two words", Path(tmp) / "p.json")

    def test_a_bad_configuration_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            parse({"watch_url": "x"}, Path("."))
        doc = {
            "watch_url": "w",
            "gateway_url": "g",
            "runner_token_file": "t",
            "environment": {"verifier_url": "v", "env_id": "e", "ca_bundle": "c"},
            "repositories": {"repo_x": {}},
        }
        with self.assertRaises(ConfigError):
            parse(doc, Path("."))


if __name__ == "__main__":
    unittest.main()


# ----- the alert job ----------------------------------------------------------------------

BATCH = json.loads((VECTORS.parent / "batch.json").read_text())
KEY = json.loads((VECTORS.parent / "hpke.json").read_text())["vectors"][0]["skRm"]
DEPLOYED = "d" * 40
ALERT_JOB = dict(
    JOB,
    kind="alert",
    alert={"id": "alt_" + "2" * 16},
    monitor={"id": "mon_" + "3" * 16, "name": "refunds-5xx", "source": {"pr": 7, "merge_sha": SHA, "file": "app.py"}},
    deployed={"sha": "e" * 40, "environment": ""},
)
del ALERT_JOB["pull_request"], ALERT_JOB["monitors"]


def found(i: int) -> dict[str, Any]:
    r = BATCH["records"][i]
    return {
        "signal": "span",
        "attributes": {"http.route": "/refunds"},
        "resource": {payload.RELEASE: DEPLOYED},
        "sealed": r["sealed_hex"],
        "enc": BATCH["enc_hex"],
    }


class AlertJobs(unittest.TestCase):
    def run_job(self, result: dict[str, Any], records: list[dict[str, Any]]) -> tuple[Any, Api, Environment]:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            repo = cfg.repository("repo_" + "a" * 16)
            scalar = base64.urlsafe_b64encode(bytes.fromhex(KEY)).rstrip(b"=").decode()
            repo.payload_key_file.write_text("cwsk1_" + scalar)
            env, api = Environment(json.dumps(result).encode()), Api(records=records)
            rec = Runner(cfg, api, env, record=lambda r: None).run_job(dict(ALERT_JOB))  # type: ignore[arg-type]
            return rec, api, env

    def test_payloads_are_opened_into_the_session_and_a_gated_fix_is_reported(self) -> None:
        gate = {"deployed_sha": DEPLOYED, "head_sha": SHA, "tests": ["tests/test_refunds.py"], "failed_exit": 1}
        result = {"kind": "alert", "outcome": "pr", "number": 12, "gate": gate}
        bad = dict(found(1), sealed="00" * 40)
        rec, api, env = self.run_job(result, [found(0), bad])
        inputs = json.loads(env.sandboxes[0].files_written[JOB_FILE])
        self.assertEqual(inputs["deployed_sha"], DEPLOYED, "the records' release, not Watch's guess")
        self.assertEqual(inputs["records"][0]["payload"], BATCH["records"][0]["payload"])
        self.assertTrue(inputs["records"][1]["payload_error"].startswith("does not open"))
        self.assertNotIn("sealed", inputs["records"][0])
        self.assertEqual(api.outcomes, [{"outcome": "pr", "number": 12, "gate": gate}])
        self.assertEqual(rec.outcome, "pr")

    def test_with_no_records_the_release_watch_saw_is_used(self) -> None:
        _, api, env = self.run_job({"kind": "alert", "outcome": "noise", "monitor": "mute"}, [])
        self.assertEqual(json.loads(env.sandboxes[0].files_written[JOB_FILE])["deployed_sha"], "e" * 40)
        self.assertEqual(api.outcomes, [{"outcome": "noise", "monitor": "mute"}])

    def test_a_result_with_free_text_reports_failed(self) -> None:
        rec, api, _ = self.run_job({"kind": "alert", "outcome": "issue", "number": 3, "title": "exfiltrated"}, [])
        self.assertEqual((api.outcomes, rec.refused), ([{"outcome": "failed"}], "title: unknown_field"))


class CrossClaim(unittest.TestCase):
    """Phase 20 criterion 4, as far as a runner alone can show it: a job of one hosted organization
    forced on another's runner opens none of the first's payloads and reaches none of its
    credentials (they are sealed to the first's sub-environment, which this runner never uses)."""

    def test_a_forced_cross_claim_opens_nothing(self) -> None:
        gate = {"deployed_sha": DEPLOYED, "head_sha": SHA, "tests": ["tests/test_refunds.py"], "failed_exit": 1}
        result = {"kind": "alert", "outcome": "pr", "number": 12, "gate": gate}
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            repo = cfg.repository("repo_" + "a" * 16)
            # This runner's own key: the records below were sealed to another organization's.
            repo.payload_key_file.write_text(payload.generate().text())
            env, api = Environment(json.dumps(result).encode()), Api(records=[found(0), found(1)])
            Runner(cfg, api, env, record=lambda r: None).run_job(dict(ALERT_JOB))  # type: ignore[arg-type]
            inputs = json.loads(env.sandboxes[0].files_written[JOB_FILE])
        self.assertTrue(all(r["payload_error"].startswith("does not open") for r in inputs["records"]))
        self.assertTrue(all(r.get("payload") is None for r in inputs["records"]), "no plaintext of the other organization's")
        # Another organization's repository is refused before any sandbox (and so any credential).
        with tempfile.TemporaryDirectory() as tmp:
            env, api = Environment(b"{}"), Api()
            rec = Runner(config(Path(tmp)), api, env, record=lambda r: None).run_job(dict(ALERT_JOB, github="other/org"))  # type: ignore[arg-type]
        self.assertEqual((env.sandboxes, api.outcomes), ([], [{"outcome": "failed"}]))
        self.assertIn("another GitHub repository", rec.refused or "")

    def test_a_hosted_token_opens_only_with_the_provisioners_key(self) -> None:
        v = json.loads((VECTORS.parent / "hosted-token.json").read_text())
        self.assertEqual(payload.open_hosted(payload.PrivateKey.parse(v["private"]), v["sealed"]), v["plaintext"].encode())
        with self.assertRaises(Exception):  # noqa: B017 (any failure to open is the point)
            payload.open_hosted(payload.generate(), v["sealed"])
