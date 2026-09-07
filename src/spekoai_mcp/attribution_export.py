"""Bounded, best-effort export to the existing logging transport.

The request path only serializes bounded metadata and enqueues it. One daemon
worker owns log writes; a stalled sink never starts another worker or grows the
queue. Counts describe this process, not durable receipt by the log collector.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from contextvars import Context
from typing import Any
from uuid import uuid4

DEFAULT_MAX_PENDING = 256
DEFAULT_MAX_EVENT_BYTES = 4096
_DROP_REASONS = (
    "queue_full",
    "oversized",
    "serialization_error",
    "sink_error",
    "closed",
    "shutdown",
)


class BoundedLogExporter:
    """Never await sink I/O or retain request context in the pending queue."""

    def __init__(
        self,
        write: Callable[[dict[str, Any]], None],
        *,
        max_pending: int = DEFAULT_MAX_PENDING,
        max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
    ) -> None:
        if max_pending < 1 or max_event_bytes < 1:
            raise ValueError("Attribution export limits must be positive")
        self._write = write
        self._max_pending = max_pending
        self._max_event_bytes = max_event_bytes
        self._pending: deque[str] = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._in_flight = 0
        self._submitted = 0
        self._exported = 0
        self._drops = dict.fromkeys(_DROP_REASONS, 0)
        self.exporter_id = str(uuid4())
        # Explicitly empty context also covers Python builds that inherit thread context.
        self._worker = threading.Thread(
            target=Context().run,
            args=(self._run,),
            name="speko-mcp-attribution",
            daemon=True,
        )
        self._worker.start()

    def submit(self, event: dict[str, Any]) -> bool:
        """Copy bounded JSON into the queue without waiting for sink progress."""
        try:
            message = json.dumps(event, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            failure = "oversized" if len(message) > self._max_event_bytes else None
        except (TypeError, ValueError, OverflowError, RecursionError):
            message = ""
            failure = "serialization_error"
        with self._condition:
            self._submitted += 1
            if self._closed:
                failure = "closed"
            elif failure is None and len(self._pending) >= self._max_pending:
                failure = "queue_full"
            if failure is not None:
                self._drops[failure] += 1
                return False
            self._pending.append(message)
            self._condition.notify()
            return True

    def snapshot(self) -> dict[str, Any]:
        """Return bounded process-local counters without contacting the sink."""
        with self._condition:
            return {
                "exporter_id": self.exporter_id,
                "submitted_total": self._submitted,
                "exported_total": self._exported,
                "dropped_total": sum(self._drops.values()),
                "drop_counts": dict(self._drops),
                "pending": len(self._pending),
                "in_flight": self._in_flight,
                "closed": self._closed,
            }

    def wait_until_idle(self, timeout: float) -> bool:
        """Bounded maintenance/test wait. Never call this from a request path."""
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            while self._pending or self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self, timeout: float = 0) -> None:
        """Discard pending records; never require a stalled write to finish."""
        with self._condition:
            self._closed = True
            self._drops["shutdown"] += len(self._pending)
            self._pending.clear()
            self._condition.notify_all()
        if timeout > 0 and threading.current_thread() is not self._worker:
            self._worker.join(timeout)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                message = self._pending.popleft()
                self._in_flight = 1
                dropped_total = sum(self._drops.values())
                drop_counts = dict(self._drops)
            failed = False
            try:
                event = json.loads(message)
                event.update(
                    exporter_id=self.exporter_id,
                    export_dropped_total=dropped_total,
                    export_drop_counts=drop_counts,
                )
                self._write(event)
            except Exception:
                # No recursive or synchronous fallback to an unavailable log sink.
                failed = True
            finally:
                with self._condition:
                    if failed:
                        self._drops["sink_error"] += 1
                    else:
                        self._exported += 1
                    self._in_flight = 0
                    self._condition.notify_all()
