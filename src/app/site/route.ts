import { readFile } from "node:fs/promises";
import path from "node:path";

export const dynamic = "force-dynamic";

// 量化终端前端是一份与框架无关的单文件(public/terminal/index.html),
// Next 版与 Kaggle(Python)版共用同一份,保证两边界面完全一致。
export async function GET() {
  const html = await readFile(path.join(process.cwd(), "public", "terminal", "index.html"), "utf8");
  return new Response(html, {
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
  });
}
