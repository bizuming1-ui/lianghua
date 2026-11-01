import { db } from "@/db";
import { sql } from "drizzle-orm";
import { ensureStarted } from "@/lib/quant/engine";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    await db.execute(sql`select 1`);
    ensureStarted(); // 健康检查同时唤醒 1 秒行情轮询
    return Response.json({ ok: true });
  } catch {
    return Response.json({ ok: false }, { status: 500 });
  }
}
