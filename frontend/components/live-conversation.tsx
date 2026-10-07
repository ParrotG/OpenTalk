'use client';

import { useEffect, useImperativeHandle, useMemo, useRef, useState, type Ref } from 'react';
import { ParticipantKind, RoomEvent, TokenSource } from 'livekit-client';
import { useAgent, useSession, useSessionMessages, useStartAudio } from '@livekit/components-react';
import { AgentSessionProvider } from '@/components/agents-ui/agent-session-provider';
import { AgentAudioVisualizerWave } from '@/components/agents-ui/agent-audio-visualizer-wave';
import { Composer } from '@/components/composer';
import { liveMessageKey } from '@/lib/transcript';
import { rememberSession } from '@/lib/current-session';
import { Allocation, Message, SavedSession, control, errorMessage, sleep } from '@/lib/control';

export type ConversationHandle = { end: () => Promise<boolean> };

type Props = {
  ref?: Ref<ConversationHandle>;
  initialVoice: boolean;
  initialText: string;
  draft: string;
  onDraft: (value: string) => void;
  allocation: Allocation;
  previous: Message[];
  onMessages: (messages: Message[]) => void;
  onStatus: (saved: SavedSession) => void;
  onFinish: (saved: SavedSession, messages: Message[], failure?: string) => Promise<void>;
};

export function LiveConversation({
  ref,
  allocation,
  previous,
  initialVoice,
  initialText,
  draft,
  onDraft,
  onMessages,
  onStatus,
  onFinish,
}: Props) {
  const tokenSource = useMemo(() => TokenSource.literal(allocation), [allocation]);
  const session = useSession(tokenSource, { agentConnectTimeoutMilliseconds: 45000 });
  const agent = useAgent(session);
  const { messages, send, isSending } = useSessionMessages(session);
  const { canPlayAudio, mergedProps } = useStartAudio({ room: session.room, props: {} });
  const [phase, setPhase] = useState<'connecting' | 'connected' | 'ending'>('connecting');
  const [voice, setVoice] = useState(initialVoice);
  const [micBusy, setMicBusy] = useState(false);
  const microphoneBusy = useRef(false);
  const queuedText = useRef(initialText);
  const initialMicrophone = useRef(initialVoice);
  const sendMessage = useRef(send);
  sendMessage.current = send;
  const latestDraft = useRef(draft);
  latestDraft.current = draft;
  const updateDraft = useRef(onDraft);
  updateDraft.current = onDraft;
  const [error, setError] = useState('');
  const [endBusy, setEndBusy] = useState(false);
  const closePromise = useRef<Promise<boolean> | null>(null);
  const finishPromise = useRef<Promise<boolean> | null>(null);
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
  const liveMessages = useRef(new Map<string, Message>());

  useEffect(() => {
    // Revisions change stream IDs but retain the transcription segment ID.
    const live = liveMessages.current;
    for (const message of messages.slice(-200)) {
      // An empty assistant stream must not split a user turn before text arrives.
      if (!message.message.trim()) continue;
      const id = liveMessageKey(message);
      live.set(id, {
        id,
        role: message.from?.isLocal ? 'user' : 'assistant',
        text: message.message,
      });
    }
    while (live.size > 200) live.delete(live.keys().next().value!);
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
          tracks: { microphone: { enabled: initialMicrophone.current } },
        });
        if (!disposed && !ending.current) {
          await setVoiceMode(initialMicrophone.current);
          setPhase('connected');
        }
      } catch (error) {
        if (disposed || ending.current) return;
        if (queuedText.current) updateDraft.current(queuedText.current);
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
          if (!disposed)
            await finalize(
              saved,
              connectionFailure.current ||
                (saved.status === 'failed'
                  ? 'The conversation ended unexpectedly. Its history is available, but it cannot be continued.'
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
      rememberSession(allocation.session, true);
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

  async function setVoiceMode(enabled: boolean) {
    const destination = [...session.room.remoteParticipants.values()].find(
      (participant) =>
        participant.kind === ParticipantKind.AGENT &&
        !participant.attributes['lk.publish_on_behalf'],
    );
    if (!destination) throw new Error('The agent is not connected yet.');
    await session.room.localParticipant.performRpc({
      destinationIdentity: destination.identity,
      method: 'opentalk.set_voice_mode',
      payload: JSON.stringify({ enabled }),
      responseTimeout: 10_000,
    });
  }

  function finalize(saved: SavedSession, failure?: string): Promise<boolean> {
    if (finishPromise.current) return finishPromise.current;
    finalized.current = true;
    ending.current = true;
    finishPromise.current = (async () => {
      startAbort.current?.abort();
      await actions.current.end();
      await callbacks.current.onFinish(saved, latestMessages.current, failure);
      return true;
    })();
    return finishPromise.current;
  }

  function end(): Promise<boolean> {
    if (finishPromise.current) return finishPromise.current;
    if (closePromise.current) return closePromise.current;
    closePromise.current = closeSession().finally(() => {
      closePromise.current = null;
    });
    return closePromise.current;
  }

  async function closeSession(): Promise<boolean> {
    setEndBusy(true);
    ending.current = true;
    setPhase('ending');
    setError('');
    try {
      await session.room.localParticipant.setMicrophoneEnabled(false);
      setVoice(false);
      await control(`sessions/${allocation.session.session_id}/end`, {
        attempt_id: allocation.session.attempt_id,
      });
      const deadline = Date.now() + 20000;
      while (Date.now() < deadline) {
        if (finishPromise.current) return await finishPromise.current;
        const { session: saved } = await control<{ session: SavedSession }>(
          `sessions/${allocation.session.session_id}`,
        );
        if (saved.status === 'completed' || saved.status === 'failed') {
          return await finalize(
            saved,
            saved.status === 'failed'
              ? 'The conversation ended unexpectedly and cannot be continued.'
              : undefined,
          );
        }
        await sleep(500);
      }
      setError('Still saving this conversation. Try switching conversations again in a moment.');
    } catch (error) {
      setError(
        `Unable to save this conversation: ${errorMessage(error)} Try switching conversations again.`,
      );
    } finally {
      setEndBusy(false);
    }
    return false;
  }

  useImperativeHandle(ref, () => ({ end }));

  useEffect(() => {
    if (phase !== 'connected' || !queuedText.current) return;
    const text = queuedText.current;
    // Claim the queued message before sending so effect replays cannot send it twice.
    queuedText.current = '';
    void setVoiceMode(false)
      .then(() => sendMessage.current(text))
      .catch((error) => {
        updateDraft.current(text);
        setError(`Message could not be sent: ${errorMessage(error)}`);
      });
  }, [phase]);

  async function sendDraft() {
    const text = draft.trim();
    if (!text || phase !== 'connected' || isSending) return;
    setError('');
    try {
      await setVoiceMode(false);
      await send(text);
      if (latestDraft.current === draft) onDraft('');
    } catch (error) {
      setError(`Message could not be sent: ${errorMessage(error)}`);
    }
  }

  async function toggleVoice() {
    if (microphoneBusy.current || phase !== 'connected') return;
    microphoneBusy.current = true;
    setMicBusy(true);
    setError('');
    try {
      if (voice) {
        await session.room.localParticipant.setMicrophoneEnabled(false);
        setVoice(false);
        await setVoiceMode(false);
      } else {
        await setVoiceMode(true);
        try {
          await session.room.localParticipant.setMicrophoneEnabled(true);
        } catch (error) {
          await setVoiceMode(false);
          throw error;
        }
      }
      if (ending.current) {
        // A permission prompt may resolve after the user has already ended the session.
        await session.room.localParticipant.setMicrophoneEnabled(false);
        await setVoiceMode(false);
      } else {
        setVoice(!voice);
      }
    } catch (error) {
      setError(`Microphone update failed: ${errorMessage(error)}`);
    } finally {
      microphoneBusy.current = false;
      setMicBusy(false);
    }
  }

  return (
    <AgentSessionProvider session={session} muted={!voice}>
      <Composer
        draft={draft}
        onDraft={onDraft}
        onSend={() => void sendDraft()}
        onVoice={() => void toggleVoice()}
        voice={voice}
        disabled={phase !== 'connected' || endBusy || micBusy}
        sending={isSending}
        visualizer={
          <>
            <AgentAudioVisualizerWave
              className="wave"
              state={agent.state}
              audioTrack={agent.microphoneTrack}
              color="#84B6F4"
            />
            <span className="voice-state" role="status">
              {phase === 'ending'
                ? 'Saving session…'
                : phase === 'connecting'
                  ? 'Connecting…'
                  : agent.state}
            </span>
          </>
        }
      />
      {!voice && phase !== 'connected' && (
        <p className="composer-status" role="status">
          {phase === 'ending' ? 'Saving session…' : 'Connecting…'}
        </p>
      )}
      {voice && !canPlayAudio && (
        <button {...mergedProps} className="text-button audio-unlock">
          Enable audio playback
        </button>
      )}
      {error && (
        <p className="voice-error" role="alert">
          {error}
        </p>
      )}
    </AgentSessionProvider>
  );
}
