import { Message } from '@/lib/control';
import type { useSessionMessages } from '@livekit/components-react';

export function liveMessageKey(message: ReturnType<typeof useSessionMessages>['messages'][number]) {
  const segment = message.attributes?.['lk.segment_id'];
  return segment
    ? `transcript:${message.from?.identity}:${message.attributes?.['lk.transcribed_track_id'] || ''}:${segment}`
    : message.id;
}

/** Merge user fragments for display while preserving the native message history. */
export function conversationBubbles(messages: Message[]): Message[] {
  const unique = new Map(messages.map((message) => [message.id, message]));
  const bubbles: Message[] = [];
  for (const message of unique.values()) {
    if (!message.text.trim()) continue;
    const last = bubbles.at(-1);
    if (message.role === 'user' && last?.role === 'user') {
      last.text += `\n${message.text}`;
    } else {
      bubbles.push({ ...message });
    }
  }
  return bubbles;
}
