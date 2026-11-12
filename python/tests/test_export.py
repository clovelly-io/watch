"""The sender's promises (SPEC-watch.md "It never hurts the service"): a bounded queue that
drops the oldest, no blocking on the network, retries on 429 and 5xx; and the sampling."""

from __future__ import annotations

import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from clovelly_watch import _export


class Ingest(BaseHTTPRequestHandler):
    """Answers 429 to the first `refuse` requests, then 200, keeping what it accepted."""

    refuse = 0
    got: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if Ingest.refuse > 0:
            Ingest.refuse -= 1
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        Ingest.got.append(body)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


class SenderTest(unittest.TestCase):
    def test_an_unreachable_ingest_keeps_the_queue_under_its_bound_dropping_the_oldest(self) -> None:
        sender = _export.Sender("http://127.0.0.1:9", "cwt_x", max_bytes=10_000)
        started = time.monotonic()
        for i in range(100):
            sender.enqueue("/v1/logs", bytes([i]) * 1000)
        self.assertLess(time.monotonic() - started, 0.5, "enqueue must not wait on the network")
        batches, size = sender.queued()
        self.assertLessEqual(size, 10_000)
        self.assertGreaterEqual(sender.stats["dropped_batches"], 89)
        # the newest survive
        self.assertEqual(sender._queue[-1][1][:1], bytes([99]))
        sender.close(0.1)

    def test_429_is_retried_and_every_batch_arrives_in_order(self) -> None:
        Ingest.refuse, Ingest.got = 2, []
        server = ThreadingHTTPServer(("127.0.0.1", 0), Ingest)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            sender = _export.Sender(f"http://127.0.0.1:{server.server_port}", "cwt_x")
            for i in range(3):
                sender.enqueue("/v1/traces", bytes([i]))
            self.assertTrue(sender.flush(10))
            self.assertEqual(Ingest.got, [b"\x00", b"\x01", b"\x02"])
            self.assertEqual(sender.stats["failed_attempts"], 2)
            sender.close(0.1)
        finally:
            server.shutdown()


class SamplingTest(unittest.TestCase):
    def test_a_trace_is_sampled_or_not_as_a_whole_at_about_the_rate(self) -> None:
        import random

        rng = random.Random(7)
        ids = [rng.getrandbits(128) for _ in range(20_000)]
        kept = sum(_export.sampled(t, 0.1) for t in ids)
        self.assertTrue(1800 < kept < 2200, kept)
        self.assertEqual([_export.sampled(t, 0.1) for t in ids[:50]], [_export.sampled(t, 0.1) for t in ids[:50]])
        self.assertFalse(any(_export.sampled(t, 0.0) for t in ids))


if __name__ == "__main__":
    unittest.main()
