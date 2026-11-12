"""The harness's own logic, without a model: what it hands Claude Code, and what it makes of
the model's answer before the runner sees it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from clovelly_watch_runner import schema

from clovelly_watch_harness import agent, alert, gate, monitors

SHA = "e" * 40


class Environment(unittest.TestCase):
    def test_claude_code_reaches_only_the_gateway_with_the_job_token(self) -> None:
        inherited = {"ANTHROPIC_API_KEY": "sk-ant-from-the-image", "CLAUDE_CODE_USE_BEDROCK": "1", "PATH": "/usr/bin"}
        with mock.patch.dict(os.environ, inherited, clear=True):
            env = agent.environment(
                {"url": "https://model.clovelly.io", "token": "cwg_" + "1" * 64, "model": "claude-opus-5-5"}, Path("/job/.claude")
            )
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://model.clovelly.io")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "cwg_" + "1" * 64)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
        for alias in ("OPUS", "SONNET", "HAIKU"):
            self.assertEqual(env[f"ANTHROPIC_DEFAULT_{alias}_MODEL"], "claude-opus-5-5")
        for off in (
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
            "DISABLE_TELEMETRY",
            "DISABLE_ERROR_REPORTING",
            "DISABLE_AUTOUPDATER",
        ):
            self.assertEqual(env[off], "1")
        self.assertEqual(env["PATH"], "/usr/bin")


class Answers(unittest.TestCase):
    def answer(self, **extra: object) -> dict:
        m = {
            "name": "refunds-5xx",
            "file": "app.py",
            "function": "create_refund",
            "signal": {"request": {"route": "/refunds", "status_class": "5xx"}},
            "condition": {"count_over": 0},
            "window_minutes": 5,
        }
        return {"create": [m | extra], "change": [], "delete": []}

    def test_the_harness_fills_the_source_and_the_result_fits_the_schema(self) -> None:
        result = monitors._drafts(self.answer(), 12, SHA)
        self.assertEqual(
            result["create"][0]["source"], {"pr": 12, "merge_sha": SHA, "file": "app.py", "function": "create_refund"}
        )
        schema.parse_monitors_result(result, pr=12, merge_sha=SHA, existing={})

    def test_the_models_own_source_is_replaced_and_extra_fields_are_refused(self) -> None:
        result = monitors._drafts(self.answer(source={"pr": 99, "merge_sha": "f" * 40, "file": "x"}), 12, SHA)
        self.assertEqual(result["create"][0]["source"]["pr"], 12)
        with self.assertRaises(schema.Refused):
            schema.parse_monitors_result(
                monitors._drafts(self.answer(note="free text"), 12, SHA), pr=12, merge_sha=SHA, existing={}
            )


class Gate(unittest.TestCase):
    def test_ci_and_watch_configuration_are_forbidden(self) -> None:
        for p in (".github/workflows/ci.yml", ".clovelly/watch.toml", ".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml"):
            self.assertTrue(gate.forbidden(p), p)
        for p in ("app.py", "tests/test_app.py", "docs/github.md", "src/.githubish/x.py"):
            self.assertFalse(gate.forbidden(p), p)

    def test_a_patch_takes_only_named_regular_files_inside_the_tree(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tree = Path(d)
            (tree / "tests").mkdir()
            (tree / "tests" / "test_a.py").write_text("def test_a(): assert False\n")
            (tree / "app.py").write_text("x = 1\n")
            (tree / "link.py").symlink_to("/etc/passwd")
            p = gate.Patch.take(tree, ["tests/test_a.py"], ["app.py", "gone.py"])
            self.assertEqual(p.fix, {"app.py": b"x = 1\n", "gone.py": None})
            for tests, fix in (
                ([], ["app.py"]),
                (["tests/test_a.py"], [".github/workflows/ci.yml"]),
                (["tests/test_a.py"], ["../outside.py"]),
                (["tests/test_a.py"], ["/job/repo/app.py"]),
                (["tests/test_a.py"], ["link.py"]),
                (["tests/missing.py"], ["app.py"]),
                (["tests/test_a.py"], ["tests/test_a.py"]),
                # Review A2: a fix touches no test and nothing that decides how tests run.
                (["tests/test_a.py"], ["tests/test_b.py"]),
                (["tests/test_a.py"], ["conftest.py"]),
                (["tests/test_a.py"], ["src/conftest.py"]),
                (["tests/test_a.py"], ["pyproject.toml"]),
                (["tests/test_a.py"], ["package.json"]),
                (["tests/test_a.py"], ["vitest.config.ts"]),
                (["tests/test_a.py"], ["lib/x.spec.js"]),
                (["tests/conftest.py"], ["app.py"]),
            ):
                with self.assertRaises(gate.Refused, msg=f"{tests} {fix}"):
                    gate.Patch.take(tree, tests, fix)

    def test_only_the_runners_failed_status_shows_a_bug(self) -> None:
        # Review A1: pytest's 2 to 5, the gate's 124 to 127 and signals are errors, not failures.
        self.assertTrue(all(gate.failure_status(n) for n in (1, 6, 123)))
        self.assertFalse(any(gate.failure_status(n) for n in (0, 2, 3, 4, 5, 124, 125, 126, 127, 128, 255)))
        for before, passed in ((1, True), (2, False), (5, False), (124, False), (0, False)):
            out = gate.Outcome(before=gate.Run(before, ""), after=gate.Run(0, ""), suite=gate.Run(0, ""))
            self.assertEqual(out.passed, passed, before)
        self.assertIn("not the test runner's", gate.Outcome(before=gate.Run(2, "")).why())

    def test_each_test_file_runs_alone_and_an_error_beats_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tree = Path(d)
            for name, code in (("fails.sh", 1), ("passes.sh", 0), ("errors.sh", 2)):
                (tree / name).write_text(f"exit {code}\n")
            run = lambda tests: gate.run_each("sh", tree, tests, 1).exit  # noqa: E731
            self.assertEqual(run(["fails.sh", "passes.sh"]), 1)
            self.assertEqual(run(["passes.sh"]), 0)
            self.assertEqual(run(["fails.sh", "errors.sh"]), 2)

    def test_guarded_files_are_compared_by_their_bytes_not_by_git(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tree = Path(d)
            (tree / ".github" / "workflows").mkdir(parents=True)
            (tree / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
            (tree / "pyproject.toml").write_text("[tool.pytest]\n")
            (tree / "app.py").write_text("x = 1\n")
            held = gate.guarded_files(tree)
            self.assertEqual(sorted(held), [".github/workflows/ci.yml", "pyproject.toml"])
            (tree / "app.py").write_text("x = 2\n")
            self.assertEqual(gate.changed_guarded(held, tree), [])
            (tree / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '-k not refunds'\n")
            (tree / "sub").mkdir()
            (tree / "sub" / "conftest.py").write_text("")
            (tree / ".github" / "workflows" / "ci.yml").unlink()
            self.assertEqual(gate.changed_guarded(held, tree), [".github/workflows/ci.yml", "pyproject.toml", "sub/conftest.py"])

    def test_nothing_goes_out_with_the_token_or_a_placeholder(self) -> None:
        # Review A10.
        gate.screen_out("cwg_secret", "a title", b"def test(): pass\n", None)
        for leak in ("x cwg_secret y", b"token=cwg_other", "clovelly://cred/github-git"):
            with self.assertRaises(gate.Refused):
                gate.screen_out("cwg_secret", "fine", leak)

    def test_a_run_sees_a_fresh_home_and_no_path_the_session_writes(self) -> None:
        # Phase 20 review A9: nothing the session left in its home or /tmp runs in the gate.
        self.assertEqual(
            gate.clean_path("/home/watch/.local/bin:/tmp/x:bin:/usr/local/bin:/usr/bin", "/home/watch"), "/usr/local/bin:/usr/bin"
        )
        with tempfile.TemporaryDirectory() as d:
            tree = Path(d)
            (tree / "env.sh").write_text(
                'printf "%s|%s|%s|%s" "$HOME" "$PYTHONNOUSERSITE" "$npm_config_userconfig" "${ANTHROPIC_API_KEY:-}"\n'
            )
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "HOME": "/home/watch"}):
                out = gate.run("sh", tree, ["env.sh"]).tail.split("|")
            self.assertTrue(out[0].startswith(tempfile.gettempdir().rstrip("/")) or "watch-gate-home-" in out[0])
            self.assertEqual(out[1:], ["1", "/dev/null", ""])
            self.assertFalse(Path(out[0]).exists(), "the home is removed after the run")

    def test_setup_files_and_compile_configuration_are_guarded(self) -> None:
        # Review A11.
        for p in (
            "src/setupTests.ts",
            "jest.setup.js",
            "vitest.setup.ts",
            "tsconfig.json",
            "web/tsconfig.build.json",
            "babel.config.js",
            ".babelrc",
            ".swcrc",
            ".npmrc",
            ".yarnrc.yml",
            "bunfig.toml",
            "vitest.workspace.ts",
        ):
            self.assertTrue(gate.runner_config(p), p)
        self.assertFalse(gate.runner_config("src/setup.ts"))

    def test_a_machine_report_tells_a_failed_assertion_from_a_file_that_did_not_load(self) -> None:
        # Review A10: Jest and Vitest exit 1 for both; their JSON report does not.
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / "r.json"
            ran = gate.Run(1, "")
            judge = lambda doc: (report.write_text(json.dumps(doc)), gate.from_report(ran, report, 1).exit)[1]  # noqa: E731
            failed = {
                "numFailedTests": 1,
                "numRuntimeErrorTestSuites": 0,
                "testResults": [{"status": "failed", "assertionResults": [{"status": "failed"}]}],
            }
            not_loaded = {
                "numFailedTests": 0,
                "numRuntimeErrorTestSuites": 1,
                "testResults": [{"status": "failed", "assertionResults": []}],
            }
            vitest_import = {
                "numFailedTests": 0,
                "testResults": [{"status": "failed", "message": "Failed to load", "assertionResults": []}],
            }
            self.assertEqual(judge(failed), 1)
            self.assertEqual(judge(not_loaded), gate.LOAD_ERROR)
            self.assertEqual(judge(vitest_import), gate.LOAD_ERROR)
            self.assertEqual(judge({"numFailedTests": 0, "testResults": []}), gate.LOAD_ERROR, "exit 1 with no failed test")
            self.assertEqual(gate.from_report(ran, Path(d) / "none.json", 1).exit, gate.LOAD_ERROR, "no report")
        self.assertIsNone(gate.strength("python -m pytest -q", None))
        self.assertIsNone(gate.strength("npx jest", "jest-json"))
        self.assertIn("same exit status", gate.strength("npx jest", None) or "")
        self.assertIn("same exit status", gate.strength("cargo test", None) or "")

    def test_a_payloads_runs_values_and_numbers_are_found_in_what_goes_out(self) -> None:
        # The payload rule (5A), as the fix round's A13 shaped it.
        records = [
            {
                "payload": {
                    "name": "POST /refunds/{refund_id}/cancel",
                    "body": {"note": "Parcel arrived CRUSHED, wants all of it back", "email": "ann@example.org"},
                    "account": 4111222233334444,
                    "at": "2026-11-11T10:00:00Z request 3f2b1c4d-aaaa-bbbb-cccc-1234567890ab",
                    "sent": 1794441785123,
                    "maria.lopez@example.org": "keyed by address",
                }
            }
        ]
        texts = lambda *t: {m.text for m in gate.payload_matches(records, *t)}  # noqa: E731
        self.assertEqual(texts('note = "a made-up note of the same length, okay"\n'), set())
        self.assertTrue(texts('note = "parcel   arrived crushed, wants all of it"'), "case and whitespace do not hide it")
        self.assertTrue(texts('note = "Parcel arrived\\u0020crushed, wants all of it"'), "nor an escape")
        self.assertEqual(texts("to: ann@example.org"), {"ann@example.org"})
        self.assertEqual(texts("card 4111-2222-3333-4444"), {"4111222233334444"}, "a number found by its digits")
        self.assertEqual(texts("cc: maria.lopez@example.org"), {"maria.lopez@example.org"}, "a key is walked too")
        self.assertEqual(
            texts("at 2026-11-11T10:00:00Z, id 3f2b1c4d-aaaa-bbbb-cccc-1234567890ab"), set(), "timestamps and UUIDs exempt"
        )
        self.assertEqual(texts("sent 1794441785123"), set(), "a Unix time is not a number to hide")
        self.assertEqual(gate.payload_matches([{"payload": {"pad": " " * 40}}], " " * 40), [], "padding is exempt")
        # What counts is a word nobody else holds: the span name's words are the code's.
        span = next(m for m in gate.payload_matches(records, "the route POST /refunds/{refund_id}/cancel fails") if m.window)
        self.assertTrue(set(gate.words(span)) <= {"post", "refunds", "refund", "refund_id", "cancel", "id"})
        crushed = gate.payload_matches(records, "parcel arrived crushed, wants all of it back")
        self.assertIn("crushed", {w for m in crushed for w in gate.words(m)})

    def test_strays_are_ended_and_the_harness_is_not(self) -> None:
        if not Path("/proc/self/status").exists():
            self.skipTest("needs /proc (the sandbox's Linux)")
        stray = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        time.sleep(0.2)
        self.assertGreaterEqual(gate.kill_strays(), 1)
        self.assertIsNotNone(stray.wait(5))


class AlertResults(unittest.TestCase):
    MONITOR = {
        "id": "mon_" + "1" * 16,
        "name": "refunds-5xx",
        "source": {"pr": 3, "merge_sha": SHA, "file": "app.py"},
        "signal": {"request": {"route": "/refunds", "status_class": "5xx"}},
        "condition": {"count_over": 0},
        "window_minutes": 5,
    }

    def test_results_are_checked_with_the_runners_schema_before_they_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.object(alert, "RESULT", Path(d) / "result.json"):
            draft = {k: self.MONITOR[k] for k in ("name", "source", "signal")} | {
                "condition": {"count_over": 20},
                "window_minutes": 10,
            }
            self.assertEqual(
                alert.write_result({"kind": "alert", "outcome": "noise", "monitor": {"replace": draft}}, self.MONITOR), 0
            )
            self.assertEqual(
                json.loads((Path(d) / "result.json").read_text())["monitor"]["replace"]["condition"], {"count_over": 20}
            )
            gate_ = {"deployed_sha": SHA, "head_sha": "f" * 40, "tests": ["tests/test_a.py"], "failed_exit": 0}
            self.assertEqual(alert.write_result({"kind": "alert", "outcome": "pr", "number": 4, "gate": gate_}, self.MONITOR), 1)

    def test_the_branch_names_the_monitor_and_the_alert(self) -> None:
        job = {"monitor": self.MONITOR, "alert": {"id": "alt_" + "2" * 16}}
        self.assertEqual(alert.branch_name(job), "clovelly/fix-refunds-5xx-alt_" + "2" * 16)


if __name__ == "__main__":
    unittest.main()
