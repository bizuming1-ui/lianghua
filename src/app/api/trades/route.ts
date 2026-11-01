import { listTrades } from "@/lib/quant/engine";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const u = new URL(req.url).searchParams;
  const offset = Math.max(0, Number(u.get("offset") ?? 0) || 0);
  const limit = Math.min(100000, Math.max(1, Number(u.get("limit") ?? 50) || 50));
  return Response.json(await listTrades(offset, limit), { headers: { "Cache-Control": "no-store" } });
}
