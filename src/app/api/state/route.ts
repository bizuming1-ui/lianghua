import { buildState } from "@/lib/quant/engine";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const since = Number(new URL(req.url).searchParams.get("since") ?? 0) || 0;
  try {
    const s = await buildState(since);
    return Response.json(s, { headers: { "Cache-Control": "no-store" } });
  } catch (e) {
    return Response.json({ error: (e as Error).message }, { status: 500 });
  }
}
