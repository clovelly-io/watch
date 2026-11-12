"""The hooks' promises from the phase 16 review: a log's rendered message never goes out
as its template, a local variable's repr runs no code of the service's, the root handler
survives logging being reconfigured, a forked worker still sends, and an export too large
for one request is split before it is sealed."""

from __future__ import annotations

import logging
import logging.config
import os
import unittest
from typing import Any

from clovelly_watch import _export, _hooks


class Logger:
    def __init__(self, out: list[dict[str, Any]]) -> None:
        self.out = out

    def emit(self, **record: Any) -> None:
        self.out.append(record)


class Provider:
    def __init__(self) -> None:
        self.out: list[dict[str, Any]] = []

    def get_logger(self, name: str) -> Logger:
        return Logger(self.out)


def emitted(msg: str, *args: Any) -> dict[str, Any]:
    provider = Provider()
    handler = _hooks.Handler(provider)  # type: ignore[arg-type]
    handler.emit(logging.LogRecord("billing", logging.WARNING, "app.py", 1, msg, args or None, None))
    return provider.out[0]


class Touchy:
    """A value whose repr runs the service's code, as a Django QuerySet's does."""

    calls = 0

    def __repr__(self) -> str:
        Touchy.calls += 1
        return "SELECT ... evaluated"


class HooksTest(unittest.TestCase):
    def test_the_template_is_sent_only_when_the_call_passed_arguments(self) -> None:
        card = "MARKER-4242"
        self.assertNotIn(_hooks.TEMPLATE, emitted(f"refund failed for {card}")["attributes"])
        with_args = emitted("refund failed for %s", card)
        self.assertEqual(with_args["attributes"][_hooks.TEMPLATE], "refund failed for %s")
        self.assertEqual(with_args["body"], "refund failed for MARKER-4242")

    def test_locals_are_repr_d_without_running_the_services_code_and_once_per_exception(self) -> None:
        Touchy.calls = 0

        def fail() -> None:
            rows = Touchy()  # noqa: F841 - a local for the capture
            numbers = [1, 2.5, None, b"x"]  # noqa: F841
            raise ValueError("boom")

        try:
            fail()
        except ValueError as e:
            first = _hooks.exception_locals(e)
            self.assertIs(_hooks.exception_locals(e), first)
            exc = e
        self.assertEqual(Touchy.calls, 0)
        self.assertIn('"rows": "<Touchy>"', first or "")
        self.assertIn("[1, 2.5, None, b'x']", first or "")
        self.assertIsNotNone(exc)

    def test_the_handler_survives_dict_config_and_basic_config(self) -> None:
        root = logging.getLogger()
        handler = _hooks.Handler(Provider())  # type: ignore[arg-type]
        root.addHandler(handler)
        _hooks.keep_handler_on_reconfigure(handler)
        try:
            logging.config.dictConfig({"version": 1, "root": {"level": "INFO", "handlers": []}})
            self.assertIn(handler, root.handlers)
            logging.basicConfig(force=True)
            self.assertIn(handler, root.handlers)
        finally:
            root.removeHandler(handler)

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork")
    def test_a_forked_child_has_a_sender_thread_and_an_empty_queue(self) -> None:
        sender = _export.Sender("http://127.0.0.1:9", "cwt_x")
        sender.enqueue("/v1/logs", b"parent's")
        pid = os.fork()
        if pid == 0:
            ok = sender._thread.is_alive() and sender.queued() == (0, 0)
            os._exit(0 if ok else 1)
        _, status = os.waitpid(pid, 0)
        sender.close(0.1)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)


class SizeSplitTest(unittest.TestCase):
    def test_an_export_over_the_limit_is_sealed_in_parts_under_it(self) -> None:
        got: list[bytes] = []

        class Sink:
            def enqueue(self, path: str, body: bytes) -> None:
                got.append(body)

        records = [b"x" * 300 for _ in range(10)]
        _export.enqueue_sealed(Sink(), "/v1/logs", records, lambda rs: b"".join(rs), limit=1000)  # type: ignore[arg-type]
        self.assertTrue(all(len(b) <= 1000 for b in got))
        self.assertEqual(b"".join(got), b"x" * 3000)
        got.clear()
        _export.enqueue_sealed(Sink(), "/v1/logs", [b"y" * 5000], lambda rs: b"".join(rs), limit=1000)  # type: ignore[arg-type]
        self.assertEqual(len(got), 1, "one record goes alone, whatever its size")
