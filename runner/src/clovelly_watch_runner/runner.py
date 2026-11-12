"""The runner (``SPEC-watch.md``, "The runner"): GitHub's webhooks in, jobs claimed from
Watch under a lease, each job run in a sandbox of the customer's Watch sub-environment
through the published sandbox SDK, and only a result that fits its closed schema passed
on.

How a job runs (the monitors job, phase 18; the alert job, phase 19; see ``JOBS``):

1. Claimed with a ten-minute lease, renewed every third of it on a thread of its own.
   The claim carries a token for the model gateway, good for this claim only.
2. A sandbox from the repository's template, 2 vCPU and 4 GiB, timed out at the job's
   limit. The runner writes ``/job/job.json`` (the job's inputs, the gateway's address and
   token) and runs ``clovelly-watch-harness run /job/job.json`` in it. An alert job's
   inputs include the alert's records with their payloads opened here, with the
   repository's payload key: the plaintext goes into the sandbox and nowhere else.
3. The runner reads ``/job/result.json`` (256 KiB at most), parses it with the job kind's
   schema (:mod:`.schema`) against the job it was for, closes the sandbox and keeps its
   final receipt.
4. A result that fits is reported as the job's outcome. Anything else reports
   ``budget_spent`` if the gateway counted the job's budget spent, ``failed`` otherwise:
   nothing of a refused result reaches Watch.

The runner holds the sandbox's access key in memory only, never the GitHub token (the
egress proxy substitutes it) and never the model key (the gateway holds it).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import github, payload, schema
from .api import ApiError, LeaseLost, WatchApi
from .config import Config, Repository

log = logging.getLogger("clovelly_watch_runner")

JOB_FILE = "/job/job.json"
RESULT_FILE = "/job/result.json"
#: A monitors job's merge commit's first-parent position, written by the harness before the
#: model is called, so a merged fix's monitor re-arms whatever the job's outcome (phase 19
#: review A8).
POSITION_FILE = "/job/position.json"
#: The harness's output stays in the sandbox: the runner records its exit status only.
LOG_FILE = "/job/harness.log"
HARNESS = "clovelly-watch-harness"
#: What the harness's exit statuses mean, recorded instead of anything it printed (phase 18
#: review A12: a session's text never reaches the runner's log).
HARNESS_EXITS = {0: "result written", 1: "no valid result", 3: "the model budget was spent", 124: "timed out"}
#: Webhook deliveries handled at once; more are answered 503.
WEBHOOKS_AT_ONCE = 32
MAX_WEBHOOK_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class JobKind:
    """How one kind of job runs: its session's time limit, how its result is parsed and
    what it needs besides the claim. ``parse`` takes the result file's JSON and the claimed
    job, and returns the outcome body for Watch's API or raises :class:`schema.Refused`;
    ``inputs`` takes Watch's API, the claim and the repository and returns more inputs."""

    timeout_secs: int
    parse: Callable[[Any, dict[str, Any]], dict[str, Any]]
    inputs: Callable[[WatchApi, dict[str, Any], Repository], dict[str, Any]] | None = None


def _monitors_outcome(doc: Any, job: dict[str, Any]) -> dict[str, Any]:
    pr = job["pull_request"]
    existing = {m["id"]: m["name"] for m in job["monitors"]}
    return schema.parse_monitors_result(doc, pr=pr["number"], merge_sha=pr["merge_sha"], existing=existing).outcome()


def _alert_outcome(doc: Any, job: dict[str, Any]) -> dict[str, Any]:
    return schema.parse_alert_result(doc, monitor=job["monitor"]).outcome()


def _alert_inputs(api: WatchApi, job: dict[str, Any], repo: Repository) -> dict[str, Any]:
    """The alert's records with their payloads opened, and the commit to reproduce on: the
    release most of the records carry, else the one Watch saw running when the alert
    opened (an ``absent`` alert has no records)."""
    key = payload.PrivateKey.parse(repo.payload_key_file.read_text())
    records = [payload.open_found(key, r) for r in api.alert_records(job["alert"]["id"])]
    seen = Counter(r["resource"].get(payload.RELEASE) for r in records if isinstance(r.get("resource"), dict))
    seen.pop(None, None)
    deployed = seen.most_common(1)[0][0] if seen else (job.get("deployed") or {}).get("sha")
    return {"records": records, "deployed_sha": deployed}


#: Each job kind Watch hands out, with the harness's command for it.
JOBS: dict[str, JobKind] = {
    "monitors": JobKind(timeout_secs=15 * 60, parse=_monitors_outcome),
    "alert": JobKind(timeout_secs=60 * 60, parse=_alert_outcome, inputs=_alert_inputs),
}

#: A session's size (``SPEC-watch.md``, "The template and the two jobs").
SESSION_CPU = 2
SESSION_MEMORY_GIB = 4


@dataclass
class JobRecord:
    """What the runner records of one job it ran, one JSON line in its log."""

    job: str
    kind: str
    repo: str
    claims: int
    outcome: str = "failed"
    refused: str | None = None
    sandbox: str | None = None
    receipt_final: bool | None = None
    receipt_exit: str | None = None
    harness_exit: int | None = None
    #: What the harness's exit status means (``HARNESS_EXITS``), never its output.
    harness_said: str | None = None
    started_unix: float = 0.0
    session_secs: float = 0.0
    spent_usd: float | None = None
    model_usage: list[dict[str, Any]] | None = None


class Lease:
    """Renews a claimed job's lease until stopped; ``lost`` is set if a renewal is refused."""

    def __init__(self, api: WatchApi, job: str, lease: str, expires_unix: int) -> None:
        self.api, self.job, self.lease = api, job, lease
        self.lost = threading.Event()
        self._stop = threading.Event()
        period = max(5.0, (expires_unix - time.time()) / 3)
        self._thread = threading.Thread(target=self._run, args=(period,), daemon=True)
        self._thread.start()

    def _run(self, period: float) -> None:
        while not self._stop.wait(period):
            try:
                self.api.renew(self.job, self.lease)
            except LeaseLost:
                log.warning("job %s: lease lost", self.job)
                self.lost.set()
                return
            except (ApiError, OSError) as e:
                log.warning("job %s: renewal failed, retrying: %s", self.job, e)

    def stop(self) -> None:
        self._stop.set()


class Runner:
    """``environment`` is the customer's environment from the sandbox SDK
    (``clovelly.Environment``); ``record`` receives each finished job's record."""

    def __init__(self, cfg: Config, api: WatchApi, environment: Any, record: Callable[[JobRecord], None] | None = None) -> None:
        self.cfg = cfg
        self.api = api
        self.environment = environment
        self.record = record or (lambda r: log.info("job %s", json.dumps(r.__dict__)))
        self._stop = threading.Event()

    # ----- webhooks -----------------------------------------------------------------------

    def webhook(self, path: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
        """Handles one delivery to ``/github/<repo id>``: verified, parsed, and reported to
        Watch. Returns the status and a short reason."""
        repo_id = path.rstrip("/").rpartition("/")[2]
        repo = self.cfg.repository(repo_id) if path.startswith("/github/") else None
        if repo is None:
            return 404, "no such repository"
        try:
            github.verify(repo.webhook_secret(), body, headers.get("x-hub-signature-256"))
            event = github.parse(headers.get("x-github-event", ""), body, github=repo.github)
        except github.Rejected as e:
            log.warning("webhook %s refused: %s", repo_id, e)
            return 401, str(e)
        try:
            if isinstance(event, github.PullRequestMerged):
                answer = self.api.pull_request_merged(repo.id, event.number, event.merge_sha)
                job = answer.get("monitors_job")
                log.info("pull request %s merged at %s: monitors job %s", event.number, event.merge_sha, job)
                # GitHub keeps the answer with the delivery: the job it queued, if this did.
                return 202, f"monitors job {job or 'already queued'}"
            elif isinstance(event, github.PullRequestClosed):
                self.api.pull_request_closed(repo.id, event.number)
            elif isinstance(event, github.IssueClosed):
                self.api.issue_closed(repo.id, event.number)
            else:
                return 202, event.reason
        except (ApiError, OSError) as e:
            # GitHub redelivers on a non-2xx answer; Watch's calls are idempotent.
            log.warning("webhook %s: Watch did not take it: %s", repo_id, e)
            return 503, "try again"
        return 202, type(event).__name__

    def serve_webhooks(self) -> ThreadingHTTPServer:
        runner = self
        slots = threading.BoundedSemaphore(WEBHOOKS_AT_ONCE)

        class Handler(BaseHTTPRequestHandler):
            # A slow or stalled client is dropped (phase 18 review A8).
            timeout = 30

            def answer(self, status: int, reason: str) -> None:
                self.send_response(status)
                self.send_header("content-type", "text/plain")
                self.end_headers()
                self.wfile.write(reason.encode() + b"\n")

            def do_GET(self) -> None:  # noqa: N802 (the http.server name)
                # A load balancer's health check: the process answers. Nothing else is served.
                if self.path == "/healthz":
                    return self.answer(200, "ok")
                return self.answer(404, "not found")

            def do_POST(self) -> None:  # noqa: N802 (the http.server name)
                # The body's length is read strictly, before anything is read of it.
                given = self.headers.get("content-length")
                if given is None:
                    return self.answer(411, "a Content-Length is required")
                if not given.isdigit():
                    return self.answer(400, "a bad Content-Length")
                if int(given) > MAX_WEBHOOK_BYTES:
                    return self.answer(413, "too large")
                if not slots.acquire(blocking=False):
                    return self.answer(503, "busy; try again")
                try:
                    body = self.rfile.read(int(given))
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    self.answer(*runner.webhook(self.path, headers, body))
                finally:
                    slots.release()

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug(fmt, *args)

        server = ThreadingHTTPServer(self.cfg.listen, Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    # ----- jobs ---------------------------------------------------------------------------

    def run_forever(self) -> None:
        """Claims and runs jobs, ``sessions`` at a time, until :meth:`stop`."""
        slots = threading.Semaphore(self.cfg.sessions)
        with ThreadPoolExecutor(self.cfg.sessions) as pool:
            while not self._stop.is_set():
                slots.acquire()
                try:
                    job = self.api.claim()
                except (ApiError, OSError) as e:
                    slots.release()
                    log.warning("claim failed: %s", e)
                    self._stop.wait(5)
                    continue
                if job is None:
                    slots.release()
                    continue

                def run(j: dict[str, Any] = job) -> None:
                    try:
                        self.run_job(j)
                    except Exception:  # a job that failed to report is logged, not lost unseen
                        log.exception("job %s: the runner failed", j.get("job"))
                    finally:
                        slots.release()

                pool.submit(run)

    def run_one(self) -> JobRecord | None:
        """Claims one job, without waiting, and runs it to its outcome: an execution of a
        hosted runner job (``work``), which then exits. None when there was nothing to claim."""
        job = self.api.claim(wait=0)
        return None if job is None else self.run_job(job)

    def stop(self) -> None:
        self._stop.set()

    def wait_until_stopped(self) -> None:
        self._stop.wait()

    def run_job(self, job: dict[str, Any]) -> JobRecord:
        """Runs one claimed job to its outcome and returns what was recorded."""
        rec = JobRecord(job=job["job"], kind=job["kind"], repo=job["repo"], claims=job["claims"], started_unix=time.time())
        kind = JOBS.get(job["kind"])
        repo = self.cfg.repository(job["repo"])
        lease = Lease(self.api, job["job"], job["lease"], job["lease_expires_unix"])
        try:
            if kind is None or repo is None:
                rec.refused = "this runner does not run that kind of job or repository"
                outcome = {"outcome": "failed"}
            elif job.get("github") != repo.github:
                # The repository is this runner's own, never Watch's say (review A12).
                rec.refused = "Watch named another GitHub repository than this runner's configuration"
                outcome = {"outcome": "failed"}
            else:
                try:
                    outcome = self._session(job, kind, repo, rec)
                except Exception as e:  # noqa: BLE001 (a sandbox that cannot be made or run ends the job failed: review A7)
                    rec.refused = f"the session could not run: {type(e).__name__}"
                    log.warning("job %s: %s", job["job"], rec.refused)
                    outcome = {"outcome": "failed"}
            if lease.lost.is_set():
                rec.outcome = "lease_lost"
            else:
                try:
                    self.api.outcome(job["job"], job["lease"], outcome)
                    rec.outcome = outcome["outcome"]
                except LeaseLost:
                    rec.outcome = "lease_lost"
                except ApiError as e:
                    # Watch refused the outcome (it re-checks what this runner checked): the
                    # job is still held; report it failed so it is not left to lapse.
                    rec.refused = f"Watch refused the outcome: {e}"
                    self.api.outcome(job["job"], job["lease"], {"outcome": "failed"})
                    rec.outcome = "failed"
        finally:
            lease.stop()
            try:
                usage = self.api.job(job["job"])
                rec.spent_usd, rec.model_usage = usage["spent_usd"], usage["model_usage"]
            except (ApiError, OSError):
                pass
            self.record(rec)
        return rec

    def _report_position(self, sb: Any, job: dict[str, Any], repo: Repository) -> None:
        """Reports the merge commit's position the harness wrote, if it did: a monitors job that
        fails or runs out of budget still lets a merged fix's monitor re-arm (review A8)."""
        try:
            got = sb.exec(["head", "-c", "64", POSITION_FILE], timeout=60)
            position = json.loads(got.stdout)["position"] if got.exit_code == 0 else None
        except Exception as e:  # noqa: BLE001 (no position: nothing to report)
            log.info("job %s: no position file: %s", job["job"], type(e).__name__)
            return
        if not isinstance(position, int) or isinstance(position, bool) or not 0 <= position < 2**62:
            return
        try:
            self.api.commits(repo.id, [{"sha": job["pull_request"]["merge_sha"], "position": position}])
        except (ApiError, OSError) as e:
            log.warning("job %s: the merge's position was not recorded: %s", job["job"], e)

    def _session(self, job: dict[str, Any], kind: JobKind, repo: Repository, rec: JobRecord) -> dict[str, Any]:
        """Runs the harness in a fresh sandbox; the outcome body for Watch's API."""
        inputs = {
            "kind": job["kind"],
            "job": job["job"],
            "github": repo.github,
            "gateway": {"url": self.cfg.gateway_url, "token": job["model"]["token"], "model": job["model"]["model"]},
            "deadline_unix": int(time.time()) + kind.timeout_secs - 60,
        }
        inputs.update({k: job[k] for k in ("pull_request", "monitors", "alert", "monitor") if k in job})
        if kind.inputs is not None:
            inputs.update(kind.inputs(self.api, job, repo))
        started = time.monotonic()
        sb = self.environment.sandbox(
            self.cfg.environment.subenv,
            template=repo.template,
            timeout=kind.timeout_secs,
            cpu=SESSION_CPU,
            memory=SESSION_MEMORY_GIB,
        )
        rec.sandbox = sb.id
        raw: bytes | None = None
        try:
            sb.files.write(JOB_FILE, json.dumps(inputs))
            # The harness's output goes to a file in the sandbox, so none of it is read back
            # (review A6, A12); the result is read with a bound, so a huge file is refused
            # without being read whole.
            run = f"{HARNESS} run {JOB_FILE} > {LOG_FILE} 2>&1"
            result = sb.exec(["sh", "-c", run], cwd="/job", timeout=kind.timeout_secs - 30)
            rec.harness_exit = result.exit_code
            rec.harness_said = HARNESS_EXITS.get(result.exit_code, "ended otherwise")
            try:
                got = sb.exec(["head", "-c", str(schema.MAX_RESULT_BYTES + 1), RESULT_FILE], timeout=60)
                raw = got.stdout if got.exit_code == 0 else None
            except Exception as e:  # noqa: BLE001 (any SDK error means there is no result)
                log.info("job %s: no result file: %s", job["job"], type(e).__name__)
            if job["kind"] == "monitors":
                self._report_position(sb, job, repo)
        finally:
            receipt = sb.close()
            rec.receipt_final, rec.receipt_exit = receipt.final, receipt.exit_kind
            rec.session_secs = time.monotonic() - started
        if raw is not None:
            try:
                if len(raw) > schema.MAX_RESULT_BYTES:
                    raise schema.Refused("", "too_large", f"over {schema.MAX_RESULT_BYTES} bytes")
                try:
                    doc = json.loads(raw)
                except ValueError:
                    raise schema.Refused("", "not_json", "the result is not JSON") from None
                return kind.parse(doc, job)
            except schema.Refused as r:
                rec.refused = f"{r.field}: {r.problem}"
                log.warning("job %s: result refused: %s", job["job"], r)
        else:
            rec.refused = "no result"
        # The harness ends with 3 when the gateway refused at the budget, which it does
        # before the spend reaches it (each request's worst case is reserved).
        if rec.harness_exit == 3:
            return {"outcome": "budget_spent"}
        try:
            spent = self.api.job(job["job"])
            if spent["spent_usd"] >= spent["budget_usd"]:
                return {"outcome": "budget_spent"}
        except (ApiError, OSError):
            pass
        return {"outcome": "failed"}
