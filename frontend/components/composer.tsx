'use client';

import { useEffect, useRef, type ReactNode } from 'react';
import { ArrowUp, AudioLines, X } from 'lucide-react';

type Props = {
  draft: string;
  onDraft: (value: string) => void;
  onSend: () => void;
  onVoice: () => void;
  voice?: boolean;
  disabled?: boolean;
  sending?: boolean;
  visualizer?: ReactNode;
};

export function Composer({
  draft,
  onDraft,
  onSend,
  onVoice,
  voice = false,
  disabled = false,
  sending = false,
  visualizer,
}: Props) {
  const input = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (!input.current) return;
    input.current.style.height = 'auto';
    input.current.style.height = `${Math.min(input.current.scrollHeight, 160)}px`;
  }, [draft, voice]);
  return (
    <form
      className={`composer ${voice ? 'voice-composer' : ''}`}
      aria-label="Message composer"
      onSubmit={(event) => {
        event.preventDefault();
        if (!disabled && !sending && draft.trim()) onSend();
      }}
    >
      {voice ? (
        <div className="voice-visualizer">{visualizer}</div>
      ) : (
        <textarea
          ref={input}
          aria-label="Message"
          placeholder="Message OpenTalk"
          rows={1}
          maxLength={8000}
          value={draft}
          disabled={disabled}
          onChange={(event) => onDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              if (!disabled && !sending && draft.trim()) onSend();
            }
          }}
        />
      )}
      <div className="composer-actions">
        {!voice && draft.trim() && (
          <button
            type="submit"
            className="icon-button send-button"
            aria-label="Send message"
            title="Send message"
            disabled={disabled || sending}
          >
            <ArrowUp aria-hidden="true" size={20} />
          </button>
        )}
        <button
          type="button"
          className={`icon-button ${voice ? 'cancel-voice' : 'voice-button'}`}
          aria-label={voice ? 'Cancel voice' : 'Start voice'}
          title={voice ? 'Cancel voice' : 'Start voice'}
          aria-pressed={voice}
          disabled={disabled || sending}
          onClick={onVoice}
        >
          {voice ? <X aria-hidden="true" size={20} /> : <AudioLines aria-hidden="true" size={21} />}
        </button>
      </div>
    </form>
  );
}
