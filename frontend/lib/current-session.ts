import type { SavedSession } from '@/lib/control';

const key = 'opentalk.currentSession';
export type CurrentSession = { id: string; attempt?: string; closing?: boolean };

export function currentSession(): CurrentSession | null {
  try {
    const value = JSON.parse(localStorage.getItem(key) || 'null');
    return value && typeof value.id === 'string' && /^[a-zA-Z0-9_-]+$/.test(value.id)
      ? value
      : null;
  } catch {
    return null;
  }
}

export function rememberSession(saved: SavedSession | null, closing = false) {
  try {
    if (saved)
      localStorage.setItem(
        key,
        JSON.stringify({
          id: saved.session_id,
          attempt: saved.attempt_id,
          closing,
        }),
      );
    else localStorage.removeItem(key);
  } catch {
    // Persistence of native history does not depend on browser storage being available.
  }
}
