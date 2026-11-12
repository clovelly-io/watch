"""What the SDK hooks besides OpenTelemetry's library instrumentations: Python `logging`
at WARNING and above, uncaught exceptions in the main thread and in threads, and the
local variables of the frames an exception passed through.
"""

from __future__ import annotations

import json
import logging
import reprlib
import sys
import threading
import traceback
from types import TracebackType
from typing import Any

from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.trace import Span as SdkSpan

LOCALS = "clovelly.locals"
TEMPLATE = "clovelly.log.template"
MAX_FRAMES = 10
MAX_VARS = 50
MAX_REPR = 512

# logging.LogRecord's own attributes; anything else on a record came from `extra=`.
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime", "taskName"}

# Types whose repr is the interpreter's own and runs no code of the service's.
_PLAIN = (int, float, complex, bool, type(None), bytes, bytearray)


class _Repr(reprlib.Repr):
    """reprlib's bounded repr for strings, numbers and the built-in containers; any other
    object is written as `<ClassName>`, so a `__repr__` of the service's (a Django QuerySet
    that runs its query, a model that loads a lazy field) never runs on the error path."""

    def repr_instance(self, x: Any, level: int) -> str:
        if type(x) in _PLAIN:
            return repr(x)[: self.maxother]
        return f"<{type(x).__qualname__}>"


_repr = _Repr()
_repr.maxstring = MAX_REPR
_repr.maxother = MAX_REPR
_repr.maxlevel = 3


def _safe_repr(v: Any) -> str:
    try:
        # Subclasses of the built-in containers and str may override __repr__; reprlib
        # dispatches on the type's name, so only the exact built-in types go through it.
        if type(v) in (str, list, tuple, dict, set, frozenset) or type(v) in _PLAIN:
            return _repr.repr(v)[:MAX_REPR]
        return f"<{type(v).__qualname__}>"
    except Exception:  # noqa: BLE001 - nothing here may fail the service
        return "<unrepresentable>"


def frames_locals(tb: TracebackType | None) -> str | None:
    """The innermost frames' local variables, as the wire format's `clovelly.locals`."""
    frames = []
    while tb is not None:
        frames.append(tb)
        tb = tb.tb_next
    out = []
    for t in reversed(frames[-MAX_FRAMES:]):
        code = t.tb_frame.f_code
        names = list(t.tb_frame.f_locals.items())[:MAX_VARS]
        out.append(
            {
                "function": code.co_name,
                "file": code.co_filename,
                "line": t.tb_lineno,
                "vars": {k: _safe_repr(v) for k, v in names},
            }
        )
    return json.dumps(out) if out else None


_LOCALS_ATTR = "__clovelly_locals__"


def exception_locals(exc: BaseException) -> str | None:
    """`frames_locals` for an exception, computed once: a span records it and a log handler
    may log it again."""
    cached = getattr(exc, _LOCALS_ATTR, False)
    if cached is not False:
        return cached
    found = frames_locals(exc.__traceback__)
    try:
        setattr(exc, _LOCALS_ATTR, found)
    except Exception:  # noqa: BLE001 - some exception types take no attributes
        pass
    return found


def exception_attributes(exc: BaseException) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "exception.type": type(exc).__name__,
        "exception.message": str(exc),
        "exception.stacktrace": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }
    found = exception_locals(exc)
    if found:
        attrs[LOCALS] = found
    return attrs


def _severity(levelno: int) -> SeverityNumber:
    if levelno >= logging.CRITICAL:
        return SeverityNumber.FATAL
    if levelno >= logging.ERROR:
        return SeverityNumber.ERROR
    return SeverityNumber.WARN


class Handler(logging.Handler):
    """Sends log records at WARNING and above as OpenTelemetry log records. The rendered
    message is the body (sealed). The template is kept in the clear as
    `clovelly.log.template` only when the call passed arguments: without them (an f-string,
    `str(exc)`) the message is the rendered text, which is payload. `extra=` fields and any
    exception go with them."""

    def __init__(self, provider: LoggerProvider) -> None:
        super().__init__(level=logging.WARNING)
        self._provider = provider

    def emit(self, record: logging.LogRecord) -> None:
        try:
            attrs: dict[str, Any] = {
                k: v if isinstance(v, (str, bool, int, float)) else _safe_repr(v)
                for k, v in vars(record).items()
                if k not in _RESERVED
            }
            attrs["code.file.path"] = record.pathname
            attrs["code.function.name"] = record.funcName
            attrs["code.line.number"] = record.lineno
            if isinstance(record.msg, str) and record.args:
                attrs[TEMPLATE] = record.msg
            if record.exc_info and record.exc_info[1] is not None:
                attrs.update(exception_attributes(record.exc_info[1]))
            try:
                body = record.getMessage()
            except Exception:  # noqa: BLE001 - a bad format string still gets sent
                body = str(record.msg)
            self._provider.get_logger(record.name).emit(
                timestamp=int(record.created * 1e9),
                severity_number=_severity(record.levelno),
                severity_text=record.levelname,
                body=body,
                attributes=attrs,
            )
        except Exception:  # noqa: BLE001 - logging must never fail because of us
            pass


MAX_BODY = 16384
BODY = "http.request.body"
_SPAN_KEY = "clovelly.watch.span"
_BODY_KEY = "clovelly.watch.body"


def asgi_server_request_hook(span: Any, scope: dict[str, Any]) -> None:
    """Keeps the server span in the ASGI scope, so the body hook below can reach it."""
    scope[_SPAN_KEY] = span


def asgi_client_request_hook(span: Any, scope: dict[str, Any], message: dict[str, Any]) -> None:
    """Puts the first 16 KiB of the request body on the server span, as
    `http.request.body`, which is off the allowlist and so sealed. OpenTelemetry's ASGI
    instrumentation calls this with each message the application receives."""
    try:
        if message.get("type") != "http.request":
            return
        server = scope.get(_SPAN_KEY)
        if server is None or not server.is_recording():
            return
        seen = scope.get(_BODY_KEY, b"") + message.get("body", b"")[:MAX_BODY]
        scope[_BODY_KEY] = seen[:MAX_BODY]
        server.set_attribute(BODY, scope[_BODY_KEY].decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - the request goes on whatever happens here
        pass


def patch_record_exception() -> None:
    """Adds `clovelly.locals` to every exception a span records. OpenTelemetry's
    instrumentations record the exception object, whose traceback still holds the frames;
    the SDK's `Span.record_exception` is the one place that sees it."""
    original = SdkSpan.record_exception
    if getattr(original, "_clovelly", False):
        return

    def record_exception(
        self: SdkSpan,
        exception: BaseException,
        attributes: Any = None,
        timestamp: int | None = None,
        escaped: bool = False,
    ) -> None:
        try:
            found = exception_locals(exception)
            if found:
                attributes = {**(attributes or {}), LOCALS: found}
        except Exception:  # noqa: BLE001
            pass
        original(self, exception, attributes, timestamp, escaped)

    record_exception._clovelly = True  # type: ignore[attr-defined]
    SdkSpan.record_exception = record_exception  # type: ignore[method-assign]


def install_excepthooks(handler: Handler, flush: Any) -> None:
    """Uncaught exceptions, in the main thread and in others, are sent as CRITICAL log
    records and flushed before the previous hook runs."""
    previous = sys.excepthook
    previous_thread = threading.excepthook

    def report(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        try:
            record = logging.LogRecord("uncaught", logging.CRITICAL, "", 0, "uncaught exception", None, (exc_type, exc, tb))
            handler.emit(record)
            flush()
        except Exception:  # noqa: BLE001
            pass

    def hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        report(exc_type, exc, tb)
        previous(exc_type, exc, tb)

    def thread_hook(args: Any) -> None:
        if args.exc_value is not None:
            report(args.exc_type, args.exc_value, args.exc_traceback)
        previous_thread(args)

    sys.excepthook = hook
    threading.excepthook = thread_hook


def keep_handler_on_reconfigure(handler: logging.Handler) -> None:
    """Puts our handler back on the root logger after `logging.config.dictConfig`,
    `fileConfig` or `basicConfig(force=True)`, which remove the root's handlers when they
    configure it: Django's LOGGING with a root section, uvicorn's --log-config."""
    import logging.config

    def wrap(module: Any, name: str) -> None:
        original = getattr(module, name)
        if getattr(original, "_clovelly", False):
            return

        def configure(*args: Any, **kwargs: Any) -> Any:
            try:
                return original(*args, **kwargs)
            finally:
                root = logging.getLogger()
                if handler not in root.handlers:
                    root.addHandler(handler)

        configure._clovelly = True  # type: ignore[attr-defined]
        setattr(module, name, configure)

    for module, name in ((logging.config, "dictConfig"), (logging.config, "fileConfig"), (logging, "basicConfig")):
        wrap(module, name)
