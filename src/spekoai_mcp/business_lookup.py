"""Provider clients for business phone lookup and carrier line-type checks.

These clients talk directly to third-party APIs (Google Places text search
and Twilio Lookup v2) using server-held environment keys — they do NOT go
through the authenticated Speko relay in ``spekoai_mcp.http_client``.

Error messages never include API keys, auth headers, or URLs carrying
credentials; provider response bodies are excerpted to at most 200 chars.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
TWILIO_LOOKUP_URL = "https://lookups.twilio.com/v2/PhoneNumbers"

PLACES_FIELD_MASK = (
    "places.displayName,places.formattedAddress,places.internationalPhoneNumber,"
    "places.utcOffsetMinutes,places.businessStatus"
)

_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None

_TIMEOUT_SECONDS = 15.0
_MIN_RESULT_COUNT = 1
_MAX_RESULT_COUNT = 5
_BODY_EXCERPT_CHARS = 200

# Keep in sync with the E.164 regex in spekoai_mcp.dial_token and action_tools.
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_PHONE_FORMATTING_RE = re.compile(r"[ \-().]")

_MISSING_PLACES_KEY_MESSAGE = (
    "GOOGLE_PLACES_API_KEY is not configured; set the GOOGLE_PLACES_API_KEY "
    "environment variable to a Google Places API key before calling search_places."
)
_MISSING_TWILIO_CREDS_MESSAGE = (
    "Twilio Lookup credentials are not configured; set TWILIO_LOOKUP_SID and "
    "TWILIO_LOOKUP_TOKEN (or TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) before "
    "calling lookup_line_type."
)


class ProviderError(RuntimeError):
    """Clean exception for third-party provider failures.

    Messages must never contain API keys, auth headers, or credentialed URLs.
    """

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message


@dataclass(frozen=True)
class PlaceCandidate:
    name: str
    address: str
    phone_e164: str
    utc_offset_minutes: int | None
    business_status: str | None


def normalize_phone(raw: str) -> str | None:
    """Normalize a formatted phone number to E.164, or None when not normalizable."""
    if not isinstance(raw, str):
        return None
    cleaned = _PHONE_FORMATTING_RE.sub("", raw.strip())
    if not _E164_RE.match(cleaned):
        return None
    return cleaned


def _places_api_key() -> str:
    """Return the Google Places API key from the environment."""
    key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
    if not key:
        raise ProviderError("places", _MISSING_PLACES_KEY_MESSAGE)
    return key


def _twilio_credentials() -> tuple[str, str]:
    """Return (sid, token) for Twilio Lookup basic auth from the environment."""
    sid = os.environ.get("TWILIO_LOOKUP_SID", "").strip()
    token = os.environ.get("TWILIO_LOOKUP_TOKEN", "").strip()
    if not sid or not token:
        sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
        token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not token:
        raise ProviderError("carrier_lookup", _MISSING_TWILIO_CREDS_MESSAGE)
    return sid, token


def _excerpt(resp: httpx.Response) -> str:
    """Return a short, credential-free excerpt of a provider response body."""
    text = resp.text.strip()
    if not text:
        return resp.reason_phrase or "no response body"
    return text[:_BODY_EXCERPT_CHARS]


async def _request(
    provider: str,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    auth: tuple[str, str] | None = None,
) -> Any:
    """Issue one provider HTTP request and return decoded JSON ({} on empty body)."""
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=True,
            transport=_TEST_TRANSPORT,
        ) as client:
            resp = await client.request(method, url, headers=headers, json=json_body, auth=auth)
    except httpx.HTTPError as exc:
        raise ProviderError(provider, f"request failed: {exc.__class__.__name__}: {exc}") from exc
    if resp.status_code >= 400:
        raise ProviderError(
            provider, f"provider returned HTTP {resp.status_code}: {_excerpt(resp)}"
        )
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderError(provider, "provider returned a non-JSON response.") from exc


def _parse_place(place: Any) -> PlaceCandidate | None:
    """Build a PlaceCandidate from one Places API entry; None when no usable phone."""
    if not isinstance(place, dict):
        return None
    raw_phone = place.get("internationalPhoneNumber")
    phone_e164 = normalize_phone(raw_phone) if isinstance(raw_phone, str) else None
    if phone_e164 is None:
        return None
    display = place.get("displayName")
    name = display.get("text") if isinstance(display, dict) else None
    address = place.get("formattedAddress")
    utc_offset = place.get("utcOffsetMinutes")
    business_status = place.get("businessStatus")
    return PlaceCandidate(
        name=name if isinstance(name, str) else "",
        address=address if isinstance(address, str) else "",
        phone_e164=phone_e164,
        utc_offset_minutes=(
            utc_offset if isinstance(utc_offset, int) and not isinstance(utc_offset, bool) else None
        ),
        business_status=(
            business_status if isinstance(business_status, str) and business_status else None
        ),
    )


async def search_places(
    name: str,
    location: str | None = None,
    *,
    limit: int = 3,
) -> list[PlaceCandidate]:
    """Resolve a business name to phone-dialable place candidates via Google Places."""
    if not isinstance(name, str) or not name.strip():
        raise ProviderError("places", "Business name must be a non-empty string.")
    api_key = _places_api_key()
    body = {
        "textQuery": f"{name} {location}" if location else name,
        "maxResultCount": min(max(limit, _MIN_RESULT_COUNT), _MAX_RESULT_COUNT),
    }
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": PLACES_FIELD_MASK}
    payload = await _request("places", "POST", PLACES_SEARCH_URL, headers=headers, json_body=body)
    places = payload.get("places") if isinstance(payload, dict) else None
    if not isinstance(places, list):
        return []
    candidates: list[PlaceCandidate] = []
    for place in places:
        candidate = _parse_place(place)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


async def lookup_line_type(e164: str) -> str | None:
    """Check the carrier line type of an E.164 number via Twilio Lookup v2."""
    if not isinstance(e164, str) or not _E164_RE.match(e164):
        raise ProviderError(
            "carrier_lookup",
            "Phone number must be in E.164 format such as '+12015551234' before a carrier lookup.",
        )
    sid, token = _twilio_credentials()
    url = f"{TWILIO_LOOKUP_URL}/{quote(e164, safe='')}?Fields=line_type_intelligence"
    payload = await _request("carrier_lookup", "GET", url, auth=(sid, token))
    if not isinstance(payload, dict):
        return None
    intelligence = payload.get("line_type_intelligence")
    if not isinstance(intelligence, dict):
        return None
    line_type = intelligence.get("type")
    return line_type if isinstance(line_type, str) and line_type else None
