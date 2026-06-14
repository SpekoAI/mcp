"""Signed, short-lived dial tokens and pure call-safety predicates.

A dial token is the ONLY way a phone number can reach ``make_call``:
``lookup_business`` mints one after a carrier check, and ``make_call``
verifies it before dialing. Tokens are compact JSON payloads signed with
HMAC-SHA256 using the ``SPEKO_DIAL_TOKEN_SECRET`` shared secret.

The pure predicates (``dial_blocked_reason``, ``line_type_blocked_reason``,
``quiet_hours_reason``) gate which destinations may be dialed and when.
They return ``None`` when the call is allowed, or a human-readable reason
string when it is blocked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_TTL_SECONDS = 900

SECRET_ENV_VAR = "SPEKO_DIAL_TOKEN_SECRET"

ALLOWED_LINE_TYPES = frozenset({"landline", "fixedVoip", "nonFixedVoip", "tollFree", "voip"})

# Keep in sync with the E.164 regex in spekoai_mcp.action_tools.
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_US_PREMIUM_RE = re.compile(r"^\+1(900|976)\d{7}$")
_EMERGENCY_NUMBERS = frozenset({"+911", "+1911", "+112", "+999", "+988", "+1988"})
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")

_QUIET_START_HOUR = 21
_QUIET_END_HOUR = 8

_MISSING_SECRET_MESSAGE = (
    "Dial token secret is not configured; set the SPEKO_DIAL_TOKEN_SECRET environment "
    "variable to a non-empty value (or pass secret=) before minting or verifying dial tokens."
)
_MALFORMED_TOKEN_MESSAGE = (
    "Malformed dial token: expected two dot-separated base64url parts "
    "('<payload>.<signature>') produced by lookup_business; run lookup_business "
    "again to mint a fresh dial token."
)
_BAD_SIGNATURE_MESSAGE = (
    "Dial token signature check failed: the token was altered or signed with a "
    "different secret; run lookup_business again to mint a fresh dial token."
)
_ACCOUNT_MISMATCH_MESSAGE = (
    "Dial token was minted for a different account; run lookup_business again to "
    "mint a dial token for the current credentials."
)


class DialTokenError(ValueError):
    """Raised when a dial token cannot be minted or verified."""


def _resolve_secret(secret: str | None) -> str:
    """Return the signing secret, falling back to SPEKO_DIAL_TOKEN_SECRET."""
    resolved = secret if secret is not None else os.environ.get(SECRET_ENV_VAR, "")
    if not resolved:
        raise DialTokenError(_MISSING_SECRET_MESSAGE)
    return resolved


def _b64url_encode(raw: bytes) -> str:
    """Encode bytes as padded base64url text."""
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64url_decode(value: str) -> bytes:
    """Decode base64url text (padded or unpadded); raise DialTokenError if invalid."""
    if not _B64URL_RE.match(value):
        raise DialTokenError(_MALFORMED_TOKEN_MESSAGE)
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except ValueError as exc:
        raise DialTokenError(_MALFORMED_TOKEN_MESSAGE) from exc


def _signature(secret: str, payload_json: bytes) -> bytes:
    """Compute the HMAC-SHA256 signature for a serialized payload."""
    return hmac.new(secret.encode("utf-8"), payload_json, hashlib.sha256).digest()


def mint_dial_token(
    *,
    e164: str,
    line_type: str,
    business_name: str,
    utc_offset_minutes: int | None,
    bearer_hash: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    secret: str | None = None,
    now: float | None = None,
) -> str:
    """Mint a signed, short-lived dial token authorizing make_call for one number."""
    resolved_secret = _resolve_secret(secret)
    issued_at = time.time() if now is None else now
    payload: dict[str, Any] = {
        "v": 1,
        "e164": e164,
        "line_type": line_type,
        "business_name": business_name,
        "utc_offset_minutes": utc_offset_minutes,
        "bh": bearer_hash,
        "exp": int(issued_at + ttl_seconds),
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = _signature(resolved_secret, payload_json)
    return f"{_b64url_encode(payload_json)}.{_b64url_encode(signature)}"


def verify_dial_token(
    token: str,
    *,
    expected_bearer_hash: str | None = None,
    secret: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify a dial token minted by lookup_business and return its payload."""
    resolved_secret = _resolve_secret(secret)
    if not isinstance(token, str):
        raise DialTokenError(_MALFORMED_TOKEN_MESSAGE)
    parts = token.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise DialTokenError(_MALFORMED_TOKEN_MESSAGE)
    payload_json = _b64url_decode(parts[0])
    provided_signature = _b64url_decode(parts[1])
    try:
        payload = json.loads(payload_json)
    except ValueError as exc:
        raise DialTokenError(_MALFORMED_TOKEN_MESSAGE) from exc
    if not isinstance(payload, dict):
        raise DialTokenError(_MALFORMED_TOKEN_MESSAGE)
    expected_signature = _signature(resolved_secret, payload_json)
    if not hmac.compare_digest(provided_signature, expected_signature):
        raise DialTokenError(_BAD_SIGNATURE_MESSAGE)
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        raise DialTokenError(_MALFORMED_TOKEN_MESSAGE)
    current = time.time() if now is None else now
    if current >= exp:
        raise DialTokenError(
            f"Dial token expired at epoch {int(exp)}; run lookup_business again to "
            "mint a fresh dial token."
        )
    bearer_hash = payload.get("bh")
    if bearer_hash is not None and bearer_hash != expected_bearer_hash:
        raise DialTokenError(_ACCOUNT_MISMATCH_MESSAGE)
    return payload


def dial_blocked_reason(e164: str) -> str | None:
    """Return why the number may not be dialed, or None when dialing is allowed."""
    if not isinstance(e164, str):
        return "Phone number must be a string in E.164 format such as '+12015551234'."
    if e164 in _EMERGENCY_NUMBERS:
        return (
            f"Dialing {e164} is blocked: emergency and crisis numbers may not be "
            "called by automated agents."
        )
    if not _E164_RE.match(e164):
        return (
            f"'{e164}' is not a valid E.164 phone number such as '+12015551234'; "
            "run lookup_business to resolve a dialable business number."
        )
    if _US_PREMIUM_RE.match(e164):
        return (
            f"Dialing {e164} is blocked: US premium-rate numbers (+1-900 and +1-976) "
            "may not be called."
        )
    return None


def line_type_blocked_reason(line_type: str | None) -> str | None:
    """Return why the carrier line type may not be dialed, or None when allowed."""
    allowed = ", ".join(sorted(ALLOWED_LINE_TYPES))
    if line_type == "mobile":
        return (
            "Line type 'mobile' is blocked: the business-lines-only policy forbids "
            "calling personal mobile numbers; only business line types "
            f"({allowed}) may be dialed."
        )
    if line_type is None:
        return (
            "Line type is unknown; calls are blocked until lookup_business confirms "
            f"a business line type ({allowed})."
        )
    if line_type not in ALLOWED_LINE_TYPES:
        return (
            f"Line type '{line_type}' is not an allowed business line type; "
            f"allowed line types: {allowed}."
        )
    return None


def quiet_hours_reason(utc_offset_minutes: int | None, *, now: float | None = None) -> str | None:
    """Return why calling now violates destination quiet hours, or None when allowed.

    Fails closed: an unknown destination UTC offset blocks the call, mirroring
    how an unknown line type blocks in ``line_type_blocked_reason``.
    """
    if utc_offset_minutes is None:
        return (
            "Destination UTC offset is unknown, so quiet hours (08:00-21:00 "
            "destination local time) cannot be verified; calls to this number "
            "are blocked."
        )
    current = time.time() if now is None else now
    local = datetime.fromtimestamp(current, tz=timezone.utc) + timedelta(
        minutes=utc_offset_minutes
    )
    if local.hour >= _QUIET_START_HOUR or local.hour < _QUIET_END_HOUR:
        return (
            f"Destination local time is {local:%H:%M}, inside quiet hours "
            "(21:00-08:00); wait until between 08:00 and 21:00 destination time."
        )
    return None
