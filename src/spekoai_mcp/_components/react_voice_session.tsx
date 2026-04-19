'use client';

import { useEffect, useMemo, useState } from 'react';
import { TokenSource } from 'livekit-client';
import {
  useAgent,
  useSession,
  useSessionContext,
  useSessionMessages,
} from '@livekit/components-react';
import { AgentAudioVisualizerBar } from '@/components/agents-ui/agent-audio-visualizer-bar';
import { AgentChatTranscript } from '@/components/agents-ui/agent-chat-transcript';
import { AgentControlBar } from '@/components/agents-ui/agent-control-bar';
import { AgentSessionProvider } from '@/components/agents-ui/agent-session-provider';
import { StartAudioButton } from '@/components/agents-ui/start-audio-button';
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
export type SessionVertical =
  | 'general'
  | 'healthcare'
  | 'insurance'
  | 'financial_services'
  | 'support_agent';
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

export function SpekoVoiceSession({
  sessionEndpoint = '/api/speko',
  defaults,
  className,
}: SpekoVoiceSessionProps) {
  const [config, setConfig] = useState<SessionConfig>(defaults);
  // Bumping `commitKey` is the signal to (a) rebuild the TokenSource
  // with the current form values baked into the POST body and (b) call
  // session.start(). Kept out of the form state so typing in the
  // textarea does not thrash the TokenSource.
  const [commitKey, setCommitKey] = useState(0);

  const tokenSource = useMemo(
    () =>
      TokenSource.endpoint(sessionEndpoint, {
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
      }),
    // Intentionally excluding `config` from deps — we only want the
    // body to reflect committed values, not every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sessionEndpoint, commitKey],
  );

  const session = useSession(tokenSource);

  return (
    <AgentSessionProvider session={session}>
      <SpekoStage
        config={config}
        onChange={setConfig}
        onCommit={() => setCommitKey((k) => k + 1)}
        committed={commitKey > 0}
        className={className}
      />
      <StartAudioButton label="Enable audio" />
    </AgentSessionProvider>
  );
}

interface StageProps {
  config: SessionConfig;
  onChange: (next: SessionConfig) => void;
  onCommit: () => void;
  committed: boolean;
  className?: string;
}

function SpekoStage({
  config,
  onChange,
  onCommit,
  committed,
  className,
}: StageProps) {
  const session = useSessionContext();
  const { isConnected, start, end } = session;
  const { audioTrack, state } = useAgent();
  const { messages } = useSessionMessages(session);

  // Connect once the user commits config — the TokenSource has been
  // rebuilt with the new body in the same render cycle.
  useEffect(() => {
    if (committed && !isConnected) {
      void start();
    }
  }, [committed, isConnected, start]);

  if (!isConnected) {
    return (
      <PreCallConfig
        config={config}
        onChange={onChange}
        onStart={onCommit}
        className={className}
      />
    );
  }

  return (
    <div className={'mx-auto grid w-full max-w-3xl gap-4 ' + (className ?? '')}>
      <Card className="border-[#FDE3CC] bg-[#FFFBF5]">
        <CardContent className="flex flex-col items-center gap-6 p-6">
          <AgentAudioVisualizerBar
            size="lg"
            color="#E8590C"
            state={state}
            audioTrack={audioTrack}
          />
          <div className="flex flex-wrap items-center justify-center gap-2 text-xs text-[#57534E]">
            <ConfigPill label="Language" value={config.language} />
            <ConfigPill label="Vertical" value={config.vertical} />
            <ConfigPill label="Optimize" value={config.optimizeFor} />
          </div>
          <AgentControlBar
            variant="default"
            isConnected={isConnected}
            onDisconnect={() => void end()}
            controls={{
              leave: true,
              microphone: true,
              screenShare: false,
              camera: false,
              chat: false,
            }}
          />
        </CardContent>
      </Card>
      <Card className="border-[#E7E5E4] bg-white">
        <CardContent className="p-4 sm:p-6">
          <AgentChatTranscript
            agentState={state}
            messages={messages}
            className="min-h-64"
          />
        </CardContent>
      </Card>
    </div>
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
  className?: string;
}

function PreCallConfig({
  config,
  onChange,
  onStart,
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
                <SelectItem value="insurance">Insurance</SelectItem>
                <SelectItem value="financial_services">
                  Financial services
                </SelectItem>
                <SelectItem value="support_agent">Support agent</SelectItem>
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
          className="mt-2 w-full rounded-full bg-[#E8590C] font-mono text-xs font-bold uppercase tracking-wider text-white shadow-sm hover:bg-[#C2410C]"
        >
          Start conversation
        </Button>
      </CardContent>
    </Card>
  );
}
