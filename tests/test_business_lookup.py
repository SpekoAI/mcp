"""Tests for `spekoai_mcp.business_lookup` — Google Places and Twilio Lookup clients.

Provider HTTP traffic is faked with `httpx.MockTransport` installed on the
module-level `_TEST_TRANSPORT` hook via monkeypatch, mirroring how the suite
fakes the Speko relay transport in `spekoai_mcp.http_client`. Environment
keys are fake values set with `monkeypatch.setenv` — no real credentials.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

import spekoai_mcp.business_lookup as business_lookup
from spekoai_mcp.business_lookup import (
    PlaceCandidate,
    ProviderError,
    lookup_line_type,
    normalize_phone,
    search_places,
)

Handler = Callable[[httpx.Request], httpx.Response]
InstallTransport = Callable[[Handler], list[dict[str, Any]]]

RAW_PHONE = "+1 415-555-0132"
NORMALIZED_PHONE = "+14155550132"

PLACES_KEY = "places-test-key"
TWILIO_SID = "ACtest"
TWILIO_TOKEN = "twilio-test-token"


@pytest.fixture
def provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", PLACES_KEY)
    monkeypatch.setenv("TWILIO_LOOKUP_SID", TWILIO_SID)
    monkeypatch.setenv("TWILIO_LOOKUP_TOKEN", TWILIO_TOKEN)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)


@pytest.fixture
def install_transport(monkeypatch: pytest.MonkeyPatch) -> InstallTransport:
    calls: list[dict[str, Any]] = []

    def _install(handler: Handler) -> list[dict[str, Any]]:
        def recording_handler(request: httpx.Request) -> httpx.Response:
            calls.append(
                {
                    "method": request.method,
                    "url": str(request.url),
                    "auth": request.headers.get("authorization"),
                    "api_key": request.headers.get("x-goog-api-key"),
                    "field_mask": request.headers.get("x-goog-fieldmask"),
                    "body": json.loads(request.content.decode("utf-8") or "{}"),
                }
            )
            return handler(request)

        monkeypatch.setattr(
            business_lookup, "_TEST_TRANSPORT", httpx.MockTransport(recording_handler)
        )
        return calls

    return _install


def places_payload() -> dict[str, Any]:
    return {
        "places": [
            {
                "displayName": {"text": "Joe's Pizza"},
                "formattedAddress": "7 Carmine St, New York, NY 10014, USA",
                "internationalPhoneNumber": RAW_PHONE,
                "utcOffsetMinutes": -240,
                "businessStatus": "OPERATIONAL",
            },
            {
                "displayName": {"text": "No Phone Deli"},
                "formattedAddress": "1 Nowhere Ave, New York, NY, USA",
                "utcOffsetMinutes": -240,
                "businessStatus": "OPERATIONAL",
            },
        ]
    }


# ── normalize_phone ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+1 415-555-0132", "+14155550132"),
        ("+1 (415) 555.0132", "+14155550132"),
        ("+442071234567", "+442071234567"),
        ("415-555-0132", None),  # no leading +
        ("+0 415 555 0132", None),  # leading zero country code
        ("+1 415 555 0132 ext 4", None),  # leftover non-digits
        ("", None),
    ],
)
def test_normalize_phone(raw: str, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


# ── search_places ────────────────────────────────────────────────────


async def test_search_places_normalizes_phone_and_propagates_offset(
    provider_env: None,
    install_transport: InstallTransport,
) -> None:
    calls = install_transport(lambda request: httpx.Response(200, json=places_payload()))
    candidates = await search_places("Joe's Pizza", "New York", limit=2)
    assert candidates == [
        PlaceCandidate(
            name="Joe's Pizza",
            address="7 Carmine St, New York, NY 10014, USA",
            phone_e164=NORMALIZED_PHONE,
            utc_offset_minutes=-240,
            business_status="OPERATIONAL",
        )
    ]
    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://places.googleapis.com/v1/places:searchText"
    assert calls[0]["api_key"] == PLACES_KEY
    assert calls[0]["field_mask"] == (
        "places.displayName,places.formattedAddress,places.internationalPhoneNumber,"
        "places.utcOffsetMinutes,places.businessStatus"
    )
    assert calls[0]["body"] == {"textQuery": "Joe's Pizza New York", "maxResultCount": 2}


async def test_search_places_clamps_limit_and_omits_location(
    provider_env: None,
    install_transport: InstallTransport,
) -> None:
    calls = install_transport(lambda request: httpx.Response(200, json={"places": []}))
    assert await search_places("Joe's Pizza", limit=99) == []
    assert calls[0]["body"] == {"textQuery": "Joe's Pizza", "maxResultCount": 5}


async def test_search_places_skips_places_without_usable_phone(
    provider_env: None,
    install_transport: InstallTransport,
) -> None:
    payload = {
        "places": [
            {
                "displayName": {"text": "No Phone Deli"},
                "formattedAddress": "1 Nowhere Ave",
            },
            {
                "displayName": {"text": "Bad Phone Bar"},
                "formattedAddress": "2 Nowhere Ave",
                "internationalPhoneNumber": "call us!",
            },
        ]
    }
    install_transport(lambda request: httpx.Response(200, json=payload))
    assert await search_places("deli") == []


async def test_search_places_missing_api_key_raises_provider_error(
    monkeypatch: pytest.MonkeyPatch,
    install_transport: InstallTransport,
) -> None:
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    calls = install_transport(lambda request: httpx.Response(200, json=places_payload()))
    with pytest.raises(ProviderError, match="GOOGLE_PLACES_API_KEY is not configured") as excinfo:
        await search_places("Joe's Pizza")
    assert str(excinfo.value).startswith("places: ")
    assert excinfo.value.provider == "places"
    assert PLACES_KEY not in str(excinfo.value)
    assert calls == []  # fails before any HTTP request


async def test_search_places_403_surfaces_provider_error_with_status(
    provider_env: None,
    install_transport: InstallTransport,
) -> None:
    install_transport(
        lambda request: httpx.Response(
            403, json={"error": {"message": "Permission denied", "status": "PERMISSION_DENIED"}}
        )
    )
    with pytest.raises(ProviderError, match="HTTP 403") as excinfo:
        await search_places("Joe's Pizza")
    assert excinfo.value.provider == "places"
    assert "Permission denied" in str(excinfo.value)
    assert PLACES_KEY not in str(excinfo.value)


# ── lookup_line_type ─────────────────────────────────────────────────


async def test_lookup_line_type_returns_mobile(
    provider_env: None,
    install_transport: InstallTransport,
) -> None:
    calls = install_transport(
        lambda request: httpx.Response(
            200,
            json={
                "phone_number": NORMALIZED_PHONE,
                "line_type_intelligence": {"type": "mobile", "carrier_name": "Example Wireless"},
            },
        )
    )
    assert await lookup_line_type(NORMALIZED_PHONE) == "mobile"
    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert "%2B14155550132" in calls[0]["url"]  # urlencoded e164 in the path
    assert "Fields=line_type_intelligence" in calls[0]["url"]
    basic = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode("ascii")
    assert calls[0]["auth"] == f"Basic {basic}"


@pytest.mark.parametrize(
    "payload",
    [
        {"phone_number": NORMALIZED_PHONE},  # key missing entirely
        {"line_type_intelligence": None},  # null
        {"line_type_intelligence": {}},  # type missing
        {"line_type_intelligence": {"type": None}},  # type null
    ],
)
async def test_lookup_line_type_missing_intelligence_returns_none(
    provider_env: None,
    install_transport: InstallTransport,
    payload: dict[str, Any],
) -> None:
    install_transport(lambda request: httpx.Response(200, json=payload))
    assert await lookup_line_type(NORMALIZED_PHONE) is None


async def test_lookup_line_type_falls_back_to_account_credentials(
    monkeypatch: pytest.MonkeyPatch,
    install_transport: InstallTransport,
) -> None:
    monkeypatch.delenv("TWILIO_LOOKUP_SID", raising=False)
    monkeypatch.delenv("TWILIO_LOOKUP_TOKEN", raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACfallback")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fallback-token")
    calls = install_transport(
        lambda request: httpx.Response(200, json={"line_type_intelligence": {"type": "landline"}})
    )
    assert await lookup_line_type(NORMALIZED_PHONE) == "landline"
    basic = base64.b64encode(b"ACfallback:fallback-token").decode("ascii")
    assert calls[0]["auth"] == f"Basic {basic}"


async def test_lookup_line_type_missing_credentials_raises(
    monkeypatch: pytest.MonkeyPatch,
    install_transport: InstallTransport,
) -> None:
    for var in (
        "TWILIO_LOOKUP_SID",
        "TWILIO_LOOKUP_TOKEN",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    calls = install_transport(lambda request: httpx.Response(200, json={}))
    with pytest.raises(
        ProviderError, match="Twilio Lookup credentials are not configured"
    ) as excinfo:
        await lookup_line_type(NORMALIZED_PHONE)
    assert excinfo.value.provider == "carrier_lookup"
    assert str(excinfo.value).startswith("carrier_lookup: ")
    assert calls == []  # fails before any HTTP request


async def test_lookup_line_type_error_status_raises_without_leaking_credentials(
    provider_env: None,
    install_transport: InstallTransport,
) -> None:
    install_transport(
        lambda request: httpx.Response(404, json={"code": 20404, "message": "not found"})
    )
    with pytest.raises(ProviderError, match="HTTP 404") as excinfo:
        await lookup_line_type(NORMALIZED_PHONE)
    assert excinfo.value.provider == "carrier_lookup"
    assert TWILIO_TOKEN not in str(excinfo.value)
    assert TWILIO_SID not in str(excinfo.value)
