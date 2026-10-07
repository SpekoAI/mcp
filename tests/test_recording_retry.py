"""A 404 must never read as "retry", and the test-call review must state a cadence.

One agent on the ChatGPT harness sent ``GET /v1/calls/:id/recording`` 1,078 times
in 151 seconds while reviewing test calls. Three things lined up: ``agents.test_call``
told it to poll with no interval and no cap, the recording only finalizes AFTER the
call ends so the first read always 404s, and ``next_step_for_error`` had no 404
branch - so every 404 came back as ``next_step=Retry the Speko MCP request``.
Platform was already answering ``retryable: false`` and a lifecycle ``status`` on
the wire; ``_parse_api_error`` dropped both.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastmcp.exceptions import ToolError

import spekoai_mcp.http_client as http_client
from spekoai_mcp.action_tools import (
    NON_RETRYABLE_NEXT_STEP,
    NOT_FOUND_NEXT_STEP,
    RECORDING_DISABLED_NEXT_STEP,
    RECORDING_MISSING_FILE_NEXT_STEP,
    RECORDING_PENDING_NEXT_STEP,
    RECORDING_PENDING_NEXT_STEP_SESSIONS,
    RECORDING_TERMINAL_NEXT_STEP,
    next_step_for_error,
)
from spekoai_mcp.profiles import (
    BUILDER_PROFILE_TOOL_NAMES,
    CHATGPT_PROFILE_TOOL_NAMES,
    CONNECTOR_EXCLUDED_TOOL_NAMES,
    DIRECTORY_REQUIRED_ABSENT_TOOL_NAMES,
    REPLIT_PROFILE_TOOL_NAMES,
)
from spekoai_mcp.server import create_server

GENERIC_RETRY = "Retry the Speko MCP request or inspect the Speko API response details."

RECORDING_PATH = "/v1/calls/call_1/recording"


def _not_available(status: str | None) -> dict[str, object]:
    """The body `apps/server/src/routes/calls.ts:572-581` returns, as enriched.

    `middleware/enrich-errors.ts` adds `retryable` and `docs_url` for every
    registered code; neither recording code is curated, so no `hint` ships.
    """
    return {
        "error": "recording not available",
        "code": "RECORDING_NOT_AVAILABLE",
        "status": status,
        "retryable": False,
        "docs_url": "https://docs.speko.dev/errors/recording_not_available",
    }


def _raise_from(body: dict[str, object], *, status_code: int = 404) -> http_client.SpekoApiError:
    resp = httpx.Response(
        status_code,
        json=body,
        request=httpx.Request("GET", f"https://api.speko.dev{RECORDING_PATH}"),
        headers={"x-request-id": "req_404"},
    )
    with pytest.raises(http_client.SpekoApiError) as raised:
        http_client._raise_api_error(resp)
    return raised.value


# --- the wire fields that were being parsed away ----------------------------


def test_api_error_carries_resource_status_and_retryable() -> None:
    exc = _raise_from(_not_available("pending"))

    assert exc.status_code == 404
    assert exc.code == "RECORDING_NOT_AVAILABLE"
    # The body's `status` is the RESOURCE's lifecycle state, not the HTTP status.
    assert exc.resource_status == "pending"
    assert exc.retryable is False
    assert exc.trace_id == "req_404"


def test_resource_status_survives_a_null_and_stays_none_when_absent() -> None:
    assert _raise_from(_not_available(None)).resource_status is None
    # A numeric `status` is some other route meaning HTTP status; not a lifecycle.
    assert _raise_from({"error": "nope", "code": "X", "status": 404}).resource_status is None
    assert _raise_from({"error": "nope"}).retryable is None


def test_action_error_shape_carries_nested_retryable() -> None:
    nested = {
        "error": {
            "code": "ACTION_NOT_FOUND",
            "message": "unknown action",
            "retryable": False,
        }
    }
    exc = _raise_from(nested)

    assert exc.code == "ACTION_NOT_FOUND"
    assert exc.retryable is False


# --- what the model is told to do next --------------------------------------


@pytest.mark.parametrize("status", ["pending", "uploading", None, "some_new_state"])
def test_pending_recording_waits_on_a_cadence_instead_of_retrying(status: str | None) -> None:
    """In flight, unset, or unrecognized: wait a stated interval, bounded."""
    step = next_step_for_error(_raise_from(_not_available(status)), path=RECORDING_PATH)

    assert step == RECORDING_PENDING_NEXT_STEP
    assert "5 seconds" in step
    assert "recording_status" in step
    assert "calls.get" in step
    assert "Do not call" in step and "loop" in step
    assert "24" in step
    assert GENERIC_RETRY not in step
    assert "Retry the Speko MCP request" not in step


SESSION_RECORDING_PATH = "/v1/sessions/sess_1/recording"


@pytest.mark.parametrize("status", ["pending", "uploading", None])
def test_session_surface_names_its_own_read_and_spelling(status: str | None) -> None:
    """`sessions.recording.get` is handwritten too, so it reaches this branch.

    The two routes serialize the same column under two spellings, so the calls
    wording would send a session caller looking for `recording_status` on a
    payload that spells it `recordingStatus`.
    """
    step = next_step_for_error(_raise_from(_not_available(status)), path=SESSION_RECORDING_PATH)

    assert step == RECORDING_PENDING_NEXT_STEP_SESSIONS
    assert "`recordingStatus` from sessions.get" in step
    assert "calls.get" not in step
    assert "5 seconds" in step
    assert "Retry the Speko MCP request" not in step


def test_session_daily_branch_omits_status_and_still_stays_bounded() -> None:
    """`routes/sessions.ts:4357` is the one RECORDING_NOT_AVAILABLE with no `status`.

    Its real cause is a Daily API error, so the wait wording is wrong about why
    - but it is capped at ~24 checks, so it cannot become a storm. Pinned here
    because it is the one body in the family that diverges.
    """
    body = {"error": "recording not available", "code": "RECORDING_NOT_AVAILABLE"}
    exc = _raise_from(body)

    assert exc.resource_status is None
    assert next_step_for_error(exc, path=SESSION_RECORDING_PATH) == (
        RECORDING_PENDING_NEXT_STEP_SESSIONS
    )


@pytest.mark.parametrize("status", ["failed", "suppressed", "discarded"])
def test_terminal_recording_tells_the_model_to_stop(status: str) -> None:
    step = next_step_for_error(_raise_from(_not_available(status)), path=RECORDING_PATH)

    assert step == RECORDING_TERMINAL_NEXT_STEP
    assert "never exist" in step
    assert "Do not retry" in step
    assert "Retry the Speko MCP request" not in step


def test_ready_but_missing_file_is_not_a_polling_problem() -> None:
    """`status !== 'ready' || !recordingObjectPath` - so `ready` can 404 too.

    Waiting cannot fix a row whose stored object is gone, so this one must not
    get the poll wording.
    """
    step = next_step_for_error(_raise_from(_not_available("ready")), path=RECORDING_PATH)

    assert step == RECORDING_MISSING_FILE_NEXT_STEP
    assert "Do not retry" in step
    assert "5 seconds" not in step


def test_recording_disabled_stops_the_whole_line_of_inquiry() -> None:
    body = {"error": "recording is not configured on this server", "code": "RECORDING_DISABLED"}
    step = next_step_for_error(_raise_from(body), path=RECORDING_PATH)

    assert step == RECORDING_DISABLED_NEXT_STEP
    assert "Do not retry" in step
    assert "Retry the Speko MCP request" not in step


@pytest.mark.parametrize(
    "code",
    [
        "NOT_FOUND",  # what enrichErrors stamps on a bare REST 404
        "ACTION_NOT_FOUND",  # nested under error.code, unregistered
        "SESSION_NOT_FOUND",  # registered, curated, and thrown by the actions layer
        "AGENT_NOT_FOUND",
        "VOICE_NOT_FOUND",
    ],
)
def test_not_found_sends_the_model_to_check_the_id(code: str) -> None:
    step = next_step_for_error(
        _raise_from({"error": "not found", "code": code}), path="/v1/calls/x"
    )

    assert step == NOT_FOUND_NEXT_STEP
    assert "unchanged" in step
    assert "Retry the Speko MCP request" not in step


# --- the two guards on either side of the new branch ------------------------


def test_unrecognized_404_without_a_verdict_keeps_the_generic_default() -> None:
    """No regression: a 404 the registry knows nothing about is unchanged."""
    exc = http_client.SpekoApiError(404, "Not Found", code="SOMETHING_NEW")

    assert next_step_for_error(exc, path="/v1/other") == GENERIC_RETRY


def test_unrecognized_404_defers_to_platforms_retryable_verdict() -> None:
    """An uncoded 404 Platform calls non-retryable must not come back as "retry"."""
    step = next_step_for_error(
        _raise_from({"error": "gone for good", "code": "SOMETHING_NEW", "retryable": False}),
        path="/v1/other",
    )

    assert step == NON_RETRYABLE_NEXT_STEP
    assert GENERIC_RETRY not in step


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (429, "EXPORT_BUSY"),
        (409, "PHONE_NUMBER_PROVISIONING_IN_PROGRESS"),
        (409, "SOUND_NOT_READY"),
    ],
)
def test_retryable_false_is_ignored_outside_404(status_code: int, code: str) -> None:
    """The flag is only trustworthy at 404, and this pins that boundary.

    `retryable` is curated for some codes and inferred from category for the
    rest - `inferRetryable` answers true only for `provider` and `quota` - so
    every one of these "still working on it" states ships `retryable: false`
    while in fact clearing on its own. Acting on the flag here would tell an
    agent never to retry a 429, which is this bug's mirror image. None of them
    answers 404, so scoping the check to 404 keeps them out of reach.
    """
    exc = _raise_from(
        {"error": "not yet", "code": code, "retryable": False}, status_code=status_code
    )

    assert exc.retryable is False
    assert next_step_for_error(exc, path="/v1/other") == GENERIC_RETRY


def test_retryable_true_still_gets_the_generic_retry() -> None:
    exc = _raise_from(
        {"error": "upstream hiccup", "code": "SIGNED_URL_FAILED", "retryable": True},
        status_code=500,
    )

    assert next_step_for_error(exc, path=RECORDING_PATH) == GENERIC_RETRY


# --- end to end, through the tool the storm actually called -----------------


@pytest.fixture
def recording_pending_api(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            404, json=_not_available("pending"), headers={"x-request-id": "req_404"}
        )

    monkeypatch.setattr(
        http_client, "get_access_token", lambda: SimpleNamespace(token="sk_test-token")
    )
    http_client._TEST_TRANSPORT = httpx.MockTransport(handler)
    try:
        yield paths
    finally:
        http_client._TEST_TRANSPORT = None


async def test_recording_tool_hands_the_model_a_cadence_not_a_retry(
    recording_pending_api: list[str],
) -> None:
    mcp = create_server()
    with pytest.raises(ToolError) as raised:
        await mcp.call_tool("calls.recording.get", {"call_id": "call_1"})

    text = str(raised.value)
    assert "recording_status" in text
    assert "5 seconds" in text
    assert "trace_id=req_404" in text
    assert "Retry the Speko MCP request" not in text
    assert recording_pending_api == ["/v1/calls/call_1/recording"]


async def test_session_recording_tool_reaches_the_same_branch(
    recording_pending_api: list[str],
) -> None:
    """Both recording tools are handwritten, so both get a next_step.

    `sessions.transcript.get`, by contrast, is shadowed by the generated
    manifest tool (`ManifestActionTool` re-raises without a next_step), so a
    404 there still reaches the model bare. It cannot 404 for a valid in-org id
    - `routes/sessions.ts:4073` answers `200 {entries: []}` instead - which is
    why that gap is not part of this fix.
    """
    mcp = create_server()
    with pytest.raises(ToolError) as raised:
        await mcp.call_tool("sessions.recording.get", {"session_id": "sess_1"})

    text = str(raised.value)
    assert "sessions.get" in text
    assert "5 seconds" in text
    assert "Retry the Speko MCP request" not in text
    assert recording_pending_api == ["/v1/sessions/sess_1/recording"]


# --- the description that started the loop ----------------------------------


async def test_test_call_description_states_a_cadence_and_the_terminal_states() -> None:
    """Pins the fix in place; `test_audio_tools.py` is the precedent for this shape.

    The storm began in this text - "poll calls.get until it ends, then read
    calls.recording.get" with no interval, no cap, and no mention that the
    recording is not ready when the call ends. A reword that drops any of those
    three reopens it.
    """
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    # Normalized: the docstring is hard-wrapped, so any phrase can straddle a newline.
    description = " ".join((tools["agents.test_call"].description or "").split())

    assert "5 seconds" in description
    assert "24" in description
    assert "recording_status" in description
    assert "AFTER the call ends" in description
    for terminal in ("failed", "suppressed", "discarded"):
        assert terminal in description

    # The route answers `{runId, status:'queued'}` (`routes/agent-test-call.ts:136`);
    # the old text promised "session ids", so an agent went looking for an id the
    # response never carried. Every review step needs agentSessionId, which the
    # worker only publishes onto the run result once the call starts.
    assert "RUN id" in description
    assert "NOT a session id" in description
    assert "agentSessionId" in description

    # Readiness arrives AFTER the call ends, so guidance that stops polling at
    # the end of the call can never observe 'ready' - the last response it read
    # still says 'pending'. The cadence has to outlive the call.
    assert "KEEP polling" in description

    # And it needs its OWN budget. One shared ~24-check budget at 5s is ~2
    # minutes, which a single call can spend before finalization even begins
    # (ttl_seconds defaults to 300 and reaches 1800), so the agent would report
    # "never finalized" for a recording that was seconds away.
    assert "FURTHER" in description
    assert "ttl_seconds" in description
    assert "THREE separately bounded phases" in description

    # The call phase must cover the WORKER's deadline, not just the session cap:
    # `run-gate.ts:507` wraps a test call in `withDeadline((ttlSec + 60) * 1000)`,
    # so a client that stops at ttl_seconds can quit up to a minute before a
    # legitimate call ends and miss both the transcript and the recording.
    assert "PLUS" in description
    assert "60s of worker startup headroom" in description

    # A run can settle WITHOUT ever writing a session id (creation failed). The
    # settled statuses are real - `agentEvalRun.status` in `db/schema.ts:3617` -
    # and the run already carries the reason, so waiting out the budget and then
    # saying only "never started" throws away the answer the agent was holding.
    for settled in ("passed", "failed", "aborted", "incomplete"):
        assert settled in description
    assert "run.status" in description
    assert "run.result" in description

    # ...but `aborted` is NOT the same as "never started". Both abort paths, and
    # the requeue path, overwrite `run.result` wholesale with an error object
    # (`queue.ts:153`, `:164`, `:225`), so an agentSessionId published earlier
    # vanishes from later reads while the session, transcript and recording all
    # survive. Treating that as "no call ever ran" strands a completed review.
    assert "RECORD that id the first time you see it" in description
    assert "REPLACES" in description
    assert "a session may well exist" in description
    # The recovery has to name a tool the profile actually ships. "whatever list
    # this connector exposes" was unfollowable on builder and Replit, which had
    # no list tool at all - see test_the_run_read_ships_wherever_test_call_does.
    assert "agents.calls.list" in description

    # And it has to IDENTIFY the call, not guess it. That endpoint filters only
    # on agent + createdAt, so a concurrent call on the same agent can sort
    # first and a "newest wins" rule would review the wrong transcript. The
    # session carries the join: `metadata.evalRunId` + `runKind` are stamped at
    # `sessions.ts:1953`, and `omitAttributionMetadata` strips only
    # `_speko_attribution`, so `calls.get` publishes both.
    assert "metadata.evalRunId" in description
    assert "metadata.runKind" in description
    assert "Never just take the newest" in description

    # The run read has to be NAMED, not implied: agents.test_call answers with a
    # run id, and until this tool existed no MCP tool could turn that into the
    # agentSessionId every later step takes, so the flow stalled at dispatch.
    assert "agents.test_call.get" in description
    # With the `run` wrapper: the route answers {run: serializeEvalRun(row)},
    # so a client polling for a bare `result.` key never finds the session id.
    assert "run.result.testCall.agentSessionId" in description


async def test_run_read_description_warns_the_result_is_overwritten() -> None:
    """The tool that serves the id documents that the id can be taken away."""
    tools = {tool.name: tool for tool in await create_server().list_tools()}
    description = " ".join((tools["agents.test_call.get"].description or "").split())

    assert "run.result.testCall.agentSessionId" in description
    assert "NOT append-only" in description
    assert "DISAPPEARS" in description
    # The full status vocabulary, so a caller can tell in-flight from settled.
    for status in ("queued", "running", "passed", "failed", "aborted", "incomplete"):
        assert status in description


async def test_the_run_read_ships_wherever_test_call_does() -> None:
    """A profile that can start a test call must be able to resolve its run.

    Without the pairing, `agents.test_call` is a dead end on that surface: the
    202 carries only a run id. The connector and directory surfaces exclude both.
    """
    paired = [
        CHATGPT_PROFILE_TOOL_NAMES,
        BUILDER_PROFILE_TOOL_NAMES,
        REPLIT_PROFILE_TOOL_NAMES,
    ]
    for names in paired:
        assert ("agents.test_call" in names) == ("agents.test_call.get" in names)
        assert "agents.test_call.get" in names
        # And the recovery read, for when a timeout overwrites the run result
        # before the client recorded the session id. Without it the guidance
        # names a tool the profile does not have.
        assert "agents.calls.list" in names

    for excluded in (CONNECTOR_EXCLUDED_TOOL_NAMES, DIRECTORY_REQUIRED_ABSENT_TOOL_NAMES):
        assert "agents.test_call" in excluded
        assert "agents.test_call.get" in excluded


async def test_run_read_404_tells_the_model_to_check_the_id(
    recording_pending_api: list[str],
) -> None:
    """A run id typo must not read as "retry" either."""
    step = next_step_for_error(
        _raise_from({"error": "not found", "code": "NOT_FOUND"}),
        path="/v1/agents/agent_1/eval-runs/run_1",
    )

    assert step == NOT_FOUND_NEXT_STEP
