"""Ready-to-paste Speko integration snippets for the builder profile.

Every snippet implements the same canonical two-part flow, sourced from
the real SDK docs bundled in ``_docs/`` (``packages/client/README.md``,
``packages/sdk*/README.md``) and the scaffold template in
``scaffolds.py``:

1. Server side: mint a voice session with ``POST /v1/sessions`` using the
   secret ``SPEKO_API_KEY``. The server SDKs (@spekoai/sdk, spekoai) do
   not expose a sessions helper yet, so the canonical integration is a
   raw HTTP call — exactly what the Next.js scaffold ships.
2. Browser side: connect with ``@spekoai/client``'s
   ``VoiceConversation.create({ transportToken, transportUrl, ... })``
   using the short-lived credentials returned by step 1.

Keep these in sync with ``apps/server/src/routes/sessions.ts`` (response
fields ``transportToken`` / ``transportUrl``, HTTP 201) and the
``@spekoai/client`` README callback surface.
"""

from __future__ import annotations

from typing import Literal

SnippetFramework = Literal["nextjs", "react", "node", "python", "curl"]

SNIPPET_FRAMEWORKS: tuple[SnippetFramework, ...] = (
    "nextjs",
    "react",
    "node",
    "python",
    "curl",
)

_SHARED_NOTES: list[str] = [
    "Never ship SPEKO_API_KEY to the browser. Mint sessions server-side; "
    "the browser only ever sees the short-lived transportToken/transportUrl "
    "pair from the /v1/sessions response.",
    "MCP tools only inform the agent during code generation - the generated "
    "app cannot call MCP tools at runtime. Runtime integration is exactly "
    "this code: SPEKO_API_KEY in the server environment plus "
    "@spekoai/client in the browser.",
    "To reuse an agent created with agents.create, replace the intent/"
    "systemPrompt fields in the /v1/sessions body with {\"agentId\": "
    "\"<agent id>\"}.",
    "Get an API key at https://platform.speko.dev/api-keys. The API base "
    "URL is https://api.speko.dev.",
    "Full /v1/sessions body schema and @spekoai/client callback surface: "
    "read the spekoai://docs/llms-full and spekoai://docs/client-readme "
    "resources, or call docs.search.",
]

_NEXTJS_TITLE = "Next.js (App Router): server session mint + browser voice call"
_NEXTJS_CODE = """\
// --- app/api/speko/session/route.ts ---------------------------------------
// Server-side: mints Speko session credentials for the browser.
// npm install @spekoai/client   (used by the page below)
// Env: SPEKO_API_KEY=sk_...     (server-side only, never NEXT_PUBLIC_*)

export const runtime = 'nodejs';

export async function POST(): Promise<Response> {
  const apiKey = process.env.SPEKO_API_KEY;
  if (!apiKey) {
    return new Response('SPEKO_API_KEY not set', { status: 500 });
  }

  const res = await fetch('https://api.speko.dev/v1/sessions', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      // Or pin a saved agent instead: { agentId: 'your-agent-id' }
      intent: { language: 'en' },
      systemPrompt: 'You are a concise, helpful voice assistant.',
    }),
  });

  if (!res.ok) {
    return new Response(await res.text(), { status: res.status });
  }

  // 201 response also includes sessionId, roomName, identity, expiresAt.
  const { transportToken, transportUrl } = (await res.json()) as {
    transportToken: string;
    transportUrl: string;
  };
  return Response.json({ transportToken, transportUrl });
}

// --- app/voice/page.tsx ----------------------------------------------------
// Browser side: starts/stops a live voice call via @spekoai/client.

'use client';

import { useRef, useState } from 'react';
import { VoiceConversation, type ConversationMessage } from '@spekoai/client';

export default function VoicePage() {
  const conversationRef = useRef<VoiceConversation | null>(null);
  const [status, setStatus] = useState<string>('idle');
  const [transcript, setTranscript] = useState<readonly ConversationMessage[]>([]);

  async function startCall() {
    const res = await fetch('/api/speko/session', { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    const { transportToken, transportUrl } = await res.json();

    conversationRef.current = await VoiceConversation.create({
      transportToken,
      transportUrl,
      onStatusChange: (next) => setStatus(next),
      // onTranscript emits the FULL reconciled transcript (deduped,
      // ordered) on every update - use it for rendering, NOT onMessage
      // (transcription segments re-deliver cumulatively and would
      // duplicate if appended).
      onTranscript: (messages) => setTranscript(messages),
      onError: (err) => console.error('speko error', err),
    });
  }

  async function endCall() {
    await conversationRef.current?.endSession();
    conversationRef.current = null;
    setStatus('idle');
  }

  return (
    <main>
      <p>Status: {status}</p>
      <button onClick={startCall}>Start call</button>
      <button onClick={endCall}>End call</button>
      <ul>
        {transcript.map((message, i) => (
          <li key={i}>
            {message.source}: {message.text}
          </li>
        ))}
      </ul>
    </main>
  );
}
"""

_REACT_TITLE = "React (Vite/SPA): browser voice call against your session endpoint"
_REACT_CODE = """\
// src/VoiceCall.tsx
// npm install @spekoai/client
//
// Requires a backend endpoint that mints Speko sessions with your secret
// SPEKO_API_KEY and returns { transportToken, transportUrl } - see the
// 'node' (Express) or 'python' (FastAPI) snippet from code_snippets.get.
// Never call api.speko.dev with an API key from the browser.

import { useRef, useState } from 'react';
import { VoiceConversation, type ConversationMessage } from '@spekoai/client';

export function VoiceCall() {
  const conversationRef = useRef<VoiceConversation | null>(null);
  const [status, setStatus] = useState<string>('idle');
  const [mode, setMode] = useState<string>('listening');
  const [transcript, setTranscript] = useState<readonly ConversationMessage[]>([]);

  async function startCall() {
    // Your backend: POST /api/session -> { transportToken, transportUrl }
    const res = await fetch('/api/session', { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    const { transportToken, transportUrl } = await res.json();

    conversationRef.current = await VoiceConversation.create({
      transportToken,
      transportUrl,
      onConnect: ({ conversationId }) => console.log('connected', conversationId),
      onDisconnect: ({ reason }) => setStatus(`disconnected: ${reason}`),
      onStatusChange: (next) => setStatus(next),
      onModeChange: (next) => setMode(next), // 'listening' | 'speaking'
      // onTranscript emits the FULL reconciled transcript (deduped,
      // ordered) on every update - use it for rendering, NOT onMessage
      // (transcription segments re-deliver cumulatively and would
      // duplicate if appended).
      onTranscript: (messages) => setTranscript(messages),
      onError: (err) => console.error('speko error', err),
    });
  }

  async function endCall() {
    await conversationRef.current?.endSession();
    conversationRef.current = null;
    setStatus('idle');
  }

  return (
    <div>
      <p>
        Status: {status} - Agent is {mode}
      </p>
      <button onClick={startCall}>Start call</button>
      <button onClick={endCall}>End call</button>
      <ul>
        {transcript.map((message, i) => (
          <li key={i}>
            {message.source}: {message.text}
          </li>
        ))}
      </ul>
    </div>
  );
}
"""

_NODE_TITLE = "Node (Express): server-side session mint endpoint"
_NODE_CODE = """\
// server.mjs - Node 18+ (global fetch). npm install express
// Env: SPEKO_API_KEY=sk_...
//
// Pair with the browser side from the 'react' snippet: the browser calls
// POST /api/session and connects with @spekoai/client using the returned
// { transportToken, transportUrl }.

import express from 'express';

const app = express();

app.post('/api/session', async (_req, res) => {
  const apiKey = process.env.SPEKO_API_KEY;
  if (!apiKey) {
    return res.status(500).send('SPEKO_API_KEY not set');
  }

  const upstream = await fetch('https://api.speko.dev/v1/sessions', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      // Or pin a saved agent instead: { agentId: 'your-agent-id' }
      intent: { language: 'en' },
      systemPrompt: 'You are a concise, helpful voice assistant.',
    }),
  });

  if (!upstream.ok) {
    return res.status(upstream.status).send(await upstream.text());
  }

  // 201 response also includes sessionId, roomName, identity, expiresAt.
  const { transportToken, transportUrl } = await upstream.json();
  res.json({ transportToken, transportUrl });
});

app.listen(3001, () => {
  console.log('Session endpoint on http://localhost:3001/api/session');
});
"""

_PYTHON_TITLE = "Python (FastAPI): server-side session mint endpoint"
_PYTHON_CODE = '''\
# main.py - pip install fastapi uvicorn httpx
# Env: SPEKO_API_KEY=sk_...
# Run: uvicorn main:app --port 3001
#
# Pair with the browser side from the 'react' snippet: the browser calls
# POST /api/session and connects with @spekoai/client using the returned
# {transportToken, transportUrl}.

import os

import httpx
from fastapi import FastAPI, HTTPException

app = FastAPI()

SPEKO_API_URL = "https://api.speko.dev"


@app.post("/api/session")
async def create_session() -> dict[str, str]:
    api_key = os.environ.get("SPEKO_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="SPEKO_API_KEY not set")

    async with httpx.AsyncClient(timeout=30.0) as client:
        upstream = await client.post(
            f"{SPEKO_API_URL}/v1/sessions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                # Or pin a saved agent instead: {"agentId": "your-agent-id"}
                "intent": {"language": "en"},
                "systemPrompt": "You are a concise, helpful voice assistant.",
            },
        )

    if upstream.status_code >= 400:
        raise HTTPException(status_code=upstream.status_code, detail=upstream.text)

    # 201 response also includes sessionId, roomName, identity, expiresAt.
    data = upstream.json()
    return {
        "transportToken": data["transportToken"],
        "transportUrl": data["transportUrl"],
    }
'''

_CURL_TITLE = "curl: mint a voice session from the command line"
_CURL_CODE = """\
# Mint a Speko voice session (server-side; requires SPEKO_API_KEY).
# The 201 response returns short-lived browser credentials:
#   { "sessionId": "...", "transportToken": "...", "transportUrl": "...",
#     "roomName": "...", "identity": "...", "expiresAt": "..." }
# Hand transportToken + transportUrl to @spekoai/client's
# VoiceConversation.create(...) in the browser.

curl -sS -X POST https://api.speko.dev/v1/sessions \\
  -H "Authorization: Bearer $SPEKO_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "intent": { "language": "en" },
    "systemPrompt": "You are a concise, helpful voice assistant."
  }'

# Reuse a saved agent instead of inline config:
curl -sS -X POST https://api.speko.dev/v1/sessions \\
  -H "Authorization: Bearer $SPEKO_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{ "agentId": "your-agent-id" }'
"""

_SNIPPETS: dict[SnippetFramework, dict[str, str]] = {
    "nextjs": {"title": _NEXTJS_TITLE, "language": "tsx", "code": _NEXTJS_CODE},
    "react": {"title": _REACT_TITLE, "language": "tsx", "code": _REACT_CODE},
    "node": {"title": _NODE_TITLE, "language": "js", "code": _NODE_CODE},
    "python": {"title": _PYTHON_TITLE, "language": "python", "code": _PYTHON_CODE},
    "curl": {"title": _CURL_TITLE, "language": "bash", "code": _CURL_CODE},
}

_DOCS_RESOURCES: list[str] = [
    "spekoai://docs/client-readme",
    "spekoai://docs/llms-full",
    "spekoai://docs/sdk-readme",
]


def get_snippet(framework: SnippetFramework) -> dict[str, object]:
    """Return the integration snippet payload for one framework."""
    entry = _SNIPPETS[framework]
    return {
        "framework": framework,
        "title": entry["title"],
        "language": entry["language"],
        "code": entry["code"],
        "notes": list(_SHARED_NOTES),
        "docs_resources": list(_DOCS_RESOURCES),
    }
