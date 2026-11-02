export const dynamic = "force-dynamic";

export async function POST() {
  return Response.json({ ok: false, error: "AI 推理仅在 Kaggle(T4×2)版可用" }, { status: 400 });
}
