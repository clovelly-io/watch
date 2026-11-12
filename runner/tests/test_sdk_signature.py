"""The runner's call to the sandbox SDK against the real SDK's signature (the fix round's A3): the
fake environment in ``test_runner`` takes anything, so this binds the keyword arguments the runner
passes to ``clovelly.Environment.sandbox`` itself. Skipped where the SDK is not importable;
``tests/watch/run.sh phase20`` runs it against the tree's own SDK. The runner still needs the SDK
release after 0.6.0, which sends the cell its placement pass; that is not a signature."""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

try:
    import clovelly

    SANDBOX = inspect.signature(clovelly.Environment.sandbox)
except (ImportError, AttributeError):  # the SDK, or its gRPC, is not installed here
    SANDBOX = None

try:
    from test_runner import JOB, Api, Environment, config
except ModuleNotFoundError:  # discovered as the package `tests` (`unittest discover -s tests -t .`)
    from tests.test_runner import JOB, Api, Environment, config

from clovelly_watch_runner.runner import Runner


@unittest.skipIf(SANDBOX is None, "the sandbox SDK is not importable here")
class TheRealSdk(unittest.TestCase):
    def test_the_runners_sandbox_call_binds_to_the_sdks_signature(self) -> None:
        calls: list[dict] = []

        class Recording(Environment):
            def sandbox(self, subenv: str, **kw):  # type: ignore[no-untyped-def]
                calls.append({"subenv": subenv, **kw})
                return super().sandbox(subenv, **kw)

        with tempfile.TemporaryDirectory() as tmp:
            env = Recording(None)
            Runner(config(Path(tmp)), Api(), env, record=lambda r: None).run_job(dict(JOB))  # type: ignore[arg-type]
        self.assertEqual(len(calls), 1, json.dumps(calls, default=str))
        call = calls[0]
        subenv = call.pop("subenv")
        assert SANDBOX is not None
        SANDBOX.bind(object(), subenv, **call)  # raises TypeError on an argument the SDK does not take


if __name__ == "__main__":
    unittest.main()
