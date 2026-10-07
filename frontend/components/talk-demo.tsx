'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, ChevronDown, History, Plus, RefreshCw, X } from 'lucide-react';
import { Composer } from '@/components/composer';
import { LiveConversation, type ConversationHandle } from '@/components/live-conversation';
import {
  Allocation,
  Health,
  Message,
  SavedSession,
  control,
  errorMessage,
  history,
} from '@/lib/control';
import { conversationBubbles } from '@/lib/transcript';

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
    <div
      className="transcript"
      role="log"
      aria-label="Conversation transcript"
      aria-live="polite"
      aria-relevant="additions text"
    >
      <div className="messages">
        {conversationBubbles(messages).map((message) => (
          <article key={message.id} className={`message ${message.role}`}>
            <p>{message.text}</p>
          </article>
        ))}
        <div ref={end} />
      </div>
    </div>
  );
}

export function TalkDemo() {
  const [health, setHealth] = useState<Health | null>(null);
  const [recent, setRecent] = useState<SavedSession[]>([]);
  const [selected, setSelected] = useState<SavedSession | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [allocation, setAllocation] = useState<Allocation | null>(null);
  const [initialVoice, setInitialVoice] = useState(false);
  const [initialText, setInitialText] = useState('');
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const busyRef = useRef(false);
  const selectionVersion = useRef(0);
  const pendingRequest = useRef<{ resume?: string; id: string } | null>(null);
  const live = useRef<ConversationHandle>(null);
  const header = useRef<HTMLElement>(null);

  const refresh = useCallback(async () => {
    const results = await Promise.allSettled([
      control<Health>('health/services'),
      control<SavedSession[]>('sessions?limit=20'),
    ]);
    setHealth(
      results[0].status === 'fulfilled'
        ? results[0].value
        : {
            status: 'unavailable',
            services: { sessions: { status: 'unavailable' } },
          },
    );
    if (results[1].status === 'fulfilled') setRecent(results[1].value);
  }, []);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    const close = (event: PointerEvent | KeyboardEvent) => {
      const escape = event instanceof KeyboardEvent && event.key === 'Escape';
      if (
        !escape &&
        (event instanceof KeyboardEvent ||
          (event.target instanceof Node && header.current?.contains(event.target)))
      )
        return;
      header.current?.querySelectorAll<HTMLDetailsElement>('details[open]').forEach((panel) => {
        panel.open = false;
        if (escape) panel.querySelector('summary')?.focus();
      });
    };
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', close);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', close);
    };
  }, []);

  function panelOpened(panel: HTMLDetailsElement) {
    if (!panel.open) return;
    header.current?.querySelectorAll('details').forEach((other) => {
      if (other !== panel) other.open = false;
    });
  }

  function newChat() {
    if (busyRef.current || allocation) return;
    ++selectionVersion.current;
    pendingRequest.current = null;
    setSelected(null);
    setMessages([]);
    setDraft('');
    setError('');
  }

  async function select(saved: SavedSession) {
    const version = ++selectionVersion.current;
    setError('');
    try {
      const [detail, transcript] = await Promise.all([
        control<{ session: SavedSession }>(`sessions/${saved.session_id}`),
        history(saved.session_id),
      ]);
      if (version !== selectionVersion.current || busyRef.current) return;
      pendingRequest.current = null;
      setSelected(detail.session);
      setMessages(transcript);
      setDraft('');
    } catch (error) {
      if (version === selectionVersion.current) setError(errorMessage(error));
    }
  }

  async function start(voice: boolean, text = '') {
    if (busyRef.current || allocation) return;
    busyRef.current = true;
    setBusy(true);
    setError('');
    ++selectionVersion.current;
    const resume = selected?.status === 'completed' ? selected.session_id : undefined;
    try {
      if (voice) {
        if (!navigator.mediaDevices?.getUserMedia)
          throw new Error('Microphone access requires localhost or HTTPS.');
        // Obtain permission before allocating a persistent voice attempt.
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true },
          video: false,
        });
        stream.getTracks().forEach((track) => track.stop());
      }
      const transcript = resume ? await history(resume) : [];
      if (!pendingRequest.current || pendingRequest.current.resume !== resume)
        pendingRequest.current = { resume, id: crypto.randomUUID() };
      const created = await control<Allocation>('sessions', {
        request_id: pendingRequest.current.id,
        resume_session_id: resume,
      });
      pendingRequest.current = null;
      setInitialVoice(voice);
      setInitialText(text);
      if (text) setDraft('');
      setMessages(transcript);
      setSelected(created.session);
      setAllocation(created);
      void refresh();
    } catch (error) {
      setError(
        error instanceof DOMException &&
          ['NotAllowedError', 'NotFoundError', 'NotReadableError'].includes(error.name)
          ? 'Microphone is unavailable. Allow microphone access and check your input device, then try again.'
          : errorMessage(error),
      );
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  const finish = useCallback(
    async (saved: SavedSession, liveMessages: Message[], failure?: string) => {
      const version = ++selectionVersion.current;
      setAllocation(null);
      setSelected(saved);
      setMessages(liveMessages);
      if (failure) setError(failure);
      try {
        const transcript = await history(saved.session_id);
        if (version === selectionVersion.current) setMessages(transcript);
      } catch {
        if (version === selectionVersion.current)
          setError(
            'Session status was saved, but its transcript could not be loaded. Select the session to retry.',
          );
      }
      void refresh();
    },
    [refresh],
  );

  const ready = health?.status === 'ok';
  const archived = !!selected && selected.status !== 'completed' && !allocation;
  return (
    <main className="shell">
      <header className="header" ref={header}>
        <a href="/" className="brand">
          OpenTalk
        </a>
        <div className="header-actions">
          <button
            className="toolbar-button"
            disabled={busy || !!allocation}
            onClick={newChat}
            aria-label="New chat"
            title="New chat"
          >
            <Plus size={18} aria-hidden="true" />
            <span className="toolbar-label">New chat</span>
          </button>
          {allocation && (
            <button
              className="text-button end-button"
              aria-label="End conversation"
              title="End conversation"
              onClick={() => void live.current?.end()}
            >
              End<span className="toolbar-label"> conversation</span>
            </button>
          )}
          {!allocation && selected?.status === 'completed' && (
            <button
              className="text-button"
              aria-label="Resume session"
              disabled={busy || !ready}
              onClick={() => void start(false)}
            >
              Resume<span className="toolbar-label"> session</span>
            </button>
          )}
          <details className="utility" onToggle={(event) => panelOpened(event.currentTarget)}>
            <summary aria-label="Recent sessions" title="Recent sessions">
              <History size={18} aria-hidden="true" />
              <span className="toolbar-label">Recent sessions</span>
              <ChevronDown className="chevron" size={13} aria-hidden="true" />
            </summary>
            <section className="utility-panel" aria-label="Recent sessions">
              <div className="section-title">
                <h2>Recent sessions</h2>
                <button
                  className="icon-button"
                  aria-label="Refresh sessions"
                  title="Refresh sessions"
                  onClick={() => void refresh()}
                >
                  <RefreshCw size={16} aria-hidden="true" />
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
          </details>
          <details className="utility" onToggle={(event) => panelOpened(event.currentTarget)}>
            <summary aria-label="Services" title="Services">
              <Activity size={18} aria-hidden="true" />
              <span className="toolbar-label">Services</span>
              <span className={`dot ${health ? (ready ? 'completed' : 'failed') : 'pending'}`} />
            </summary>
            <section className="utility-panel" aria-label="Backend services">
              <div className="section-title">
                <h2>Services</h2>
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
                Checks run every 10 seconds. They do not test browser media connectivity or call
                model providers.
              </p>
            </section>
          </details>
        </div>
      </header>
      {error && (
        <div className="error" role="alert">
          {error}
          <button className="icon-button" aria-label="Dismiss error" onClick={() => setError('')}>
            <X size={18} aria-hidden="true" />
          </button>
        </div>
      )}
      {archived && (
        <p className="archive-notice">
          {selected.status === 'failed'
            ? 'This session ended unexpectedly and cannot be resumed. Start a new chat to continue.'
            : 'This session is active in another connection. Start a new chat to continue.'}
        </p>
      )}
      <Transcript messages={messages} />
      <div className="composer-dock">
        {allocation ? (
          <LiveConversation
            key={allocation.session.attempt_id}
            ref={live}
            allocation={allocation}
            previous={messages}
            initialVoice={initialVoice}
            initialText={initialText}
            draft={draft}
            onDraft={setDraft}
            onMessages={setMessages}
            onStatus={setSelected}
            onFinish={finish}
          />
        ) : (
          <Composer
            draft={draft}
            onDraft={setDraft}
            onSend={() => void start(false, draft.trim())}
            onVoice={() => void start(true)}
            disabled={busy || !ready || archived}
          />
        )}
        {!allocation && busy && (
          <p className="composer-status" role="status">
            Connecting…
          </p>
        )}
      </div>
    </main>
  );
}
