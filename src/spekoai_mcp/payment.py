"""What the model reads when Platform refuses a request until the workspace pays.

One table for every payment refusal, on every tool: the out-of-credit
middleware, the handwritten tools' next steps and the manifest-generated tools
all render through `payment_step`, so the same refusal reads the same way
wherever it happens.

The text states facts and names tools; it does not script the assistant.
Anthropic's connector review rejects tool text that tells Claude how to behave
or promotes products, and the assistant words the answer better than a fixed
script does. No amount is suggested: the user picks it.
"""

from __future__ import annotations

import hashlib

from spekoai_mcp import http_client

INSUFFICIENT_CREDITS_CODE = http_client.INSUFFICIENT_CREDITS_CODE
DESTINATION_REQUIRES_CARD_CODE = "DESTINATION_REQUIRES_CARD"
PHONE_NUMBER_PROVISIONING_CREDITS_REQUIRED_CODE = "PHONE_NUMBER_PROVISIONING_CREDITS_REQUIRED"
PHONE_NUMBER_CARD_REQUIRED_CODE = "PHONE_NUMBER_CARD_REQUIRED"
VERIFIED_CALLER_ID_PAYMENT_REQUIRED_CODE = "VERIFIED_CALLER_ID_PAYMENT_REQUIRED"
PROVIDER_REQUIRES_TOP_UP_CODE = "PROVIDER_REQUIRES_TOP_UP"
PHONE_NUMBER_PAYMENT_REQUIRED_CODE = "PHONE_NUMBER_PAYMENT_REQUIRED"

# Refusals a top-up clears. A spending cap is not one: adding credit does not
# lift it, so it keeps its own step in `action_tools`.
PAYMENT_CODES = frozenset(
    {
        INSUFFICIENT_CREDITS_CODE,
        DESTINATION_REQUIRES_CARD_CODE,
        PHONE_NUMBER_PROVISIONING_CREDITS_REQUIRED_CODE,
        PHONE_NUMBER_CARD_REQUIRED_CODE,
        VERIFIED_CALLER_ID_PAYMENT_REQUIRED_CODE,
        PROVIDER_REQUIRES_TOP_UP_CODE,
        PHONE_NUMBER_PAYMENT_REQUIRED_CODE,
    }
)

# Surfaces whose model may hand the user a payment link. Not chatgpt: OpenAI
# bans checkout initiation in a plugin, links included.
LINK_PROFILES = frozenset({"connector", "customer", "muse"})
CHATGPT_PROFILE = "chatgpt"

# Surfaces where a refusal names no price, amount, top-up or way to pay. OpenAI's
# plugin guidelines bar advertising pricing or promotions (1.3) and promoting
# upgrades (5.1), but allow saying a feature is not available on the user's
# current plan, which is what these surfaces say instead.
NO_PRICING_PROFILES = frozenset({CHATGPT_PROFILE})

TOP_UP_LINK_TOOL = "billing.top_up_link.create"

# The Add credit button experiment. `apps/server/src/services/payment-wall.ts`
# computes the same bucket, so the server labels `paywall_refused` with the arm
# this text showed without any header. Change both together.
_ARM_SALT = "paywall-button-v1:"
BUTTON_ARM = "button"
TEXT_ARM = "text"

_CAUSES = {
    DESTINATION_REQUIRES_CARD_CODE: (
        "Free credit covers calls in the US and Canada. This call needs one top-up of any "
        "amount, from $5, and after it every country stays unlocked. Buying a phone number "
        "does not unlock this call."
    ),
    PHONE_NUMBER_PROVISIONING_CREDITS_REQUIRED_CODE: (
        "The workspace needs more credit to buy the dedicated phone number for this call."
    ),
    PHONE_NUMBER_CARD_REQUIRED_CODE: (
        "The workspace's phone number was released because it was not paid for. One top-up "
        "gets a new number on the next call."
    ),
    VERIFIED_CALLER_ID_PAYMENT_REQUIRED_CODE: (
        "Calling from the workspace's own verified number needs one paid top-up of any "
        "amount first. Free credit does not count."
    ),
    PHONE_NUMBER_PAYMENT_REQUIRED_CODE: (
        "Buying a phone number needs one paid top-up of any amount, from $5, until the "
        "workspace's business verification is approved. Free credit does not count."
    ),
    INSUFFICIENT_CREDITS_CODE: "The workspace is out of credit.",
    PROVIDER_REQUIRES_TOP_UP_CODE: (
        "ElevenLabs runs on Speko's key after the workspace's first top-up of any amount, "
        "from $5. Free credit covers every other provider: the same request without the "
        "ElevenLabs provider pin, voice and model is served by another provider."
    ),
}

# The same refusals where the surface names no price or way to pay. Each says
# what is not available and what did not happen.
_PLAN_CAUSES = {
    DESTINATION_REQUIRES_CARD_CODE: (
        "Calls to this country are not available on the workspace's current balance or "
        "plan. The call was not placed and nothing was charged."
    ),
    PHONE_NUMBER_PROVISIONING_CREDITS_REQUIRED_CODE: (
        "This call needs a dedicated phone number, which is not available on the "
        "workspace's current balance or plan. The call was not placed and nothing was charged."
    ),
    PHONE_NUMBER_CARD_REQUIRED_CODE: (
        "The workspace has no active phone number, and a new one is not available on its "
        "current balance or plan. The call was not placed and nothing was charged."
    ),
    VERIFIED_CALLER_ID_PAYMENT_REQUIRED_CODE: (
        "Calling from the workspace's own verified number is not available on its current "
        "balance or plan. Nothing was charged."
    ),
    PHONE_NUMBER_PAYMENT_REQUIRED_CODE: (
        "A new dedicated phone number is not available on the workspace's current balance or "
        "plan. Nothing was charged."
    ),
    INSUFFICIENT_CREDITS_CODE: (
        "This request is not available on the workspace's current balance or plan. "
        "Nothing was charged."
    ),
    PROVIDER_REQUIRES_TOP_UP_CODE: (
        "ElevenLabs is not available on the workspace's current balance or plan, and "
        "nothing was charged. The same request without the ElevenLabs provider pin, voice "
        "and model is served by another provider."
    ),
}

_AFTER_PAYMENT = (
    "Credit shows in credits.balance.get about a minute after payment; until then this "
    "request returns the same refusal."
)


def paywall_arm(organization_id: str) -> str:
    """The experiment bucket for an organization: stable and 50/50."""
    first = hashlib.sha256(f"{_ARM_SALT}{organization_id}".encode()).digest()[0]
    return BUTTON_ARM if first % 2 == 1 else TEXT_ARM


def session_arm() -> str:
    """This session's bucket; `text` when the token names no organization."""
    from spekoai_mcp.profiles import get_access_token

    claims = getattr(get_access_token(), "claims", None)
    organization_id = claims.get("organization_id") if isinstance(claims, dict) else None
    if isinstance(organization_id, str) and organization_id:
        return paywall_arm(organization_id)
    return TEXT_ARM


def is_payment_refusal(exc: BaseException) -> bool:
    if not isinstance(exc, http_client.SpekoApiError):
        return False
    return exc.credit_exhausted or exc.code in PAYMENT_CODES


# Tools that quote a price or take a payment. A surface that serves none of them
# has no way to act on a price, so its tool text names none.
PRICING_TOOLS = ("sessions.phone.quote", TOP_UP_LINK_TOOL, http_client.CHECKOUT_ACTION_ID)


def profile_shows_prices(profile: str | None) -> bool:
    """Whether tool text on ``profile`` may name prices and top-ups."""
    from spekoai_mcp.profiles import profile_serves_tool

    if profile in NO_PRICING_PROFILES:
        return False
    return any(profile_serves_tool(name, profile) for name in PRICING_TOOLS)


def hides_pricing() -> bool:
    """Whether this deployment's surface names no price, top-up or way to pay."""
    from spekoai_mcp.profiles import current_profile

    return current_profile() in NO_PRICING_PROFILES


def cause(exc: http_client.SpekoApiError) -> str:
    code = INSUFFICIENT_CREDITS_CODE if exc.credit_exhausted else (exc.code or "")
    if hides_pricing():
        return _PLAN_CAUSES.get(code, _PLAN_CAUSES[INSUFFICIENT_CREDITS_CODE])
    return _CAUSES.get(code, "")


def remedy(exc: http_client.SpekoApiError) -> str:
    """How this session pays, with the refusal's one-tap link where the surface allows it."""
    return how_to_pay(exc.top_up_url)


def how_to_pay(top_up_url: str | None) -> str:
    """How this session pays: the link if the surface may show one, else the tool or page."""
    from spekoai_mcp.profiles import current_profile, session_can_call

    profile = current_profile()
    if profile in NO_PRICING_PROFILES:
        return ""
    url = top_up_url if profile in LINK_PROFILES else None
    button = session_can_call(TOP_UP_LINK_TOOL)
    if url:
        parts = [
            f"Top-up link: {url} It opens a secure payment page where the user picks the "
            "amount, from $5 (Apple Pay and Link work)."
        ]
        if button and session_arm() == BUTTON_ARM:
            parts.append(f"{TOP_UP_LINK_TOOL} shows this link to the user as an Add credit button.")
        parts.append(_AFTER_PAYMENT)
        return " ".join(parts)
    if button:
        return (
            f"{TOP_UP_LINK_TOOL} returns a payment link where the user picks the amount, "
            f"from $5. {_AFTER_PAYMENT}"
        )
    if http_client.checkout_available():
        return (
            f"{http_client.CHECKOUT_ACTION_ID} returns a payment link for an amount the user "
            f"names, from $5 to $10,000. {_AFTER_PAYMENT}"
        )
    return f"A workspace owner or admin adds credit at {http_client.BILLING_URL}. {_AFTER_PAYMENT}"


def payment_step(exc: http_client.SpekoApiError) -> str:
    """The next step for a payment refusal: what blocks the request, then how to pay."""
    return " ".join(part for part in (cause(exc), remedy(exc)) if part)
