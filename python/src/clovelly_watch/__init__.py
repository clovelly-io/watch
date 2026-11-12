"""Clovelly Watch for Python: one line, `clovelly_watch.init()`, called before the web
framework's app is created, and the service sends the telemetry Watch's monitors need.

It is a thin distribution of OpenTelemetry: OpenTelemetry's instrumentations for the
libraries the service already uses, plus the split (docs/watch-wire.md), which keeps a
closed list of metadata readable and seals everything else to the repository's payload
key. Nothing it does raises into the application.

Configuration is the environment: CLOVELLY_WATCH_TOKEN, CLOVELLY_WATCH_KEY, and
optionally CLOVELLY_WATCH_RELEASE, CLOVELLY_WATCH_ENVIRONMENT, CLOVELLY_WATCH_ENDPOINT,
CLOVELLY_WATCH_SAMPLE_RATE, OTEL_SERVICE_NAME and CLOVELLY_WATCH_STATS_FILE (diagnostics). Keyword arguments override them.
"""

from __future__ import annotations

import atexit
import base64
import importlib
import importlib.util
import logging
import os
import re
import threading
from typing import Any

__all__ = ["flush", "init", "shutdown", "stats"]
__version__ = "0.1.0"

DEFAULT_ENDPOINT = "https://ingest.clovelly.io"
DEFAULT_SAMPLE_RATE = 0.1
# Where the release comes from, in order: ours, then the platforms that set one.
RELEASE_VARIABLES = (
    "CLOVELLY_WATCH_RELEASE",
    "VERCEL_GIT_COMMIT_SHA",
    "RENDER_GIT_COMMIT",
    "RAILWAY_GIT_COMMIT_SHA",
    "HEROKU_SLUG_COMMIT",
    "SOURCE_VERSION",
    "COMMIT_REF",
    "GITHUB_SHA",
)
# (the library, OpenTelemetry's instrumentor for it); each is used only if its library
# is installed.
INSTRUMENTATIONS = (
    ("fastapi", "opentelemetry.instrumentation.fastapi", "FastAPIInstrumentor"),
    ("django", "opentelemetry.instrumentation.django", "DjangoInstrumentor"),
    ("flask", "opentelemetry.instrumentation.flask", "FlaskInstrumentor"),
    ("requests", "opentelemetry.instrumentation.requests", "RequestsInstrumentor"),
    ("httpx", "opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
    ("psycopg", "opentelemetry.instrumentation.psycopg", "PsycopgInstrumentor"),
    ("sqlalchemy", "opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor"),
)

_lock = threading.Lock()
_state: dict[str, Any] = {}


def _release(explicit: str | None) -> str | None:
    for value in [explicit] + [os.environ.get(v) for v in RELEASE_VARIABLES]:
        if value:
            value = value.strip().lower()
            return value if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) else None
    return None


def _payload_key(value: str) -> Any:
    from ._hpke import public_key

    if not value.startswith("cwk1_"):
        raise ValueError("CLOVELLY_WATCH_KEY does not start with cwk1_")
    raw = base64.urlsafe_b64decode(value[5:] + "=" * (-len(value[5:]) % 4))
    return public_key(raw)


def init(
    *,
    token: str | None = None,
    key: str | None = None,
    release: str | None = None,
    environment: str | None = None,
    endpoint: str | None = None,
    service: str | None = None,
    sample_rate: float | None = None,
) -> bool:
    """Starts sending. Returns whether it did: without a token or a payload key it warns
    once and sends nothing. Calling it again does nothing."""
    from ._warn import warn_once

    with _lock:
        if _state:
            return bool(_state.get("enabled"))
        _state["enabled"] = False
        try:
            token = token or os.environ.get("CLOVELLY_WATCH_TOKEN", "")
            if not token.startswith("cwt_"):
                warn_once("token", "CLOVELLY_WATCH_TOKEN is not set (or not a cwt_ token); sending nothing")
                return False
            try:
                recipient = _payload_key(key or os.environ.get("CLOVELLY_WATCH_KEY", ""))
            except (ValueError, TypeError) as e:
                warn_once("key", f"CLOVELLY_WATCH_KEY is not a payload key ({e}); sending nothing")
                return False
            found = _release(release)
            if found is None:
                warn_once(
                    "release", "no release: set CLOVELLY_WATCH_RELEASE to the deployed commit; these records arm no monitor"
                )
            rate = sample_rate
            if rate is None:
                rate = float(os.environ.get("CLOVELLY_WATCH_SAMPLE_RATE", DEFAULT_SAMPLE_RATE))
            _start(
                token=token,
                recipient=recipient,
                release=found,
                environment=environment or os.environ.get("CLOVELLY_WATCH_ENVIRONMENT"),
                endpoint=endpoint or os.environ.get("CLOVELLY_WATCH_ENDPOINT", DEFAULT_ENDPOINT),
                service=service or os.environ.get("OTEL_SERVICE_NAME", "unknown_service"),
                rate=min(max(rate, 0.0), 1.0),
            )
            _state["enabled"] = True
            return True
        except Exception as e:  # noqa: BLE001 - init never breaks the service
            warn_once("init", f"could not start ({type(e).__name__}: {e}); sending nothing")
            return False


def _start(
    *, token: str, recipient: Any, release: str | None, environment: str | None, endpoint: str, service: str, rate: float
) -> None:
    # Stable HTTP semantic conventions (http.request.method, http.response.status_code,
    # http.server.request.duration), read by the instrumentations when they start.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    # Every request header, as http.request.header.* attributes: off the allowlist, so sealed.
    os.environ.setdefault("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", ".*")

    from opentelemetry import _logs, metrics, trace
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    from . import _export, _hooks

    attrs: dict[str, str] = {"service.name": service}
    if release:
        attrs["vcs.ref.head.revision"] = release
    if environment:
        attrs["deployment.environment.name"] = environment
    resource = Resource(attrs)

    sender = _export.Sender(endpoint, token)
    tracer = TracerProvider(resource=resource, sampler=ALWAYS_ON, shutdown_on_exit=False)
    tracer.add_span_processor(
        _export.SamplingProcessor(
            BatchSpanProcessor(_export.SealingSpanExporter(sender, recipient), schedule_delay_millis=2000), rate
        )
    )
    meter = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(_export.StrippingMetricExporter(sender), export_interval_millis=10_000)],
        shutdown_on_exit=False,
    )
    logs = LoggerProvider(resource=resource, shutdown_on_exit=False)
    logs.add_log_record_processor(
        BatchLogRecordProcessor(_export.SealingLogExporter(sender, recipient), schedule_delay_millis=2000)
    )
    trace.set_tracer_provider(tracer)
    metrics.set_meter_provider(meter)
    _logs.set_logger_provider(logs)

    _hooks.patch_record_exception()
    handler = _hooks.Handler(logs)
    logging.getLogger().addHandler(handler)
    _hooks.keep_handler_on_reconfigure(handler)

    def flush() -> None:
        tracer.force_flush(2000)
        logs.force_flush(2000)
        sender.flush(2.0)

    _hooks.install_excepthooks(handler, flush)

    # Library-specific options: FastAPI's request bodies (sealed), and no span per ASGI
    # send, which is noise.
    extra: dict[str, dict[str, Any]] = {
        "fastapi": {
            "server_request_hook": _hooks.asgi_server_request_hook,
            "client_request_hook": _hooks.asgi_client_request_hook,
            "exclude_spans": ["send"],
        }
    }
    for library, module, name in INSTRUMENTATIONS:
        if importlib.util.find_spec(library) is None:
            continue
        try:
            instrumentor = getattr(importlib.import_module(module), name)()
            instrumentor.instrument(tracer_provider=tracer, meter_provider=meter, **extra.get(library, {}))
        except Exception as e:  # noqa: BLE001
            from ._warn import warn_once

            warn_once(f"instrument-{library}", f"could not instrument {library}: {type(e).__name__}")

    _state.update(sender=sender, tracer=tracer, meter=meter, logs=logs, handler=handler)
    atexit.register(shutdown)
    stats_file = os.environ.get("CLOVELLY_WATCH_STATS_FILE")
    if stats_file:
        threading.Thread(target=_write_stats, args=(stats_file,), name="clovelly-watch-stats", daemon=True).start()


def _write_stats(path: str) -> None:
    """CLOVELLY_WATCH_STATS_FILE: the sender's counters, rewritten every five seconds, for
    diagnosing a service without touching its code."""
    import json
    import time

    while True:
        try:
            with open(path, "w") as f:
                json.dump(stats(), f)
        except OSError:
            pass
        time.sleep(5)


def flush(timeout: float = 2.0) -> bool:
    """Exports what is pending and waits up to `timeout` for the queue to be sent: for short
    jobs and serverless handlers. Returns whether everything went."""
    sender = _state.get("sender")
    if sender is None:
        return False
    try:
        _state["tracer"].force_flush(int(timeout * 1000))
        _state["logs"].force_flush(int(timeout * 1000))
        return bool(sender.flush(timeout))
    except Exception:  # noqa: BLE001
        return False


def shutdown(timeout: float = 2.0) -> None:
    """Exports what is pending and sends what it can within `timeout`. Runs at exit."""
    sender = _state.get("sender")
    if sender is None:
        return
    try:
        _state["tracer"].force_flush(int(timeout * 1000))
        _state["logs"].force_flush(int(timeout * 1000))
        _state["meter"].force_flush(int(timeout * 1000))
        sender.close(timeout)
    except Exception:  # noqa: BLE001
        pass


def stats() -> dict[str, int]:
    """The sender's counters and its queue, for diagnostics and tests."""
    sender = _state.get("sender")
    if sender is None:
        return {}
    batches, size = sender.queued()
    return {**sender.stats, "queued_batches": batches, "queued_bytes": size}
