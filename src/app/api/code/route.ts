import { buildCodePack } from "@/lib/codePack";

export const dynamic = "force-dynamic";

export async function GET() {
  return Response.json(await buildCodePack(), { headers: { "Cache-Control": "no-store" } });
}
