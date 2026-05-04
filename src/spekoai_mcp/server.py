"""FastMCP v3 server exposing SpekoAI knowledge to MCP clients.

Surfaces:

- **Resources** — `spekoai://docs/index` and `spekoai://docs/{slug}` ship
  every SDK/adapter's README + SKILLS.md + CLAUDE.md + a quickstart
  example inside the wheel. See `docs.py`, `resources.py`.
- **Prompts** — `scaffold_project` (scenario, language, runtime) walks
  an MCP client through bootstrapping a SpekoAI project. See `prompts.py`.
- **Tools** — `search_docs` (full-text over bundled docs) and
  `list_packages` (structured manifest).

Auth model: today every surface ships static bundled data, so OAuth is
not required to use this server. The wiring is retained end-to-end
(`auth.py` builds an `OAuthProxy`; `create_server(auth=)` accepts one;
the CLI mounts it if env vars are present) — when future tools need
the caller's identity they can declare `auth=` per-component and the
deployment flips OAuth env vars on. Do not remove the auth plumbing
to simplify; it's intentionally future-ready.
"""

from __future__ import annotations

import os
from typing import Annotated, Literal

import httpx
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import OAuthProxy
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from spekoai_mcp import recommendations, scaffolds, search
from spekoai_mcp.components import register_components
from spekoai_mcp.docs import DocEntry, load_manifest
from spekoai_mcp.prompts import register_prompts
from spekoai_mcp.resources import register_resources


class OrganizationBalance(BaseModel):
    """Credit balance for the caller's organization."""

    balance_micro_usd: str = Field(
        description=(
            "Balance in micro-USD (1_000_000 µ$ = $1). Serialized as a "
            "string so >2**53 values round-trip losslessly over JSON."
        ),
    )
    balance_usd: float = Field(
        description="Same value as balance_micro_usd, expressed as USD."
    )
    updated_at: str = Field(
        description="ISO-8601 timestamp of the last balance mutation."
    )

INSTRUCTIONS = "\n\n".join(
    " ".join(paragraph.split())
    for paragraph in [
        """
        SpekoAI MCP — the authoritative source for SpekoAI's SDKs,
        adapters, and platform.
        """,
        """
        Start here: call the `scaffold_project` prompt to bootstrap a
        new project, or read the `spekoai://docs/index` resource to see
        every bundled doc. Prefer the skill-sheet resources
        (`spekoai://docs/*-skills`) first — they are dense,
        LLM-oriented summaries of each package's API, gotchas, and
        minimal snippets. READMEs (`spekoai://docs/*-readme`) are longer
        prose. Use the `search_docs(query)` tool when you need to find
        something across all bundled docs, and `list_packages()` for
        structured metadata.
        """,
        """
        Need a recommended stack? Call `recommended_stack(optimize_for,
        language, region)` where `optimize_for` is one of `latency`
        (default — minimize TTFB), `accuracy` (maximize quality
        scores), or `cost` (minimize per-minute price). It returns
        the @spekoai package list plus the top-3 STT / LLM / TTS /
        S2S provider picks (with composite scores, p50 latency, and
        per-minute cost) ranked from the bundled v0 benchmark
        fixtures. Pair it with `scaffold_voice_app(optimize_for,
        region, ...)` to bake those choices into a Next.js scaffold
        the agent can execute verbatim. Vertical / use-case branching
        is intentionally omitted in v0 — benchmark data isn't yet
        vertical-tuned.
        """,
        """
        SpekoAI is a voice-AI gateway: one API that routes STT, LLM,
        and TTS calls to the best provider per (language, optimizeFor),
        with failover handled server-side. Public
        packages: `@spekoai/sdk` (TypeScript server SDK),
        `@spekoai/client` (browser WebRTC SDK), `spekoai` (Python
        server SDK), `@spekoai/adapter-livekit` (LiveKit Agents
        wrapper). `@spekoai/adapter-vapi` and `@spekoai/adapter-retell`
        are scaffolded placeholders — not production-ready.
        """,
    ]
)


class PackageInfo(BaseModel):
    """Structured package metadata returned by `list_packages`."""

    package_name: str = Field(description="Human name or npm/PyPI name.")
    npm_or_pypi: str | None = Field(
        description="Canonical install name, or null for internal packages."
    )
    status: str = Field(description="One of: stable, alpha, scaffold, internal, platform.")
    readme_uri: str | None = Field(
        description="MCP resource URI for the README, if one is shipped."
    )
    skills_uri: str | None = Field(
        description="MCP resource URI for the SKILLS sheet, if one is shipped."
    )


def _render_scaffold_instructions(manifest: scaffolds.ScaffoldManifest) -> str:
    """Render a scaffold manifest as an agent-actionable checklist.

    Returned alongside the structured manifest so a receiving agent
    (Claude Code, Cursor, etc.) has prose instructions to execute
    without re-interpreting the JSON. File bodies are emitted verbatim
    inside fenced blocks so the agent can copy-paste without escaping.
    """
    file_blocks: list[str] = []
    for f in manifest.files:
        fence = "```" + (f.language_hint or "")
        file_blocks.append(
            f"**`{f.path}`** (action: {f.action})\n{fence}\n{f.content}\n```"
        )
    files_section = "\n\n".join(file_blocks)
    installs_section = "\n".join(f"- `{cmd}`" for cmd in manifest.install_commands)
    env_rows: list[str] = []
    for env in manifest.env_vars:
        required_tag = "required" if env.required else "optional"
        example = f" (example: `{env.example}`)" if env.example else ""
        env_rows.append(f"- `{env.name}` — {required_tag}. {env.description}{example}")
    env_section = "\n".join(env_rows) if env_rows else "_(none)_"
    post_section = "\n".join(f"- {step}" for step in manifest.post_install_steps)
    docs_section = "\n".join(f"- `{uri}`" for uri in manifest.docs_resources)
    component_section = (
        "\n".join(f"- `{uri}`" for uri in manifest.component_resources)
        if manifest.component_resources
        else "_(none)_"
    )
    return (
        "# Speko voice-app scaffold\n\n"
        "Execute this scaffold verbatim — do not paraphrase file "
        "contents. Each step is idempotent; re-running in a fresh "
        "directory produces the same result.\n\n"
        "## Step 1 — Create these files\n\n"
        f"{files_section}\n\n"
        "## Step 2 — Run install commands (in order)\n\n"
        f"{installs_section}\n\n"
        "## Step 3 — Set environment variables\n\n"
        f"{env_section}\n\n"
        "## Step 4 — Post-install\n\n"
        f"{post_section}\n\n"
        "## Reference docs to read next\n\n"
        f"{docs_section}\n\n"
        "## Inlined component resources\n\n"
        f"{component_section}\n"
    )


def _build_package_infos(manifest: list[DocEntry]) -> list[PackageInfo]:
    """Collapse per-doc manifest entries into one row per package."""
    by_name: dict[str, dict[str, object]] = {}
    for entry in manifest:
        name = entry["package_name"]
        row = by_name.setdefault(
            name,
            {
                "package_name": name,
                "npm_or_pypi": entry["npm_or_pypi"],
                "status": entry["status"],
                "readme_uri": None,
                "skills_uri": None,
            },
        )
        uri = f"spekoai://docs/{entry['slug']}"
        if entry["kind"] == "readme":
            row["readme_uri"] = uri
        elif entry["kind"] == "skills":
            row["skills_uri"] = uri
    return [PackageInfo(**row) for row in by_name.values()]  # type: ignore[arg-type]


def create_server(auth: OAuthProxy | None = None) -> FastMCP:
    """Build a FastMCP server. `auth` is passed via the constructor (the
    supported wiring path); production deployments must supply one.
    """
    mcp: FastMCP = FastMCP(
        name="spekoai",
        instructions=INSTRUCTIONS,
        auth=auth,
    )

    register_resources(mcp)
    register_components(mcp)
    register_prompts(mcp)

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(_: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    @mcp.tool
    async def search_docs(
        _ctx: Context,
        query: Annotated[
            str,
            Field(
                description=(
                    "Free-text query. Matched case-insensitively against "
                    "titles and body of every bundled SpekoAI doc."
                ),
            ),
        ],
        limit: Annotated[
            int,
            Field(
                ge=1,
                le=20,
                description="Max hits to return. Defaults to 5.",
            ),
        ] = 5,
    ) -> list[search.DocHit]:
        """Search bundled SpekoAI docs. Returns slug, title, score, snippet.

        Use this when you need to find something across all SDKs/adapters
        without reading every resource. Each hit's `slug` can be opened
        directly as `spekoai://docs/{slug}`.
        """
        return search.search(query, limit=limit)

    @mcp.tool
    async def list_packages(_ctx: Context) -> list[PackageInfo]:
        """List every SpekoAI package known to this server, with URIs to
        the bundled README and SKILLS sheet where available.

        Prefer this over parsing markdown when you want structured data
        (e.g. "which packages are production-stable vs scaffold-only?").
        """
        return _build_package_infos(load_manifest())

    # `recommendations.UseCase` / `scaffolds.SpokenLanguage` are module-level
    # `Literal` aliases. FastMCP's tool-schema resolver (like the prompt
    # one) uses Pydantic TypeAdapter, which cannot dereference those
    # aliases under `from __future__ import annotations` — see the
    # explanatory comment on `scaffold_project` in `prompts.py`. Inline
    # the Literal values directly here to sidestep the forward-ref
    # resolution issue without disabling future-annotations.
    @mcp.tool
    async def recommended_stack(
        _ctx: Context,
        optimize_for: Annotated[
            Literal["latency", "accuracy", "cost"],
            Field(
                description=(
                    "Ranking preset for the STT / LLM / TTS / S2S "
                    "candidates. `latency` (default) minimizes time-"
                    "to-first-output, `accuracy` maximizes quality "
                    "scores, `cost` minimizes per-minute price. "
                    "Vertical / use-case branching is deliberately "
                    "not exposed yet — benchmark data isn't tuned "
                    "per vertical."
                ),
            ),
        ] = "latency",
        language: Annotated[
            str,
            Field(
                description=(
                    "BCP-47 language tag of the caller. v0 fixtures "
                    "cover English (`en`) only; non-English requests "
                    "echo the intent but return empty provider picks "
                    "plus a `notes` entry citing the gap."
                ),
            ),
        ] = "en",
        region: Annotated[
            str,
            Field(
                description=(
                    "Routing region. `global` selects batch ranking "
                    "for STT/TTS; `us-east4`, `europe-west3`, "
                    "`asia-southeast1` select streaming/realtime "
                    "ranking. S2S is realtime-only — `global` falls "
                    "back to `realtime.us-east4` and surfaces that "
                    "fallback in `notes`."
                ),
            ),
        ] = "global",
    ) -> recommendations.StackRecommendation:
        """Return the SpekoAI stack plus real provider picks.

        Use this before scaffolding to get (a) the @spekoai packages
        to install and (b) the top-3 STT / LLM / TTS / S2S provider
        picks for the caller's optimize-for axis. Provider rankings
        are sourced from the bundled v0 benchmark fixtures and include
        composite score, p50 latency, and per-minute cost where
        published.

        Example: a user asking "best stack with low latency" maps to
        `recommended_stack(optimize_for="latency", language="en",
        region="us-east4")`. A high-volume app prioritizing margin
        maps to `optimize_for="cost"`. A transcription-heavy product
        with stricter quality bars maps to `optimize_for="accuracy"`.

        Follow up with `scaffold_voice_app(optimize_for=<same>,
        region=<same>)` so the scaffold's route defaults match the
        picks the agent just surfaced.
        """
        return recommendations.recommend(
            optimize_for=optimize_for,
            language=language,
            region=region,
        )

    @mcp.tool
    async def scaffold_voice_app(
        _ctx: Context,
        languages: Annotated[
            list[Literal["en", "es"]] | None,
            Field(
                description=(
                    "Spoken languages the agent should support. Defaults "
                    "to ['en']. The first entry sets the session's "
                    "intent.language; adding 'es' also appends a "
                    "multilingual note to the system prompt."
                ),
            ),
        ] = None,
        system_prompt: Annotated[
            str | None,
            Field(
                description=(
                    "Override the default neutral voice-assistant "
                    "system prompt. Strongly recommended — set this "
                    "to your domain-specific persona. Leave null to "
                    "use the generic baseline."
                ),
            ),
        ] = None,
        optimize_for: Annotated[
            Literal["latency", "accuracy", "cost"],
            Field(
                description=(
                    "Ranking preset for the route's `intent.optimizeFor` "
                    "default. `latency` (default) leaves the route's "
                    "optimizeFor commented out so the Speko router picks "
                    "with its own defaults; `accuracy` or `cost` bakes "
                    "the preset into both the route handler and the "
                    "page config so the scaffold ships with explicit "
                    "intent."
                ),
            ),
        ] = "latency",
        region: Annotated[
            str,
            Field(
                description=(
                    "Routing region default for the scaffold's "
                    "`intent.region`. `global` (default) leaves region "
                    "off; any other value bakes the region into the "
                    "route handler so all sessions ship with that "
                    "intent. Header comment in the route file lists the "
                    "STT/LLM/TTS/S2S picks for the requested "
                    "(optimize_for, region) pair."
                ),
            ),
        ] = "global",
    ) -> ToolResult:
        """Build a Next.js App Router voice-app scaffold.

        Returns an actionable manifest: a text block with step-by-step
        instructions the agent should execute verbatim, plus structured
        content with the exact file bodies, install commands, and
        env vars. Create each file at its given `path` byte-for-byte —
        no paraphrasing.

        When `optimize_for` is non-`latency` or `region` is non-`global`,
        the route handler bakes those values in (they're commented out
        in the default scaffold) and the file's header comment lists
        the top-1 STT / LLM / TTS / S2S provider picks the runtime
        would route to so the user can audit before shipping.

        Domain-flavored prompts: pass your own `system_prompt`. The
        bundled default is a neutral voice-assistant baseline because
        v0 routing data isn't yet vertical-tuned.
        """
        manifest = scaffolds.build_voice_app_manifest(
            languages=languages,
            system_prompt=system_prompt,
            optimize_for=optimize_for,
            region=region,
        )
        return ToolResult(
            content=[TextContent(type="text", text=_render_scaffold_instructions(manifest))],
            structured_content=manifest.model_dump(),
        )

    @mcp.tool
    async def get_balance(_ctx: Context) -> OrganizationBalance:
        """Get the caller's current prepaid credit balance.

        Calls the SpekoAI `/v1/credits/balance` endpoint on the caller's
        behalf using the OAuth access token from the current MCP request.
        Use this to answer "how much credit do I have left?" without
        redirecting the user to the dashboard.
        """
        # The OAuth token FastMCP verified for this request is the same
        # token the Speko API accepts via Bearer auth — the SpekoAI server
        # runs its own JWT verification against the same issuer. Relay it
        # verbatim so the API resolves the exact caller's org.
        access_token = get_access_token()
        if access_token is None:
            raise ToolError(
                "get_balance requires an authenticated caller. Configure "
                "OAuth on the MCP server (SPEKOAI_OAUTH_* env vars) and "
                "connect via an OAuth-capable MCP client."
            )
        api_base = os.environ.get(
            "SPEKOAI_API_URL", "https://api.speko.ai"
        ).rstrip("/")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{api_base}/v1/credits/balance",
                headers={"Authorization": f"Bearer {access_token.token}"},
            )
        if resp.status_code != 200:
            raise ToolError(
                f"SpekoAI /v1/credits/balance returned "
                f"{resp.status_code}: {resp.text[:500]}"
            )
        return OrganizationBalance.model_validate(resp.json())

    return mcp
