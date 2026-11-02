import { historyBars } from "@/lib/quant/history";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const u = new URL(req.url);
  try {
    return Response.json(await historyBars(u.searchParams.get("code") ?? "", u.searchParams.get("tf") ?? "5m"), {
      headers: { "Cache-Control": "no-store" },
    });
  } catch (e) {
    return Response.json({ ok: false, error: (e as Error).message, bars: [] }, { status: 400 });
  }
}
