"""Tests for `spekoai_mcp.dial_token` — signed dial tokens and safety predicates.

All time-dependent assertions use explicit `now` epochs (never sleep), and
secrets are passed explicitly except where the SPEKO_DIAL_TOKEN_SECRET env
fallback is itself under test.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from spekoai_mcp.dial_token import (
    ALLOWED_LINE_TYPES,
    DEFAULT_TTL_SECONDS,
    DialTokenError,
    dial_blocked_reason,
    line_type_blocked_reason,
    mint_dial_token,
    quiet_hours_reason,
    verify_dial_token,
)

SECRET = "test-secret"
NOW = 1_750_000_000.0


def _mint(**overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "e164": "+12015551234",
        "line_type": "landline",
        "business_name": "Joe's Pizza",
        "utc_offset_minutes": -300,
        "bearer_hash": None,
        "secret": SECRET,
        "now": NOW,
    }
    kwargs.update(overrides)
    return mint_dial_token(**kwargs)


def _split(token: str) -> tuple[str, str]:
    payload_b64, signature_b64 = token.split(".")
    return payload_b64, signature_b64


# ── mint / verify roundtrip ──────────────────────────────────────────


def test_mint_verify_roundtrip_preserves_all_fields() -> None:
    token = _mint(bearer_hash="hash-abc", ttl_seconds=600)
    payload = verify_dial_token(token, expected_bearer_hash="hash-abc", secret=SECRET, now=NOW)
    assert payload == {
        "v": 1,
        "e164": "+12015551234",
        "line_type": "landline",
        "business_name": "Joe's Pizza",
        "utc_offset_minutes": -300,
        "bh": "hash-abc",
        "exp": int(NOW + 600),
    }


def test_default_ttl_is_900_seconds() -> None:
    assert DEFAULT_TTL_SECONDS == 900
    token = _mint()
    payload = verify_dial_token(token, secret=SECRET, now=NOW + DEFAULT_TTL_SECONDS - 1)
    assert payload["exp"] == int(NOW + DEFAULT_TTL_SECONDS)


# ── tampering ────────────────────────────────────────────────────────


def test_tampered_payload_fails_with_signature_error() -> None:
    payload_b64, signature_b64 = _split(_mint())
    payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")))
    payload["e164"] = "+19995550000"
    forged_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    forged_b64 = base64.urlsafe_b64encode(forged_json).decode("ascii")
    with pytest.raises(DialTokenError, match="signature"):
        verify_dial_token(f"{forged_b64}.{signature_b64}", secret=SECRET, now=NOW)


def test_tampered_signature_fails() -> None:
    payload_b64, signature_b64 = _split(_mint())
    raw = bytearray(base64.urlsafe_b64decode(signature_b64.encode("ascii")))
    raw[0] ^= 0xFF
    forged_sig = base64.urlsafe_b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(DialTokenError, match="signature"):
        verify_dial_token(f"{payload_b64}.{forged_sig}", secret=SECRET, now=NOW)


def test_wrong_secret_fails_with_signature_error() -> None:
    token = _mint()
    with pytest.raises(DialTokenError, match="signature"):
        verify_dial_token(token, secret="other-secret", now=NOW)


# ── expiry ───────────────────────────────────────────────────────────


def test_expired_token_fails() -> None:
    token = _mint(now=1_000.0, ttl_seconds=60)
    assert verify_dial_token(token, secret=SECRET, now=1_059.0)["exp"] == 1_060
    with pytest.raises(DialTokenError, match="expired"):
        verify_dial_token(token, secret=SECRET, now=1_060.0)
    with pytest.raises(DialTokenError, match="expired"):
        verify_dial_token(token, secret=SECRET, now=2_000.0)


# ── secret resolution ────────────────────────────────────────────────


def test_missing_secret_mentions_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPEKO_DIAL_TOKEN_SECRET", raising=False)
    with pytest.raises(DialTokenError, match="SPEKO_DIAL_TOKEN_SECRET"):
        _mint(secret=None)
    with pytest.raises(DialTokenError, match="SPEKO_DIAL_TOKEN_SECRET"):
        verify_dial_token("a.b", now=NOW)


def test_empty_secret_mentions_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEKO_DIAL_TOKEN_SECRET", "")
    with pytest.raises(DialTokenError, match="SPEKO_DIAL_TOKEN_SECRET"):
        _mint(secret=None)
    with pytest.raises(DialTokenError, match="SPEKO_DIAL_TOKEN_SECRET"):
        _mint(secret="")


def test_secret_defaults_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEKO_DIAL_TOKEN_SECRET", "env-secret")
    token = _mint(secret=None)
    payload = verify_dial_token(token, now=NOW)
    assert payload["e164"] == "+12015551234"


# ── bearer hash binding ──────────────────────────────────────────────


def test_bearer_hash_mismatch_fails() -> None:
    token = _mint(bearer_hash="hash-abc")
    with pytest.raises(DialTokenError, match="different account"):
        verify_dial_token(token, expected_bearer_hash="hash-xyz", secret=SECRET, now=NOW)
    with pytest.raises(DialTokenError, match="different account"):
        verify_dial_token(token, expected_bearer_hash=None, secret=SECRET, now=NOW)


def test_bearer_hash_match_passes() -> None:
    token = _mint(bearer_hash="hash-abc")
    payload = verify_dial_token(token, expected_bearer_hash="hash-abc", secret=SECRET, now=NOW)
    assert payload["bh"] == "hash-abc"


def test_token_without_bearer_hash_verifies_regardless_of_expected() -> None:
    token = _mint(bearer_hash=None)
    assert verify_dial_token(token, secret=SECRET, now=NOW)["bh"] is None
    payload = verify_dial_token(token, expected_bearer_hash="anything", secret=SECRET, now=NOW)
    assert payload["bh"] is None


# ── malformed tokens ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "token",
    ["", "no-dot", "a.b.c", ".", "a.", ".b", "!!!.???", "a.b"],
)
def test_malformed_tokens_fail_structurally(token: str) -> None:
    with pytest.raises(DialTokenError, match="[Mm]alformed"):
        verify_dial_token(token, secret=SECRET, now=NOW)


def test_non_json_payload_is_malformed() -> None:
    payload_b64 = base64.urlsafe_b64encode(b"not json").decode("ascii")
    sig_b64 = base64.urlsafe_b64encode(b"x" * 32).decode("ascii")
    with pytest.raises(DialTokenError, match="[Mm]alformed"):
        verify_dial_token(f"{payload_b64}.{sig_b64}", secret=SECRET, now=NOW)


def test_non_dict_json_payload_is_malformed() -> None:
    payload_b64 = base64.urlsafe_b64encode(b"[1,2,3]").decode("ascii")
    sig_b64 = base64.urlsafe_b64encode(b"x" * 32).decode("ascii")
    with pytest.raises(DialTokenError, match="[Mm]alformed"):
        verify_dial_token(f"{payload_b64}.{sig_b64}", secret=SECRET, now=NOW)


# ── dial_blocked_reason ──────────────────────────────────────────────


@pytest.mark.parametrize("number", ["+12015551234", "+442071234567", "+18005551234"])
def test_dial_blocked_reason_allows_valid_business_numbers(number: str) -> None:
    assert dial_blocked_reason(number) is None


@pytest.mark.parametrize("number", ["+1911", "+911", "+112", "+999", "+988", "+1988"])
def test_dial_blocked_reason_blocks_emergency_numbers(number: str) -> None:
    assert dial_blocked_reason(number) is not None


@pytest.mark.parametrize("number", ["+19005551234", "+19765551234"])
def test_dial_blocked_reason_blocks_us_premium_numbers(number: str) -> None:
    assert dial_blocked_reason(number) is not None


@pytest.mark.parametrize(
    "number",
    ["+12345", "garbage", "", "12015551234", "+0123456789", "+1 201 555 1234"],
)
def test_dial_blocked_reason_blocks_short_codes_and_garbage(number: str) -> None:
    assert dial_blocked_reason(number) is not None


# ── line_type_blocked_reason ─────────────────────────────────────────


@pytest.mark.parametrize("line_type", sorted(ALLOWED_LINE_TYPES))
def test_line_type_blocked_reason_allows_business_lines(line_type: str) -> None:
    assert line_type_blocked_reason(line_type) is None


def test_line_type_blocked_reason_blocks_mobile_with_policy_message() -> None:
    reason = line_type_blocked_reason("mobile")
    assert reason is not None
    assert "business" in reason


@pytest.mark.parametrize("line_type", [None, "personal", "", "LANDLINE", "pager"])
def test_line_type_blocked_reason_fails_closed(line_type: str | None) -> None:
    assert line_type_blocked_reason(line_type) is not None


# ── quiet_hours_reason ───────────────────────────────────────────────


def _utc_epoch(hour: int, minute: int) -> float:
    return datetime(2026, 6, 11, hour, minute, tzinfo=timezone.utc).timestamp()


def test_quiet_hours_local_2059_allowed() -> None:
    assert quiet_hours_reason(0, now=_utc_epoch(20, 59)) is None


def test_quiet_hours_local_2100_blocked() -> None:
    reason = quiet_hours_reason(0, now=_utc_epoch(21, 0))
    assert reason is not None
    assert "21:00" in reason


def test_quiet_hours_local_0759_blocked() -> None:
    reason = quiet_hours_reason(0, now=_utc_epoch(7, 59))
    assert reason is not None
    assert "07:59" in reason


def test_quiet_hours_local_0800_allowed() -> None:
    assert quiet_hours_reason(0, now=_utc_epoch(8, 0)) is None


def test_quiet_hours_offset_none_fails_closed() -> None:
    # Unknown destination offset blocks the call even in daytime UTC.
    for hour in (12, 23):
        reason = quiet_hours_reason(None, now=_utc_epoch(hour, 0))
        assert reason is not None
        assert "unknown" in reason


def test_quiet_hours_applies_destination_offset() -> None:
    # UTC 12:00 + 540 minutes => local 21:00 (blocked); + 539 => 20:59 (allowed).
    assert quiet_hours_reason(540, now=_utc_epoch(12, 0)) is not None
    assert quiet_hours_reason(539, now=_utc_epoch(12, 0)) is None
    # UTC 02:00 - 300 minutes => local 21:00 the previous day (blocked).
    reason = quiet_hours_reason(-300, now=_utc_epoch(2, 0))
    assert reason is not None
    assert "21:00" in reason
