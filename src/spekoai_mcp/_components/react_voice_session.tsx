'use client';

import { useEffect, useRef, useState } from 'react';
import type {
  ConversationMessage,
  ConversationMode,
  ConversationStatus,
  VoiceConversation,
} from '@spekoai/client';

export interface SpekoVoiceSessionProps {
  sessionEndpoint: string;
  sessionBody?: Record<string, unknown>;
  onError?: (err: Error) => void;
  onTranscript?: (msg: ConversationMessage) => void;
  className?: string;
}

export function SpekoVoiceSession({
  sessionEndpoint,
  sessionBody,
  onError,
  onTranscript,
  className,
}: SpekoVoiceSessionProps) {
  const [status, setStatus] = useState<ConversationStatus>('disconnected');
  const [mode, setMode] = useState<ConversationMode | null>(null);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [isMuted, setIsMuted] = useState(false);
  const conversationRef = useRef<VoiceConversation | null>(null);

  async function start() {
    try {
      const res = await fetch(sessionEndpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sessionBody ?? {}),
      });
      if (!res.ok) {
        throw new Error(`session ${res.status}: ${await res.text()}`);
      }
      const { conversationToken, livekitUrl } = await res.json();

      const { VoiceConversation } = await import('@spekoai/client');
      const conversation = await VoiceConversation.create({
        conversationToken,
        livekitUrl,
        onConnect: () => {},
        onDisconnect: () => setStatus('disconnected'),
        onMessage: (msg) => {
          setMessages((prev) => {
            const last = prev[prev.length - 1];
            if (last && !last.isFinal && last.source === msg.source) {
              return [...prev.slice(0, -1), msg];
            }
            return [...prev, msg];
          });
          onTranscript?.(msg);
        },
        onStatusChange: setStatus,
        onModeChange: setMode,
        onError: (err) => onError?.(err),
      });
      conversationRef.current = conversation;
    } catch (err) {
      onError?.(err instanceof Error ? err : new Error(String(err)));
    }
  }

  async function stop() {
    const conversation = conversationRef.current;
    conversationRef.current = null;
    await conversation?.endSession();
  }

  async function toggleMute() {
    const next = !isMuted;
    await conversationRef.current?.setMicMuted(next);
    setIsMuted(next);
  }

  useEffect(() => {
    return () => {
      void conversationRef.current?.endSession();
      conversationRef.current = null;
    };
  }, []);

  const idle = status === 'disconnected';

  return (
    <div className={className}>
      <div>
        Status: {status}
        {mode ? ` · ${mode}` : ''}
      </div>
      <div>
        {idle ? (
          <button type="button" onClick={start}>
            Start
          </button>
        ) : (
          <>
            <button type="button" onClick={stop}>
              End
            </button>
            <button type="button" onClick={toggleMute}>
              {isMuted ? 'Unmute' : 'Mute'}
            </button>
          </>
        )}
      </div>
      <ol>
        {messages.map((m, i) => (
          <li key={i}>
            <strong>{m.source}:</strong> {m.text}
          </li>
        ))}
      </ol>
    </div>
  );
}
