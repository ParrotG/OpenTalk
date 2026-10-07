'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { AgentAudioVisualizerWave } from '@/components/agents-ui/agent-audio-visualizer-wave';
import { LiveConversation } from '@/components/live-conversation';
import {
  Allocation,
  Health,
  Message,
  SavedSession,
  control,
  errorMessage,
  history,
} from '@/lib/control';

function date(timestamp: number) {
  return new Date(timestamp * 1000).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function Transcript({ messages }: { messages: Message[] }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [messages]);
  return (
    <section className="transcript panel" aria-label="Transcript">
      <div className="section-title">
        <h2>Transcript</h2>
        <span>Recent messages</span>
      </div>
      <div
        className="messages"
        role="log"
        aria-label="Conversation transcript"
        aria-live="polite"
        aria-relevant="additions text"
      >
        {messages.length ? (
          messages.map((message) => (
            <article key={message.id} className={`message ${message.role}`}>
              <span className="message-role">{message.role === 'user' ? 'You' : 'Assistant'}</span>
              <p>{message.text}</p>
              {message.interrupted && <small>Interrupted</small>}
            </article>
          ))
        ) : (
          <div className="empty-transcript">
            <svg
              className="empty-mark"
              width="42"
              height="28"
              viewBox="0 0 42 28"
              fill="none"
              aria-hidden="true"
            >
              <path d="M1 14h6l4-8 7 16 6-16 6 16 4-8h7" stroke="currentColor" strokeWidth="1.3" />
            </svg>
            <p>Your conversation will appear here.</p>
            <small>Speak naturally. You can interrupt the assistant by speaking.</small>
          </div>
        )}
        <div ref={end} />
      </div>
    </section>
  );
}

export function TalkDemo() {
  const [health, setHealth] = useState<Health | null>(null);
  const [recent, setRecent] = useState<SavedSession[]>([]);
  const [selected, setSelected] = useState<SavedSession | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [allocation, setAllocation] = useState<Allocation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const busyRef = useRef(false);
  const selectionVersion = useRef(0);
  const pendingRequest = useRef<{ resume?: string; id: string } | null>(null);

  const refresh = useCallback(async () => {
    const results = await Promise.allSettled([
      control<Health>('health/services'),
      control<SavedSession[]>('sessions?limit=20'),
    ]);
    setHealth(
      results[0].status === 'fulfilled'
        ? results[0].value
        : { status: 'unavailable', services: { sessions: { status: 'unavailable' } } },
    );
    if (results[1].status === 'fulfilled') setRecent(results[1].value);
  }, []);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => clearInterval(timer);
  }, [refresh]);

  async function select(saved: SavedSession) {
    const version = ++selectionVersion.current;
    setError('');
    try {
      const [detail, transcript] = await Promise.all([
        control<{ session: SavedSession }>(`sessions/${saved.session_id}`),
        history(saved.session_id),
      ]);
      if (version !== selectionVersion.current || busyRef.current) return;
      setSelected(detail.session);
      setMessages(transcript);
    } catch (error) {
      setError(errorMessage(error));
    }
  }

  async function start(resume?: string) {
    if (busyRef.current || allocation) return;
    busyRef.current = true;
    setBusy(true);
    setError('');
    ++selectionVersion.current;
    try {
      if (!navigator.mediaDevices?.getUserMedia)
        throw new Error('Microphone access requires localhost or HTTPS.');
      // Ask for microphone permission before allocating a persistent attempt.
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
        video: false,
      });
      stream.getTracks().forEach((track) => track.stop());
      const transcript = resume ? await history(resume) : [];
      if (!pendingRequest.current || pendingRequest.current.resume !== resume)
        pendingRequest.current = { resume, id: crypto.randomUUID() };
      const created = await control<Allocation>('sessions', {
        request_id: pendingRequest.current.id,
        resume_session_id: resume,
      });
      pendingRequest.current = null;
      setMessages(transcript);
      setSelected(created.session);
      setAllocation(created);
      void refresh();
    } catch (error) {
      const message =
        error instanceof DOMException &&
        ['NotAllowedError', 'NotFoundError', 'NotReadableError'].includes(error.name)
          ? 'Microphone is unavailable. Allow microphone access and check your input device, then try again.'
          : errorMessage(error);
      setError(message);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  const finish = useCallback(
    async (saved: SavedSession, liveMessages: Message[], failure?: string) => {
      setAllocation(null);
      setSelected(saved);
      setMessages(liveMessages);
      if (failure) setError(failure);
      try {
        setMessages(await history(saved.session_id));
      } catch {
        setError(
          'Session status was saved, but its transcript could not be loaded. Select the session to retry.',
        );
      }
      void refresh();
    },
    [refresh],
  );

  const ready = health?.status === 'ok';
  return (
    <main className="shell">
      <header className="header">
        <a href="/" className="brand">
          <span className="brand-symbol">o</span>OpenTalk
        </a>
        <span className="eyebrow">VOICE DEMO</span>
      </header>
      <div className="intro">
        <h1>A conversation, simply.</h1>
        <p>Connect your microphone. Pick up where you left off.</p>
      </div>
      {error && (
        <div className="error" role="alert">
          {error}
          <button aria-label="Dismiss error" onClick={() => setError('')}>
            ×
          </button>
        </div>
      )}
      <div className="workspace">
        <div className="conversation">
          {allocation ? (
            <LiveConversation
              key={allocation.session.attempt_id}
              allocation={allocation}
              previous={messages}
              onMessages={setMessages}
              onStatus={setSelected}
              onFinish={finish}
            />
          ) : (
            <section className="voice-panel" aria-label="Voice controls">
              <div className="voice-top">
                <span className="eyebrow">VOICE</span>
                <span className="voice-state">
                  {busy
                    ? 'Preparing microphone'
                    : selected
                      ? selected.status
                      : 'Ready when you are'}
                </span>
              </div>
              <AgentAudioVisualizerWave className="wave" state="disconnected" color="#86E9BE" />
              <div className="voice-bottom">
                <span className="voice-hint">
                  {selected
                    ? 'Start fresh or resume a completed session.'
                    : 'Your microphone is used only while connected.'}
                </span>
                <button className="primary" disabled={busy || !ready} onClick={() => void start()}>
                  {busy ? 'Connecting…' : 'Start conversation'}
                  <span aria-hidden="true">↗</span>
                </button>
              </div>
            </section>
          )}
          <Transcript messages={messages} />
        </div>
        <aside className="sidebar">
          <section className="panel session-panel" aria-label="Session details">
            <div className="section-title">
              <h2>Session</h2>
              <span className={`status ${selected?.status || 'idle'}`}>
                {selected?.status || 'idle'}
              </span>
            </div>
            {selected ? (
              <>
                <label>SESSION ID</label>
                <code title={selected.session_id}>{selected.session_id}</code>
                <div className="session-meta">
                  <span>Started</span>
                  <span>{date(selected.created_at)}</span>
                </div>
                <div className="session-meta">
                  <span>Attempt</span>
                  <code title={selected.attempt_id}>{selected.attempt_id.slice(0, 12)}</code>
                </div>
                {!allocation && selected.status === 'completed' && (
                  <button
                    className="secondary full"
                    disabled={busy || !ready}
                    onClick={() => void start(selected.session_id)}
                  >
                    Resume session <span aria-hidden="true">↗</span>
                  </button>
                )}
                {selected.status === 'failed' && (
                  <p className="caption">This session ended unexpectedly and cannot be resumed.</p>
                )}
                {selected.status === 'active' && !allocation && (
                  <p className="caption">This session is active in another connection.</p>
                )}
              </>
            ) : (
              <p className="caption">
                A new session begins when you connect. Completed sessions can be resumed.
              </p>
            )}
          </section>
          <section className="panel recent-panel" aria-label="Recent sessions">
            <div className="section-title">
              <h2>Recent sessions</h2>
              <button className="text-button" onClick={() => void refresh()}>
                Refresh
              </button>
            </div>
            <div className="recent-list">
              {recent.length ? (
                recent.map((saved) => (
                  <button
                    className={`recent-item ${selected?.session_id === saved.session_id ? 'selected' : ''}`}
                    disabled={!!allocation || busy}
                    key={saved.session_id}
                    onClick={() => void select(saved)}
                  >
                    <span>
                      <code>{saved.session_id.slice(0, 12)}</code>
                      <small>{date(saved.updated_at)}</small>
                    </span>
                    <span className={`dot ${saved.status}`} title={saved.status} />
                    <span className="sr-only">{saved.status}</span>
                  </button>
                ))
              ) : (
                <p className="caption">No sessions yet.</p>
              )}
            </div>
          </section>
          <section className="services" aria-label="Backend services">
            <div className="section-title">
              <h2>Services</h2>
              <span className={`dot ${ready ? 'completed' : 'failed'}`} />
            </div>
            {health ? (
              Object.entries(health.services).map(([name, check]) => (
                <div className="service" key={name}>
                  <span>
                    {(
                      {
                        sessions: 'Session API',
                        livekit: 'LiveKit API',
                        worker: 'Agent worker',
                        providers: 'Model configuration',
                      } as Record<string, string>
                    )[name] || name}
                  </span>
                  <span
                    className={`service-state ${['ok', 'configured'].includes(check.status) ? 'available' : ''}`}
                  >
                    {check.status.replaceAll('_', ' ')}
                  </span>
                </div>
              ))
            ) : (
              <p className="caption">Checking services…</p>
            )}
            <p className="caption">
              Checks run every 10 seconds. They do not test browser media connectivity or call model
              providers.
            </p>
          </section>
        </aside>
      </div>
      <footer>
        OpenTalk <span>·</span> A small space for voice.
      </footer>
    </main>
  );
}
