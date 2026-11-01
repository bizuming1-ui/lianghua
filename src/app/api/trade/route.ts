import { placeOrder } from "@/lib/quant/engine";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  let body: { symbol?: string; side?: string; qty?: number };
  try {
    body = await req.json();
  } catch {
    return Response.json({ ok: false, error: "请求格式错误" }, { status: 400 });
  }
  const side = String(body.side ?? "").toUpperCase();
  if (side !== "BUY" && side !== "SELL") {
    return Response.json({ ok: false, error: "side 必须是 BUY 或 SELL" }, { status: 400 });
  }
  const r = await placeOrder(String(body.symbol ?? ""), side, Number(body.qty), "manual");
  return Response.json(r, { status: r.ok ? 200 : 400 });
}
