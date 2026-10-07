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
  sleep,
} from '@/lib/control';
import { currentSession, rememberSession, type CurrentSession } from '@/lib/current-session';
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
  const [restoring, setRestoring] = useState(true);
  const restored = useRef(false);
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

  async function newChat() {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      if (allocation && !(await live.current?.end())) return;
      ++selectionVersion.current;
      pendingRequest.current = null;
      rememberSession(null);
      setSelected(null);
      setMessages([]);
      setDraft('');
      setError('');
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  async function select(saved: { session_id: string }, remembered?: CurrentSession) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError('');
    let version = selectionVersion.current;
    try {
      if (allocation && !(await live.current?.end())) return;
      version = ++selectionVersion.current;
      let detail = await control<{ session: SavedSession }>(`sessions/${saved.session_id}`);
      if (
        remembered?.closing &&
        remembered.attempt === detail.session.attempt_id &&
        ['active', 'pending'].includes(detail.session.status)
      ) {
        // Retry only this browser's recorded normal-close intent, never another attempt.
        await control(`sessions/${saved.session_id}/end`, { attempt_id: remembered.attempt });
        detail = await control<{ session: SavedSession }>(`sessions/${saved.session_id}`);
      }
      const deadline = Date.now() + 25000;
      while (
        detail.session.end_requested &&
        detail.session.status === 'active' &&
        Date.now() < deadline
      ) {
        await sleep(500);
        detail = await control<{ session: SavedSession }>(`sessions/${saved.session_id}`);
      }
      const transcript = await history(saved.session_id);
      if (version !== selectionVersion.current) return;
      pendingRequest.current = null;
      setSelected(detail.session);
      rememberSession(detail.session);
      setMessages(transcript);
      setDraft('');
    } catch (error) {
      if (version === selectionVersion.current) setError(errorMessage(error));
    } finally {
      busyRef.current = false;
      setBusy(false);
      setRestoring(false);
    }
  }

  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    const saved = currentSession();
    if (saved) void select({ session_id: saved.id }, saved);
    else setRestoring(false);
  }, []);

  useEffect(() => {
    if (allocation || !selected || !['active', 'pending'].includes(selected.status)) return;
    const id = selected.session_id;
    const version = selectionVersion.current;
    let disposed = false;
    let polling = false;
    const timer = setInterval(async () => {
      if (polling || busyRef.current) return;
      polling = true;
      try {
        const detail = await control<{ session: SavedSession }>(`sessions/${id}`);
        if (disposed || version !== selectionVersion.current) return;
        setSelected(detail.session);
        if (['completed', 'failed'].includes(detail.session.status)) {
          const transcript = await history(id);
          if (!disposed && version === selectionVersion.current) {
            setMessages(transcript);
            rememberSession(detail.session);
          }
        }
      } catch {
        // Keep saved history visible during a temporary control API outage.
      } finally {
        polling = false;
      }
    }, 2000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [allocation, selected?.session_id, selected?.status]);

  async function start(voice: boolean, text = '') {
    if (busyRef.current || allocation || restoring) return;
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
        voice_enabled: voice,
      });
      pendingRequest.current = null;
      setInitialVoice(voice);
      setInitialText(text);
      if (text) setDraft('');
      setMessages(transcript);
      setSelected(created.session);
      rememberSession(created.session);
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
      rememberSession(saved);
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
            disabled={busy || restoring}
            onClick={() => void newChat()}
            aria-label="New chat"
            title="New chat"
          >
            <Plus size={18} aria-hidden="true" />
            <span className="toolbar-label">New chat</span>
          </button>
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
                      disabled={busy || restoring}
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
            : selected.end_requested
              ? 'This conversation is still being saved. It will be available shortly.'
              : 'This conversation is active in another connection. Start a new chat to continue.'}
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
            disabled={busy || restoring || !ready || archived}
          />
        )}
        {!allocation && (busy || restoring) && (
          <p className="composer-status" role="status">
            {restoring ? 'Loading conversation…' : 'Preparing conversation…'}
          </p>
        )}
      </div>
    </main>
  );
}
