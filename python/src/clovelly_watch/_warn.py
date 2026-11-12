"""The SDK's own warnings: once each, to stderr, never through the service's logging
(whose handlers include ours, which would send them as records)."""

from __future__ import annotations

import sys
import threading

_seen: set[str] = set()
_lock = threading.Lock()


def warn_once(key: str, message: str) -> None:
    with _lock:
        if key in _seen:
            return
        _seen.add(key)
    try:
        print(f"clovelly-watch: {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - a closed stderr is not the service's problem
        pass
