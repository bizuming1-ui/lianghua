import { setAuto } from "@/lib/quant/engine";

export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as { enabled?: boolean };
  await setAuto(!!body.enabled);
  return Response.json({ ok: true, enabled: !!body.enabled });
}
