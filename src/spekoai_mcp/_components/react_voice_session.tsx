'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { VoiceConversation } from '@spekoai/client';
import type {
  ConversationMessage,
  ConversationMode,
  ConversationStatus,
} from '@spekoai/client';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';

export type SessionLanguage = 'en-US' | 'es-US';
export type SessionVertical = 'general' | 'healthcare' | 'finance' | 'legal';
export type SessionOptimizeFor = 'latency' | 'quality';

export interface SessionConfig {
  language: SessionLanguage;
  vertical: SessionVertical;
  optimizeFor: SessionOptimizeFor;
  systemPrompt: string;
}

export interface SpekoVoiceSessionProps {
  sessionEndpoint?: string;
  defaults: SessionConfig;
  className?: string;
}

interface TranscriptEntry {
  id: string;
  source: ConversationMessage['source'];
  text: string;
  isFinal: boolean;
}

export function SpekoVoiceSession({
  sessionEndpoint = '/api/speko',
  defaults,
  className,
}: SpekoVoiceSessionProps) {
  const [config, setConfig] = useState<SessionConfig>(defaults);
  const [conversation, setConversation] = useState<VoiceConversation | null>(
    null,
  );
  const [status, setStatus] = useState<ConversationStatus>('disconnected');
  const [mode, setMode] = useState<ConversationMode>('listening');
  const [messages, setMessages] = useState<TranscriptEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [isStarting, setIsStarting] = useState(false);
  const [micMuted, setMicMuted] = useState(false);

  const conversationRef = useRef<VoiceConversation | null>(null);
  useEffect(() => {
    conversationRef.current = conversation;
  }, [conversation]);

  useEffect(() => {
    return () => {
      void conversationRef.current?.endSession();
    };
  }, []);

  const start = useCallback(async () => {
    setError(null);
    setIsStarting(true);
    try {
      const res = await fetch(sessionEndpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          intent: {
            language: config.language,
            vertical: config.vertical,
            optimizeFor: config.optimizeFor,
          },
          systemPrompt: config.systemPrompt,
        }),
      });
      if (!res.ok) {
        throw new Error(`${sessionEndpoint} ${res.status}: ${await res.text()}`);
      }
      const { server_url, participant_token } = (await res.json()) as {
        server_url: string;
        participant_token: string;
      };
      setMessages([]);
      const conv = await VoiceConversation.create({
        conversationToken: participant_token,
        livekitUrl: server_url,
        onStatusChange: (s) => setStatus(s),
        onModeChange: (m) => setMode(m),
        onMessage: (msg) => {
          setMessages((prev) => mergeMessage(prev, msg));
        },
        onError: (err) => {
          setError(err instanceof Error ? err.message : String(err));
        },
      });
      setConversation(conv);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsStarting(false);
    }
  }, [config, sessionEndpoint]);

  const end = useCallback(async () => {
    await conversationRef.current?.endSession();
    setConversation(null);
    setStatus('disconnected');
    setMode('listening');
    setMicMuted(false);
  }, []);

  const toggleMic = useCallback(async () => {
    const conv = conversationRef.current;
    if (!conv) return;
    const next = !micMuted;
    await conv.setMicMuted(next);
    setMicMuted(next);
  }, [micMuted]);

  const isConnected = status === 'connected';

  if (!isConnected) {
    return (
      <PreCallConfig
        config={config}
        onChange={setConfig}
        onStart={start}
        isStarting={isStarting || status === 'connecting'}
        error={error}
        className={className}
      />
    );
  }

  return (
    <div className={'mx-auto grid w-full max-w-3xl gap-4 ' + (className ?? '')}>
      <Card className="border-[#FDE3CC] bg-[#FFFBF5]">
        <CardContent className="flex flex-col items-center gap-6 p-6">
          <ModeIndicator mode={mode} />
          <div className="flex flex-wrap items-center justify-center gap-2 text-xs text-[#57534E]">
            <ConfigPill label="Language" value={config.language} />
            <ConfigPill label="Vertical" value={config.vertical} />
            <ConfigPill label="Optimize" value={config.optimizeFor} />
          </div>
          <div className="flex gap-2">
            <Button
              variant="outline"
              onClick={() => void toggleMic()}
              className="rounded-full font-mono text-xs uppercase tracking-wider"
            >
              {micMuted ? 'Unmute mic' : 'Mute mic'}
            </Button>
            <Button
              onClick={() => void end()}
              className="rounded-full bg-[#E8590C] font-mono text-xs font-bold uppercase tracking-wider text-white hover:bg-[#C2410C]"
            >
              End call
            </Button>
          </div>
        </CardContent>
      </Card>
      <Card className="border-[#E7E5E4] bg-white">
        <CardContent className="p-4 sm:p-6">
          <Transcript messages={messages} mode={mode} />
        </CardContent>
      </Card>
      {error ? (
        <p className="mx-auto max-w-3xl text-sm text-[#B91C1C]">{error}</p>
      ) : null}
    </div>
  );
}

function mergeMessage(
  prev: TranscriptEntry[],
  msg: ConversationMessage,
): TranscriptEntry[] {
  const last = prev[prev.length - 1];
  // Interim updates from the same speaker overwrite the last entry until
  // a final lands; a final message promotes the interim to stable and
  // starts a fresh slot for whatever comes next.
  if (last && !last.isFinal && last.source === msg.source) {
    const next = prev.slice(0, -1);
    next.push({ ...last, text: msg.text, isFinal: msg.isFinal });
    return next;
  }
  return [
    ...prev,
    {
      id: `${msg.source}-${prev.length}-${Date.now()}`,
      source: msg.source,
      text: msg.text,
      isFinal: msg.isFinal,
    },
  ];
}

function ModeIndicator({ mode }: { mode: ConversationMode }) {
  const label = mode === 'speaking' ? 'Agent speaking' : 'Listening';
  const tone =
    mode === 'speaking'
      ? 'bg-[#E8590C] shadow-[0_0_24px_rgba(232,89,12,0.45)]'
      : 'bg-[#FDE3CC]';
  return (
    <div className="flex flex-col items-center gap-3">
      <span
        aria-hidden
        className={
          'h-16 w-16 rounded-full transition-all duration-200 ' +
          (mode === 'speaking' ? 'scale-110 ' : 'scale-100 ') +
          tone
        }
      />
      <span className="font-mono text-[10px] uppercase tracking-wider text-[#A8A29E]">
        {label}
      </span>
    </div>
  );
}

function Transcript({
  messages,
  mode,
}: {
  messages: TranscriptEntry[];
  mode: ConversationMode;
}) {
  if (messages.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-[#A8A29E]">
        {mode === 'speaking'
          ? 'Agent is speaking…'
          : 'Say something — transcript will appear here.'}
      </p>
    );
  }
  return (
    <ol className="flex max-h-80 flex-col gap-3 overflow-y-auto">
      {messages.map((m) => (
        <li key={m.id} className="flex flex-col gap-1">
          <span className="font-mono text-[10px] uppercase tracking-wider text-[#A8A29E]">
            {m.source === 'user' ? 'You' : 'Agent'}
          </span>
          <span
            className={
              'text-sm leading-relaxed ' +
              (m.isFinal ? 'text-[#1C1917]' : 'italic text-[#57534E]')
            }
          >
            {m.text}
          </span>
        </li>
      ))}
    </ol>
  );
}

function ConfigPill({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-[#FDE3CC] bg-white/70 px-2.5 py-1 font-mono">
      <span className="uppercase tracking-wider text-[10px] text-[#A8A29E]">
        {label}
      </span>
      <span className="text-[#1C1917]">{value}</span>
    </span>
  );
}

interface PreCallConfigProps {
  config: SessionConfig;
  onChange: (next: SessionConfig) => void;
  onStart: () => void;
  isStarting: boolean;
  error: string | null;
  className?: string;
}

function PreCallConfig({
  config,
  onChange,
  onStart,
  isStarting,
  error,
  className,
}: PreCallConfigProps) {
  const update = <K extends keyof SessionConfig>(
    key: K,
    value: SessionConfig[K],
  ) => onChange({ ...config, [key]: value });

  return (
    <Card
      className={
        'mx-auto w-full max-w-xl border-[#FDE3CC] bg-[#FFFBF5] shadow-sm ' +
        (className ?? '')
      }
    >
      <CardHeader className="pb-2">
        <CardTitle className="text-[#1C1917]">Configure your session</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-5 pb-8 pt-4">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div className="flex flex-col gap-1.5">
            <Label
              htmlFor="speko-language"
              className="text-xs uppercase tracking-wider text-[#57534E]"
            >
              Language
            </Label>
            <Select
              value={config.language}
              onValueChange={(v) => update('language', v as SessionLanguage)}
            >
              <SelectTrigger
                id="speko-language"
                className="border-[#E7E5E4] bg-white"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="en-US">English (en-US)</SelectItem>
                <SelectItem value="es-US">Spanish (es-US)</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label
              htmlFor="speko-vertical"
              className="text-xs uppercase tracking-wider text-[#57534E]"
            >
              Vertical
            </Label>
            <Select
              value={config.vertical}
              onValueChange={(v) => update('vertical', v as SessionVertical)}
            >
              <SelectTrigger
                id="speko-vertical"
                className="border-[#E7E5E4] bg-white"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="general">General</SelectItem>
                <SelectItem value="healthcare">Healthcare</SelectItem>
                <SelectItem value="finance">Finance</SelectItem>
                <SelectItem value="legal">Legal</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label
              htmlFor="speko-optimize"
              className="text-xs uppercase tracking-wider text-[#57534E]"
            >
              Optimize for
            </Label>
            <Select
              value={config.optimizeFor}
              onValueChange={(v) =>
                update('optimizeFor', v as SessionOptimizeFor)
              }
            >
              <SelectTrigger
                id="speko-optimize"
                className="border-[#E7E5E4] bg-white"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="latency">Latency</SelectItem>
                <SelectItem value="quality">Quality</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label
            htmlFor="speko-prompt"
            className="text-xs uppercase tracking-wider text-[#57534E]"
          >
            System prompt
          </Label>
          <Textarea
            id="speko-prompt"
            value={config.systemPrompt}
            onChange={(e) => update('systemPrompt', e.target.value)}
            rows={6}
            className="resize-y border-[#E7E5E4] bg-white font-mono text-sm leading-relaxed"
          />
        </div>
        <Button
          size="lg"
          onClick={onStart}
          disabled={isStarting}
          className="mt-2 w-full rounded-full bg-[#E8590C] font-mono text-xs font-bold uppercase tracking-wider text-white shadow-sm hover:bg-[#C2410C] disabled:opacity-60"
        >
          {isStarting ? 'Connecting…' : 'Start conversation'}
        </Button>
        {error ? (
          <p className="text-xs text-[#B91C1C]">{error}</p>
        ) : null}
      </CardContent>
    </Card>
  );
}
