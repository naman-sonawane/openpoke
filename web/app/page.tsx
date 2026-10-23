'use client';

import clsx from 'clsx';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useSettings } from '@/components/SettingsModal';
import { ChatInput } from '@/components/chat/ChatInput';
import { ChatMessages } from '@/components/chat/ChatMessages';
import { ErrorBanner } from '@/components/chat/ErrorBanner';
import { useAutoScroll } from '@/components/chat/useAutoScroll';
import type { ChatBubble } from '@/components/chat/types';
import { StaffPanel, type IntakeState } from '@/components/intake/StaffPanel';
import { useVoiceCall, type TurnResult, type VoiceStatus } from '@/components/voice/useVoiceCall';

const POLL_INTERVAL_MS = 1500;

const STATUS_LABEL: Record<VoiceStatus, string> = {
  off: 'Voice off',
  listening: 'Listening…',
  thinking: 'Thinking…',
  speaking: 'Speaking…',
};

const formatEscapeCharacters = (text: string): string => {
  return text
    .replace(/\\n/g, '\n')
    .replace(/\\t/g, '\t')
    .replace(/\\r/g, '\r')
    .replace(/\\\\/g, '\\');
};

const isRenderableMessage = (entry: any) =>
  typeof entry?.role === 'string' &&
  typeof entry?.content === 'string' &&
  entry.content.trim().length > 0;

const toBubbles = (payload: any): ChatBubble[] => {
  if (!Array.isArray(payload?.messages)) return [];

  return payload.messages
    .filter(isRenderableMessage)
    .map((message: any, index: number) => ({
      id: `history-${index}`,
      role: message.role,
      text: formatEscapeCharacters(message.content),
    }));
};

export default function Page() {
  const { settings, setSettings } = useSettings();
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<ChatBubble[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [isWaitingForResponse, setIsWaitingForResponse] = useState(false);
  const [intakeState, setIntakeState] = useState<IntakeState | null>(null);
  const [lastLatencyMs, setLastLatencyMs] = useState<number | null>(null);
  // Each browser tab is its own call; the server keeps calls apart by id.
  const [callId, setCallId] = useState<string | null>(null);
  const callIdRef = useRef<string | null>(null);
  callIdRef.current = callId;
  const { scrollContainerRef, handleScroll } = useAutoScroll({
    items: messages,
    isWaiting: isWaitingForResponse,
  });

  const loadHistory = useCallback(async () => {
    const id = callIdRef.current;
    if (!id) return;
    try {
      const res = await fetch(`/api/voice/transcript?call_id=${id}`, { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      if (callIdRef.current === id) setMessages(toBubbles(data));
    } catch (err: any) {
      if (err?.name === 'AbortError') return;
      console.error('Failed to load transcript', err);
    }
  }, []);

  const loadIntakeState = useCallback(async () => {
    const id = callIdRef.current;
    if (!id) return;
    try {
      const res = await fetch(`/api/intake/state?call_id=${id}`, { cache: 'no-store' });
      if (res.ok && callIdRef.current === id) setIntakeState(await res.json());
    } catch (err) {
      console.debug('Failed to load intake state', err);
    }
  }, []);

  const refresh = useCallback(async () => {
    await Promise.all([loadHistory(), loadIntakeState()]);
  }, [loadHistory, loadIntakeState]);

  useEffect(() => {
    void refresh();
    const intervalId = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => window.clearInterval(intervalId);
  }, [refresh]);

  // Detect and store browser timezone on first load
  useEffect(() => {
    if (settings.timezone) return;
    const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    fetch('/api/timezone', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ timezone: browserTimezone }),
    })
      .then((response) => {
        if (response.ok) setSettings({ ...settings, timezone: browserTimezone });
      })
      .catch((err) => console.debug('Timezone detection failed:', err));
  }, [settings, setSettings]);

  // One caller turn, typed or spoken. Waits for the reply instead of polling.
  const sendTurn = useCallback(
    async (text: string): Promise<TurnResult | null> => {
      const trimmed = text.trim();
      if (!trimmed) return null;
      if (!callIdRef.current) {
        setError('Press New call first.');
        return null;
      }

      setError(null);
      setIsWaitingForResponse(true);
      setMessages((prev) => [...prev, { id: `user-${Date.now()}`, role: 'user', text: trimmed }]);

      try {
        const res = await fetch('/api/voice/turn', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: trimmed, call_id: callIdRef.current }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data?.error || `Request failed (${res.status})`);
        setLastLatencyMs(data.latency_ms ?? null);
        return { reply: data.reply || null, endCall: Boolean(data.call_ended) };
      } catch (err: any) {
        console.error('Failed to send turn', err);
        setError(err?.message || 'Failed to send message');
        return null;
      } finally {
        setIsWaitingForResponse(false);
        void refresh();
      }
    },
    [refresh],
  );

  // On laptop or phone speakers the mic would pick up the agent's own voice,
  // so it pauses while the agent talks. Interrupting is explicit instead:
  // Space, or a tap on the status bar.
  const voice = useVoiceCall({ onTurn: sendTurn, bargeIn: false });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const typing = (event.target as HTMLElement)?.tagName === 'INPUT';
      if (event.code === 'Space' && !typing && voice.status === 'speaking') {
        event.preventDefault();
        voice.interrupt();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [voice]);

  const startNewCall = useCallback(async () => {
    window.speechSynthesis?.cancel();
    setError(null);
    setLastLatencyMs(null);
    try {
      // A new call gets a new id: nothing to clear, and other calls (other
      // tabs, other devices) are untouched.
      const res = await fetch('/api/voice/start', { method: 'POST' });
      const data = await res.json();
      if (!res.ok || !data?.call_id) throw new Error(data?.error || 'Could not start a call');
      callIdRef.current = data.call_id;
      setCallId(data.call_id);
      setMessages([]);
      setIntakeState(null);
      await refresh();
      if (voice.active && data?.reply) void voice.speak(data.reply);
    } catch (err: any) {
      setError(err?.message || 'Failed to start a new call');
    }
  }, [refresh, voice]);

  const toggleDemo = useCallback(
    async (key: 'fail_next_tool' | 'slot_conflict', value: boolean) => {
      await fetch('/api/intake/demo', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [key]: value, call_id: callIdRef.current }),
      });
      void loadIntakeState();
    },
    [loadIntakeState],
  );

  const handleSubmit = useCallback(async () => {
    const value = input;
    if (!value.trim()) return;
    setInput('');
    const result = await sendTurn(value);
    if (result?.reply && voice.active) {
      await voice.speak(result.reply);
      if (result.endCall) voice.stop();
    }
  }, [input, sendTurn, voice]);

  return (
    <main className="chat-bg min-h-screen p-4 sm:p-6">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-4">
        <header className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h1 className="text-lg font-semibold">Maple Street Family Medicine</h1>
            <p className="text-sm text-gray-500">Voice intake and scheduling</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {voice.supported ? (
              <button
                className={clsx(
                  'rounded-md border px-3 py-2 text-sm',
                  voice.active ? 'border-red-200 bg-red-50 text-red-700' : 'border-gray-200 hover:bg-gray-50',
                )}
                onClick={voice.active ? voice.stop : voice.start}
              >
                {voice.active ? 'Stop voice' : 'Start voice'}
              </button>
            ) : (
              <span className="text-xs text-gray-500">Voice needs Chrome or Edge</span>
            )}
            <button className="btn" onClick={() => void startNewCall()}>
              New call
            </button>
          </div>
        </header>

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
          <div className="card flex flex-col overflow-hidden">
            <div
              className={clsx(
                'flex items-center gap-2 border-b border-gray-100 px-4 py-2 text-sm text-gray-600',
                voice.status === 'speaking' && 'cursor-pointer hover:bg-gray-50',
              )}
              onClick={voice.status === 'speaking' ? voice.interrupt : undefined}
            >
              <span
                className={clsx(
                  'h-2.5 w-2.5 rounded-full',
                  voice.status === 'listening' && 'animate-pulse bg-green-500',
                  voice.status === 'thinking' && 'bg-amber-400',
                  voice.status === 'speaking' && 'bg-blue-500',
                  voice.status === 'off' && 'bg-gray-300',
                )}
              />
              {STATUS_LABEL[voice.status]}
              {voice.status === 'speaking' && (
                <span className="text-xs text-gray-400">Space or tap to interrupt</span>
              )}
              {voice.interim &&<span className="truncate italic text-gray-400">“{voice.interim}”</span>}
            </div>

            <ChatMessages
              messages={messages}
              isWaitingForResponse={isWaitingForResponse}
              scrollContainerRef={scrollContainerRef}
              onScroll={handleScroll}
            />

            <div className="border-t border-gray-200 p-3">
              {error && <ErrorBanner message={error} onDismiss={() => setError(null)} />}
              <ChatInput
                value={input}
                canSubmit={input.trim().length > 0 && !isWaitingForResponse}
                placeholder={voice.active ? 'Speak, or type as the caller…' : 'Type as the caller…'}
                onChange={setInput}
                onSubmit={handleSubmit}
              />
            </div>
          </div>

          <StaffPanel state={intakeState} lastLatencyMs={lastLatencyMs} onToggleDemo={toggleDemo} />
        </div>
      </div>
    </main>
  );
}
