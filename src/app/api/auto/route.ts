import { setAuto } from "@/lib/quant/engine";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  let body: { enabled?: boolean; mode?: string } = {};
  try {
    body = await req.json();
  } catch {
    /* 空请求体按关闭处理 */
  }
  const mode = String(body.mode ?? (body.enabled ? "rule" : "off")).toLowerCase();
  if (mode === "ai") {
    return Response.json({ ok: false, error: "AI 推理策略仅在 Kaggle(T4×2)版可用,请按『复制代码界面』部署" }, { status: 400 });
  }
  if (mode !== "off" && mode !== "rule") {
    return Response.json({ ok: false, error: "mode 必须是 off / rule / ai" }, { status: 400 });
  }
  await setAuto(mode === "rule");
  return Response.json({ ok: true, mode, enabled: mode === "rule" });
}
