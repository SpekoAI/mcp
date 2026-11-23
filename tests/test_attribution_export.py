"""Slow sinks cannot consume request latency or unbounded memory."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace

import httpx
import mcp.types as mt
import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import ToolResult

from spekoai_mcp import attribution
from spekoai_mcp.attribution_export import BoundedLogExporter


def _assert_accounted(snapshot: dict) -> None:
    assert snapshot["submitted_total"] == (
        snapshot["exported_total"]
        + snapshot["dropped_total"]
        + snapshot["pending"]
        + snapshot["in_flight"]
    )


async def test_blocked_stderr_does_not_block_httpx_logging_tools_or_event_loop(monkeypatch) -> None:
    entered, released = threading.Event(), threading.Event()
    messages = []

    def blocked_write(fd, payload):
        assert fd == 2
        entered.set()
        assert released.wait(2)
        messages.append(payload.decode("ascii"))
        return len(payload)

    root_log = StringIO()
    root = logging.getLogger()
    # Match production: HTTPX responses synchronously use the root INFO handler.
    monkeypatch.setattr(root, "handlers", [logging.StreamHandler(root_log)])
    monkeypatch.setattr(root, "level", logging.INFO)
    monkeypatch.setattr(attribution, "_write_stderr", blocked_write)
    monkeypatch.setenv("ATTRIBUTION_ENABLED", "true")
    monkeypatch.setattr(attribution, "get_access_token", lambda: None)
    observed_clock = [datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(attribution, "datetime", SimpleNamespace(now=lambda _: observed_clock[0]))
    exporter = BoundedLogExporter(attribution._write_event)
    monkeypatch.setattr(attribution, "_EXPORTER", exporter)
    context = MiddlewareContext(message=mt.CallToolRequestParams(name="agents.list"))
    expected = ToolResult(content="private result")

    async def call_next(_):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get("https://platform.example.test/v1/agents")
            assert response.status_code == 200
        return expected

    # This also bounds failure time if synchronous logging is accidentally restored.
    emergency_release = threading.Timer(0.5, released.set)
    emergency_release.start()
    try:
        started = time.monotonic()
        result = await attribution.InvocationAttributionMiddleware().on_call_tool(
            context, call_next
        )
        assert time.monotonic() - started < 0.2
        assert result is expected
        assert await asyncio.to_thread(entered.wait, 1)
        observed_clock[0] += timedelta(minutes=1)
        started = time.monotonic()
        result = await attribution.InvocationAttributionMiddleware().on_call_tool(
            context, call_next
        )
        await asyncio.sleep(0.01)
        assert time.monotonic() - started < 0.2
        assert result is expected
        assert not released.is_set()
        assert exporter.snapshot()["in_flight"] == 1
        assert exporter.snapshot()["pending"] == 1
        assert attribution.invocation_headers() == {}
    finally:
        observed_clock[0] += timedelta(hours=1)
        released.set()
        emergency_release.cancel()
        assert exporter.wait_until_idle(2)
        exporter.close(timeout=1)
    assert len(messages) == 2
    assert root_log.getvalue().count('"HTTP/1.1 200 OK"') == 2
    assert all(json.loads(message)["event_version"] == 3 for message in messages)
    assert [json.loads(message)["completed_at"] for message in messages] == [
        "2026-09-06T12:00:00.000Z",
        "2026-09-06T12:01:00.000Z",
    ]
    assert "private result" not in "".join(messages)


@pytest.mark.parametrize("failure", ["exception", "short_write"])
def test_actual_stderr_write_failures_count_as_loss(monkeypatch, failure) -> None:
    def broken_write(fd, payload):
        assert fd == 2
        if failure == "exception":
            raise OSError("private sink failure")
        return len(payload) - 1

    monkeypatch.setattr(attribution, "_write_stderr", broken_write)
    exporter = BoundedLogExporter(attribution._write_event)
    try:
        assert exporter.submit({"event": "test"})
        assert exporter.wait_until_idle(1)
        assert exporter.snapshot()["exported_total"] == 0
        assert exporter.snapshot()["drop_counts"]["sink_error"] == 1
        _assert_accounted(exporter.snapshot())
    finally:
        exporter.close(timeout=1)


def test_process_exits_with_a_worker_blocked_in_a_real_stderr_pipe() -> None:
    # No logger mocks: a full OS pipe makes os.write block while Python exits.
    program = """
import logging
import os
import time
from spekoai_mcp import attribution

logging.basicConfig(level=logging.INFO)
read_fd, write_fd = os.pipe()
os.set_blocking(write_fd, False)
try:
    while True:
        os.write(write_fd, b'x' * 4096)
except BlockingIOError:
    pass
os.set_blocking(write_fd, True)
os.dup2(write_fd, 2)
assert attribution._EXPORTER.submit({'event': 'blocked-stderr-exit-test'})
deadline = time.monotonic() + 1
while not attribution._EXPORTER.snapshot()['in_flight']:
    assert time.monotonic() < deadline
    time.sleep(0.001)
assert not attribution._EXPORTER.wait_until_idle(0.02)
attribution._EXPORTER.close(timeout=0.01)
print('exited without waiting for stderr', flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "exited without waiting for stderr"


def test_queue_stays_bounded_and_reports_loss_after_sink_recovers() -> None:
    entered, released = threading.Event(), threading.Event()
    events = []

    def write(event):
        entered.set()
        assert released.wait(2)
        events.append(event)

    exporter = BoundedLogExporter(write, max_pending=4, max_event_bytes=128)
    try:
        assert exporter.submit({"sequence": 0})
        assert entered.wait(1)
        for sequence in range(1, 1005):
            assert exporter.submit({"sequence": sequence}) is (sequence <= 4)
        snapshot = exporter.snapshot()
        assert snapshot["pending"] == 4
        assert snapshot["in_flight"] == 1
        assert snapshot["drop_counts"]["queue_full"] == 1000
        assert len(exporter._pending) == 4
        assert sum(map(len, exporter._pending)) <= 4 * 128
        _assert_accounted(snapshot)
        released.set()
        assert exporter.wait_until_idle(2)
        assert exporter.submit({"sequence": 1005})
        assert exporter.wait_until_idle(2)
        assert [event["sequence"] for event in events] == [0, 1, 2, 3, 4, 1005]
        assert events[-1]["export_dropped_total"] == 1000
        assert events[-1]["export_drop_counts"]["queue_full"] == 1000
        assert events[-1]["exporter_id"] == exporter.exporter_id
        assert exporter.snapshot()["exported_total"] == 6
        _assert_accounted(exporter.snapshot())
    finally:
        released.set()
        exporter.close(timeout=1)


def test_sink_failure_drops_once_and_worker_recovers_without_fallback() -> None:
    attempts, events = [], []

    def write(event):
        attempts.append(event)
        if len(attempts) == 1:
            raise OSError("private sink failure")
        events.append(event)

    exporter = BoundedLogExporter(write)
    try:
        assert exporter.submit({"sequence": 1})
        assert exporter.wait_until_idle(1)
        assert exporter.snapshot()["drop_counts"]["sink_error"] == 1
        assert exporter.submit({"sequence": 2})
        assert exporter.wait_until_idle(1)
        assert len(attempts) == 2
        assert events[0]["sequence"] == 2
        assert events[0]["export_drop_counts"]["sink_error"] == 1
        assert "private sink failure" not in json.dumps(events)
        _assert_accounted(exporter.snapshot())
    finally:
        exporter.close(timeout=1)


def test_shutdown_is_bounded_while_sink_is_blocked_and_rejects_new_records() -> None:
    entered, released = threading.Event(), threading.Event()

    def write(_):
        entered.set()
        assert released.wait(2)

    exporter = BoundedLogExporter(write, max_pending=2)
    try:
        assert exporter.submit({"sequence": 0})
        assert entered.wait(1)
        assert exporter.submit({"sequence": 1})
        assert exporter.submit({"sequence": 2})
        assert not exporter.wait_until_idle(0.01)
        started = time.monotonic()
        exporter.close(timeout=0.01)
        assert time.monotonic() - started < 0.2
        assert exporter._worker.daemon
        assert exporter._worker.is_alive()
        assert exporter.snapshot()["drop_counts"]["shutdown"] == 2
        assert not exporter.submit({"sequence": 3})
        assert exporter.snapshot()["drop_counts"]["closed"] == 1
        assert exporter.snapshot()["pending"] == 0
        _assert_accounted(exporter.snapshot())
        exporter.close()
        assert exporter.snapshot()["drop_counts"]["shutdown"] == 2
    finally:
        released.set()
        exporter.close(timeout=1)
    assert not exporter._worker.is_alive()
    _assert_accounted(exporter.snapshot())


def test_concurrent_submitters_keep_exact_counts_and_single_worker() -> None:
    entered, released = threading.Event(), threading.Event()
    worker_ids = set()

    def write(_):
        worker_ids.add(threading.get_ident())
        entered.set()
        assert released.wait(2)

    exporter = BoundedLogExporter(write, max_pending=8)
    try:
        assert exporter.submit({"sequence": -1})
        assert entered.wait(1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(
                pool.map(lambda sequence: exporter.submit({"sequence": sequence}), range(800))
            )
        assert sum(accepted) == 8
        assert exporter.snapshot()["drop_counts"]["queue_full"] == 792
        _assert_accounted(exporter.snapshot())
        released.set()
        assert exporter.wait_until_idle(2)
        assert exporter.snapshot()["exported_total"] == 9
        assert len(worker_ids) == 1
        _assert_accounted(exporter.snapshot())
    finally:
        released.set()
        exporter.close(timeout=1)


def test_queue_copies_metadata_and_worker_has_no_inherited_request_context() -> None:
    private_context = ContextVar("synthetic_secret", default=None)
    token = private_context.set("synthetic-secret")
    observations = []
    entered, released = threading.Event(), threading.Event()

    def write(event):
        entered.set()
        assert released.wait(2)
        observations.append((event, private_context.get()))

    exporter = BoundedLogExporter(write)
    try:
        first = {"receivers": ["platform"]}
        assert exporter.submit(first)
        first["receivers"].append("PRIVATE MUTATION")
        assert entered.wait(1)
        queued = {"receivers": ["router_direct"]}
        assert exporter.submit(queued)
        queued["receivers"].append("PRIVATE MUTATION")
        released.set()
        assert exporter.wait_until_idle(2)
        assert all(context is None for _, context in observations)
        assert "PRIVATE MUTATION" not in json.dumps(observations)
        assert "synthetic-secret" not in json.dumps(observations)
    finally:
        private_context.reset(token)
        released.set()
        exporter.close(timeout=1)


def test_oversized_and_unserializable_records_are_counted_without_queue_growth() -> None:
    events = []
    exporter = BoundedLogExporter(events.append, max_event_bytes=32)
    try:
        assert not exporter.submit({"value": "x" * 33})
        assert not exporter.submit({"value": "\u2603" * 10})
        assert not exporter.submit({"value": object()})
        assert not exporter.submit({"value": float("nan")})
        assert exporter.snapshot()["pending"] == 0
        assert exporter.snapshot()["drop_counts"]["oversized"] == 2
        assert exporter.snapshot()["drop_counts"]["serialization_error"] == 2
        assert events == []
        _assert_accounted(exporter.snapshot())
    finally:
        exporter.close(timeout=1)


@pytest.mark.parametrize("options", [{"max_pending": 0}, {"max_event_bytes": 0}])
def test_invalid_bounds_never_start_a_worker(options) -> None:
    with pytest.raises(ValueError):
        BoundedLogExporter(lambda _: None, **options)
