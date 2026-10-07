'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { RoomEvent, TokenSource } from 'livekit-client';
import { useAgent, useSession, useSessionMessages, useStartAudio } from '@livekit/components-react';
import { AgentSessionProvider } from '@/components/agents-ui/agent-session-provider';
import { AgentAudioVisualizerWave } from '@/components/agents-ui/agent-audio-visualizer-wave';
import { Allocation, Message, SavedSession, control, errorMessage, sleep } from '@/lib/control';

type Props = {
  allocation: Allocation;
  previous: Message[];
  onMessages: (messages: Message[]) => void;
  onStatus: (saved: SavedSession) => void;
  onFinish: (saved: SavedSession, messages: Message[], failure?: string) => Promise<void>;
};

export function LiveConversation({ allocation, previous, onMessages, onStatus, onFinish }: Props) {
  const tokenSource = useMemo(() => TokenSource.literal(allocation), [allocation]);
  const session = useSession(tokenSource, { agentConnectTimeoutMilliseconds: 45000 });
  const agent = useAgent(session);
  const { messages } = useSessionMessages(session);
  const { canPlayAudio, mergedProps } = useStartAudio({ room: session.room, props: {} });
  const [phase, setPhase] = useState<'connecting' | 'connected' | 'ending'>('connecting');
  const [muted, setMuted] = useState(false);
  const [error, setError] = useState('');
  const [endBusy, setEndBusy] = useState(false);
  const endInFlight = useRef(false);
  const connectionFailure = useRef<string | undefined>(undefined);
  const ending = useRef(false);
  const finalized = useRef(false);
  const latestMessages = useRef(previous);
  const callbacks = useRef({ onMessages, onStatus, onFinish });
  callbacks.current = { onMessages, onStatus, onFinish };
  const actions = useRef({ start: session.start, end: session.end });
  actions.current = { start: session.start, end: session.end };
  const startAbort = useRef<AbortController | null>(null);
  const baseline = useRef(previous);

  useEffect(() => {
    // The SDK updates partial transcripts by ID. Keep each logical message only once.
    const live = new Map<string, Message>();
    for (const message of messages)
      live.set(message.id, {
        id: message.id,
        role: message.from?.isLocal ? 'user' : 'assistant',
        text: message.message,
      });
    const combined = [...baseline.current, ...live.values()].slice(-200);
    latestMessages.current = combined;
    callbacks.current.onMessages(combined);
  }, [messages]);

  useEffect(() => {
    let disposed = false;
    let polling = false;
    const abort = new AbortController();
    startAbort.current = abort;
    const timeout = setTimeout(() => abort.abort(), 45000);
    const launch = setTimeout(async () => {
      try {
        // Playback may need another user gesture; the audio unlock button handles that.
        await session.room.startAudio().catch(() => {});
        await actions.current.start({
          signal: abort.signal,
          tracks: { microphone: { enabled: true } },
        });
        if (!disposed && !ending.current) setPhase('connected');
      } catch (error) {
        if (disposed || ending.current) return;
        ending.current = true;
        setPhase('ending');
        const cause = errorMessage(error);
        const message = cause.includes('could not establish pc connection')
          ? 'Audio connection failed (WebRTC). Service checks do not test the browser media network. For Windows/WSL localhost, restart LiveKit with config/livekit-local.yaml and retry.'
          : `Connection failed: ${cause}`;
        connectionFailure.current = message;
        try {
          await control(`sessions/${allocation.session.session_id}/end`, {
            attempt_id: allocation.session.attempt_id,
          });
        } catch {
          /* Status polling will reconcile an unavailable control API. */
        }
        await actions.current.end();
        if (!disposed) setError(message);
      } finally {
        clearTimeout(timeout);
      }
    }, 0);
    const poll = setInterval(async () => {
      if (disposed || polling || finalized.current) return;
      polling = true;
      try {
        const { session: saved } = await control<{ session: SavedSession }>(
          `sessions/${allocation.session.session_id}`,
        );
        if (disposed || finalized.current) return;
        callbacks.current.onStatus(saved);
        if (saved.status === 'completed' || saved.status === 'failed') {
          finalized.current = true;
          ending.current = true;
          await actions.current.end();
          if (!disposed)
            await callbacks.current.onFinish(
              saved,
              latestMessages.current,
              connectionFailure.current ||
                (saved.status === 'failed'
                  ? 'The session ended unexpectedly. Its saved history is available, but it cannot be resumed.'
                  : undefined),
            );
        }
      } catch {
        /* A temporary API outage must not tear down working audio. */
      } finally {
        polling = false;
      }
    }, 2000);
    const disconnected = () => {
      if (!ending.current && !disposed)
        setError('Connection lost. Waiting for the backend to finalize this session.');
    };
    session.room.on(RoomEvent.Disconnected, disconnected);
    return () => {
      disposed = true;
      clearTimeout(launch);
      clearTimeout(timeout);
      clearInterval(poll);
      abort.abort();
      session.room.off(RoomEvent.Disconnected, disconnected);
      void actions.current.end();
    };
  }, [allocation, session.room]);

  useEffect(() => {
    // Closing the tab is best effort; an unconfirmed close is never shown as completed.
    const leave = () => {
      if (finalized.current) return;
      navigator.sendBeacon(
        `/api/control/sessions/${allocation.session.session_id}/end`,
        new Blob([JSON.stringify({ attempt_id: allocation.session.attempt_id })], {
          type: 'application/json',
        }),
      );
    };
    window.addEventListener('pagehide', leave);
    return () => window.removeEventListener('pagehide', leave);
  }, [allocation]);

  async function end() {
    if (finalized.current || endInFlight.current) return;
    endInFlight.current = true;
    setEndBusy(true);
    ending.current = true;
    setPhase('ending');
    setError('');
    // Aborting connection would disconnect before checkpointing, so stop only input here.
    try {
      await session.room.localParticipant.setMicrophoneEnabled(false);
      setMuted(true);
      await control(`sessions/${allocation.session.session_id}/end`, {
        attempt_id: allocation.session.attempt_id,
      });
      const deadline = Date.now() + 20000;
      while (Date.now() < deadline && !finalized.current) {
        const { session: saved } = await control<{ session: SavedSession }>(
          `sessions/${allocation.session.session_id}`,
        );
        if (finalized.current) return;
        if (saved.status === 'completed' || saved.status === 'failed') {
          finalized.current = true;
          startAbort.current?.abort();
          await actions.current.end();
          await callbacks.current.onFinish(
            saved,
            latestMessages.current,
            saved.status === 'failed'
              ? 'The session ended unexpectedly and cannot be resumed.'
              : undefined,
          );
          return;
        }
        await sleep(500);
      }
      if (!finalized.current)
        setError(
          'Still waiting for the backend to save this session. You can retry End conversation.',
        );
    } catch (error) {
      setError(
        `Unable to confirm session completion: ${errorMessage(error)} Retry End conversation.`,
      );
    } finally {
      endInFlight.current = false;
      setEndBusy(false);
    }
  }

  async function toggleMicrophone() {
    try {
      await session.room.localParticipant.setMicrophoneEnabled(muted);
      setMuted(!muted);
    } catch (error) {
      setError(`Microphone update failed: ${errorMessage(error)}`);
    }
  }

  return (
    <AgentSessionProvider session={session}>
      <section className="voice-panel" aria-label="Voice controls">
        <div className="voice-top">
          <span className="eyebrow">VOICE</span>
          <span className="voice-state" role="status">
            {phase === 'ending'
              ? 'Saving session…'
              : phase === 'connecting'
                ? 'Connecting…'
                : agent.state}
          </span>
        </div>
        <AgentAudioVisualizerWave
          className="wave"
          state={agent.state}
          audioTrack={agent.microphoneTrack}
          color="#86E9BE"
        />
        <div className="voice-bottom">
          <span className="voice-hint">
            {phase === 'connected'
              ? 'Speak to interrupt. Your transcript updates live.'
              : 'Waiting for the agent…'}
          </span>
          <div className="voice-buttons">
            <button
              className="mic-button"
              disabled={phase !== 'connected'}
              aria-pressed={muted}
              onClick={() => void toggleMicrophone()}
            >
              {muted ? 'Unmute mic' : 'Mute mic'}
            </button>
            <button className="end-button" disabled={endBusy} onClick={() => void end()}>
              End conversation
            </button>
          </div>
        </div>
        {!canPlayAudio && (
          <button {...mergedProps} className="audio-unlock">
            Enable audio playback
          </button>
        )}
        {error && (
          <p className="voice-error" role="alert">
            {error}
          </p>
        )}
      </section>
    </AgentSessionProvider>
  );
}
