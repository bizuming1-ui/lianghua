import { resetAccount } from "@/lib/quant/engine";

export const dynamic = "force-dynamic";

export async function POST() {
  await resetAccount();
  return Response.json({ ok: true });
}
