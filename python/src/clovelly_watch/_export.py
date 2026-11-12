"""Getting batches out without touching the service: OpenTelemetry's batch processors hand
records to the exporters here, which split and seal them and put the encoded request on
a bounded queue. One background thread sends the queue to ingest. When the queue is full
the oldest batch is dropped, so memory stays under the bound whatever ingest does, and
nothing here raises into the application.
"""

from __future__ import annotations

import os
import random
import threading
import time
import urllib.error
import urllib.request
import weakref
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    ObservableCounter,
    ObservableGauge,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import AggregationTemporality, MetricExporter, MetricExportResult, MetricsData
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

from . import _split
from ._hpke import Sealer
from ._warn import warn_once

# The queue's bound, in encoded bytes. The batch processors in front of it hold at most
# 2,048 spans and 2,048 log records of their own.
MAX_QUEUE_BYTES = 8 << 20
# The largest request sent: an export over this is split in two, each half sealed on its
# own, until every part fits. Ingest refuses bodies over 4 MiB.
MAX_REQUEST_BYTES = 2 << 20
REQUEST_TIMEOUT = 10.0
MAX_BACKOFF = 30.0


class Sender:
    """A bounded queue of encoded requests and the one thread that sends them."""

    def __init__(self, endpoint: str, token: str, max_bytes: int = MAX_QUEUE_BYTES) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._token = token
        self._max_bytes = max_bytes
        self._queue: deque[tuple[str, bytes]] = deque()
        self._bytes = 0
        self._cond = threading.Condition()
        self._closed = False
        self._stop = threading.Event()
        self._busy = False
        self.stats = {"sent_batches": 0, "dropped_batches": 0, "failed_attempts": 0, "refused_batches": 0}
        self._start()
        # A forked child (gunicorn --preload, multiprocessing) has no sender thread: start
        # one, with an empty queue, since the parent sends what it had.
        if hasattr(os, "register_at_fork"):
            me = weakref.ref(self)
            os.register_at_fork(after_in_child=lambda: (s := me()) is not None and s._after_fork())

    def _start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="clovelly-watch-sender", daemon=True)
        self._thread.start()

    def _after_fork(self) -> None:
        self._queue = deque()
        self._bytes = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._busy = False
        if not self._closed:
            self._start()

    def enqueue(self, path: str, body: bytes) -> None:
        """Adds a request; drops the oldest until it fits. Never blocks on the network."""
        with self._cond:
            if len(body) > self._max_bytes:
                self.stats["dropped_batches"] += 1
                return
            while self._queue and self._bytes + len(body) > self._max_bytes:
                _, old = self._queue.popleft()
                self._bytes -= len(old)
                self.stats["dropped_batches"] += 1
            self._queue.append((path, body))
            self._bytes += len(body)
            self._cond.notify()

    def queued(self) -> tuple[int, int]:
        """(batches, bytes) waiting."""
        with self._cond:
            return len(self._queue), self._bytes

    def flush(self, timeout: float) -> bool:
        """Waits until the queue is empty and nothing is in flight, or `timeout` passes."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._queue or self._busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(min(left, 0.1))
        return True

    def close(self, timeout: float) -> None:
        """Sends what it can within `timeout`, then stops the thread; the rest is dropped."""
        self.flush(timeout)
        self._stop.set()
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def _post(self, path: str, body: bytes) -> tuple[int, float | None]:
        """The status (0 for no answer) and any Retry-After in seconds."""
        req = urllib.request.Request(
            self._endpoint + path,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-protobuf", "Authorization": f"Bearer {self._token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                r.read()
                return r.status, None
        except urllib.error.HTTPError as e:
            retry = e.headers.get("Retry-After") if e.headers else None
            try:
                after = float(retry) if retry else None
            except ValueError:
                after = None
            return e.code, after
        except Exception:  # noqa: BLE001 - any failure to reach ingest is "no answer"
            return 0, None

    def _run(self) -> None:
        backoff = 1.0
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if not self._queue:
                    return
                path, body = self._queue[0]
                self._busy = True
            status, after = self._post(path, body)
            with self._cond:
                retry = status == 0 or status == 429 or status >= 500
                # The head may have been dropped while we were sending; remove only it.
                if self._queue and self._queue[0][1] is body and not retry:
                    self._queue.popleft()
                    self._bytes -= len(body)
                self._busy = False
                self._cond.notify_all()
                if status == 200:
                    self.stats["sent_batches"] += 1
                elif not retry:
                    self.stats["refused_batches"] += 1
                    warn_once(f"status-{status}", f"ingest refused a batch with HTTP {status}; check CLOVELLY_WATCH_TOKEN")
                else:
                    self.stats["failed_attempts"] += 1
            if retry:
                wait = min(after if after is not None else backoff, 60.0)
                backoff = min(backoff * 2, MAX_BACKOFF)
                if self._stop.wait(wait * (0.5 + random.random() / 2)):
                    return
            else:
                backoff = 1.0


def _guard(what: str, fail: Any, fn: Callable[[], Any]) -> Any:
    """Runs `fn`; any exception becomes one warning and `fail`."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - the SDK never raises into the service
        warn_once(f"export-{what}", f"could not export {what}: {type(e).__name__}")
        return fail


def enqueue_sealed(
    sender: Sender,
    path: str,
    records: Sequence[Any],
    seal: Callable[[Sequence[Any]], bytes],
    limit: int = MAX_REQUEST_BYTES,
) -> None:
    """Seals `records` into one request, or, when that is over `limit`, into halves sealed
    on their own, recursively; one record goes alone whatever its size (its payload is
    capped at 64 KiB). Error bursts are when requests grow, and a request ingest refuses
    loses every record in it."""
    body = seal(records)
    if len(body) > limit and len(records) > 1:
        half = len(records) // 2
        enqueue_sealed(sender, path, records[:half], seal, limit)
        enqueue_sealed(sender, path, records[half:], seal, limit)
        return
    sender.enqueue(path, body)


class SealingSpanExporter(SpanExporter):
    def __init__(self, sender: Sender, recipient: ec.EllipticCurvePublicKey) -> None:
        self._sender, self._recipient = sender, recipient

    def _seal(self, spans: Sequence[ReadableSpan]) -> bytes:
        request = encode_spans(spans)
        _split.seal_traces(request, Sealer(self._recipient))
        return request.SerializeToString()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        def run() -> SpanExportResult:
            enqueue_sealed(self._sender, "/v1/traces", list(spans), self._seal)
            return SpanExportResult.SUCCESS

        return _guard("spans", SpanExportResult.FAILURE, run)

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class SealingLogExporter(LogRecordExporter):
    def __init__(self, sender: Sender, recipient: ec.EllipticCurvePublicKey) -> None:
        self._sender, self._recipient = sender, recipient

    def _seal(self, batch: Sequence[ReadableLogRecord]) -> bytes:
        request = encode_logs(batch)
        _split.seal_logs(request, Sealer(self._recipient))
        return request.SerializeToString()

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        def run() -> LogRecordExportResult:
            enqueue_sealed(self._sender, "/v1/logs", list(batch), self._seal)
            return LogRecordExportResult.SUCCESS

        return _guard("logs", LogRecordExportResult.FAILURE, run)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self) -> None:
        pass


# Delta for what monitors count, so ingest adds per-minute counters without differencing.
_DELTA = {
    Counter: AggregationTemporality.DELTA,
    Histogram: AggregationTemporality.DELTA,
    ObservableCounter: AggregationTemporality.DELTA,
    UpDownCounter: AggregationTemporality.CUMULATIVE,
    ObservableUpDownCounter: AggregationTemporality.CUMULATIVE,
    ObservableGauge: AggregationTemporality.CUMULATIVE,
}


class StrippingMetricExporter(MetricExporter):
    def __init__(self, sender: Sender) -> None:
        super().__init__(preferred_temporality=_DELTA)
        self._sender = sender

    def export(self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: Any) -> MetricExportResult:
        def run() -> MetricExportResult:
            request = encode_metrics(metrics_data)
            if _split.strip_metrics(request):
                self._sender.enqueue("/v1/metrics", request.SerializeToString())
            return MetricExportResult.SUCCESS

        return _guard("metrics", MetricExportResult.FAILURE, run)

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
        pass


def sampled(trace_id: int, rate: float) -> bool:
    """The same answer for every span of a trace: the trace id's low 64 bits against the
    rate."""
    return (trace_id & 0xFFFF_FFFF_FFFF_FFFF) < rate * 2**64


class SamplingProcessor(SpanProcessor):
    """Decides when a span ends: every failed span, and a share of the rest by trace."""

    def __init__(self, inner: SpanProcessor, rate: float) -> None:
        self._inner, self._rate = inner, rate

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        if span.status.status_code is StatusCode.ERROR or (span.context and sampled(span.context.trace_id, self._rate)):
            self._inner.on_end(span)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)
