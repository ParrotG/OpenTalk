export type SavedSession = {
  session_id: string;
  attempt_id: string;
  status: 'pending' | 'active' | 'completed' | 'failed';
  agent_key: string;
  created_at: number;
  updated_at: number;
  ended_at: number | null;
  end_requested: boolean;
  error_code?: string | null;
};
export type Allocation = {
  session: SavedSession;
  serverUrl: string;
  participantToken: string;
  participantName: string;
  roomName: string;
};
export type Message = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  interrupted?: boolean;
};
export type Health = { status: string; services: Record<string, { status: string }> };

export async function control<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/control/${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store',
    signal: AbortSignal.timeout(10000),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || data.error || 'Session request failed.');
  return data as T;
}

export async function history(id: string): Promise<Message[]> {
  const data = await control<{
    items: {
      id: string;
      type: string;
      role?: string;
      content?: unknown[];
      interrupted?: boolean;
    }[];
  }>(`sessions/${id}/history?tail=true&limit=200`);
  return data.items
    .filter(
      (item) => item.type === 'message' && (item.role === 'user' || item.role === 'assistant'),
    )
    .map((item) => ({
      id: item.id,
      role: item.role as Message['role'],
      text: (item.content || []).filter((value) => typeof value === 'string').join('\n'),
      interrupted: item.interrupted,
    }))
    .filter((item) => item.text.trim());
}

export const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : 'An unexpected error occurred.';
