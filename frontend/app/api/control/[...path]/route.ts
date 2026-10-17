import { NextRequest, NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';
const backend = () => (process.env.OPENTALK_API_URL || 'http://127.0.0.1:8080').replace(/\/$/, '');
const validPath =
  /^(health\/services|documents(?:\/[0-9a-f]{64})?|sessions|sessions\/[a-zA-Z0-9_-]+(?:\/(history|end))?)$/;
let lastFailureLog = 0;

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const path = (await context.params).path.join('/');
  if (
    !validPath.test(path) ||
    (request.method === 'POST' && !/^(documents|sessions(?:\/[a-zA-Z0-9_-]+\/end)?)$/.test(path)) ||
    (request.method === 'DELETE' &&
      !/^(documents\/[0-9a-f]{64}|sessions\/[a-zA-Z0-9_-]+)$/.test(path))
  ) {
    return NextResponse.json({ message: 'Endpoint not found.' }, { status: 404 });
  }
  // Next.js can normalize nextUrl to localhost even when the browser uses 127.0.0.1.
  const origin = request.headers.get('origin');
  if (
    ['POST', 'DELETE'].includes(request.method) &&
    origin &&
    (!URL.canParse(origin) || new URL(origin).host !== request.headers.get('host'))
  ) {
    return NextResponse.json(
      { message: 'Cross-origin requests are not allowed.' },
      { status: 403 },
    );
  }
  try {
    let body: BodyInit | undefined;
    let headers: HeadersInit | undefined;
    if (request.method === 'POST' && path === 'documents') {
      const contentType = request.headers.get('content-type');
      if (!contentType?.startsWith('multipart/form-data;')) {
        return NextResponse.json(
          { message: 'A multipart PDF upload is required.' },
          { status: 400 },
        );
      }
      body = request.body || undefined;
      headers = { 'Content-Type': contentType };
    } else if (request.method === 'POST') {
      const raw = await request.text();
      if (raw.length > 16384)
        return NextResponse.json({ message: 'Request is too large.' }, { status: 413 });
      const data = JSON.parse(raw);
      if (!data || typeof data !== 'object' || Array.isArray(data)) {
        return NextResponse.json({ message: 'A JSON object is required.' }, { status: 400 });
      }
      if (path === 'sessions') {
        // Agent selection stays on the server; the UI has no business-specific controls.
        let agent = process.env.OPENTALK_AGENT || 'conversation';
        if (data.resume_session_id) {
          if (
            typeof data.resume_session_id !== 'string' ||
            !/^[a-zA-Z0-9_-]+$/.test(data.resume_session_id)
          ) {
            return NextResponse.json({ message: 'Invalid session ID.' }, { status: 400 });
          }
          const saved = await fetch(`${backend()}/sessions/${data.resume_session_id}`, {
            cache: 'no-store',
            signal: AbortSignal.timeout(8000),
          });
          if (!saved.ok)
            return new NextResponse(await saved.text(), {
              status: saved.status,
              headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
            });
          agent = (await saved.json()).session.agent_key;
        }
        body = JSON.stringify({
          request_id: data.request_id,
          resume_session_id: data.resume_session_id,
          voice_enabled: data.voice_enabled,
          agent,
        });
      } else {
        body = JSON.stringify({ attempt_id: data.attempt_id });
      }
      headers = { 'Content-Type': 'application/json' };
    }
    const url = new URL(`${backend()}/${path}`);
    for (const name of ['limit', 'offset', 'tail']) {
      const value = request.nextUrl.searchParams.get(name);
      if (value !== null) url.searchParams.set(name, value);
    }
    const response = await fetch(url, {
      method: request.method,
      body,
      cache: 'no-store',
      headers,
      signal: AbortSignal.timeout(
        path === 'documents' && request.method === 'POST'
          ? 120000
          : request.method === 'DELETE'
            ? 30000
            : 8000,
      ),
      ...(path === 'documents' && request.method === 'POST' ? { duplex: 'half' } : {}),
    } as RequestInit & { duplex?: 'half' });
    return new NextResponse(await response.text(), {
      status: response.status,
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
    });
  } catch (error) {
    if (error instanceof SyntaxError)
      return NextResponse.json({ message: 'Invalid JSON request.' }, { status: 400 });
    if (Date.now() - lastFailureLog > 30000) {
      console.warn('OpenTalk API is unavailable. Check OPENTALK_API_URL and backend services.');
      lastFailureLog = Date.now();
    }
    return NextResponse.json(
      {
        error: 'backend_unavailable',
        message: 'OpenTalk API is unavailable. Check backend services and try again.',
      },
      { status: 503 },
    );
  }
}

export { proxy as DELETE, proxy as GET, proxy as POST };
