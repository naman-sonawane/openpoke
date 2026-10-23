export const runtime = 'nodejs';

const serverBase = process.env.PY_SERVER_URL || 'http://localhost:8001';

async function forward(req: Request, action: string) {
  const url = `${serverBase.replace(/\/$/, '')}/api/v1/intake/${encodeURIComponent(action)}`;
  try {
    const upstream = await fetch(url, {
      method: req.method,
      headers: { 'Content-Type': 'application/json' },
      body: req.method === 'GET' ? undefined : await req.text(),
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

export async function GET(req: Request, { params }: { params: { action: string } }) {
  return forward(req, params.action);
}

export async function POST(req: Request, { params }: { params: { action: string } }) {
  return forward(req, params.action);
}
