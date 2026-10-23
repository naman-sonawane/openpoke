export const runtime = 'nodejs';

const serverBase = process.env.PY_SERVER_URL || 'http://localhost:8001';

// GET /api/voice/transcript?call_id=… passes straight through.
export async function GET(req: Request, { params }: { params: { action: string } }) {
  const search = new URL(req.url).search;
  const url = `${serverBase.replace(/\/$/, '')}/api/v1/voice/${encodeURIComponent(params.action)}${search}`;
  try {
    const upstream = await fetch(url, { cache: 'no-store' });
    return new Response(await upstream.text(), {
      status: upstream.status,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  } catch (e: any) {
    return new Response(JSON.stringify({ error: e?.message || 'Upstream error' }), { status: 502 });
  }
}

// Proxies /api/voice/{start,turn} to the Python server. Unlike /api/chat this
// waits for the reply, so a voice turn can be spoken as soon as it lands.
export async function POST(req: Request, { params }: { params: { action: string } }) {
  const url = `${serverBase.replace(/\/$/, '')}/api/v1/voice/${encodeURIComponent(params.action)}`;
  try {
    const upstream = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: await req.text(),
      cache: 'no-store',
    });
    return new Response(await upstream.text(), {
      status: upstream.status,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  } catch (e: any) {
    return new Response(JSON.stringify({ error: e?.message || 'Upstream error' }), { status: 502 });
  }
}
