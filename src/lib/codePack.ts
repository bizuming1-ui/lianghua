import { readdir, readFile } from "node:fs/promises";
import path from "node:path";

export interface CodeFile {
  path: string; // 展示名 / GitHub 路径
  content: string;
  lines: number;
}

export interface CodePack {
  github: CodeFile[];
  kaggle: CodeFile[];
  generatedAt: number;
}

const ROOT_FILES = [
  "README.md",
  ".gitignore",
  ".env.example",
  "package.json",
  "tsconfig.json",
  "next.config.ts",
  "drizzle.config.json",
  "eslint.config.mjs",
  "postcss.config.mjs",
];
const DIRS = ["src", "public", "kaggle"];
const TEXT_EXT = new Set([".ts", ".tsx", ".js", ".mjs", ".json", ".md", ".css", ".html", ".py", ".txt", ".svg", ".yml", ".yaml"]);
const SKIP = new Set(["node_modules", ".next", ".git", "__pycache__"]);

async function walk(dir: string, base: string, out: string[]) {
  let entries;
  try {
    entries = await readdir(path.join(base, dir), { withFileTypes: true });
  } catch {
    return;
  }
  for (const e of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    if (SKIP.has(e.name)) continue;
    const rel = `${dir}/${e.name}`;
    if (e.isDirectory()) await walk(rel, base, out);
    else if (TEXT_EXT.has(path.extname(e.name))) out.push(rel);
  }
}

const countLines = (s: string) => (s.length === 0 ? 0 : s.split("\n").length - (s.endsWith("\n") ? 1 : 0));

async function read(base: string, rel: string): Promise<CodeFile | null> {
  try {
    const content = await readFile(path.join(base, rel), "utf8");
    return { path: rel, content, lines: countLines(content) };
  } catch {
    return null;
  }
}

/** 把 server.py / index.html 嵌入 launch_cell.py 模板,得到可直接粘贴到 Kaggle 的单个代码单元。 */
function embed(tpl: string, token: string, content: string): string {
  const marker = `r'''${token}'''`;
  const body = content.endsWith("\n") ? content : content + "\n";
  let replacement: string;
  if (body.includes("'''")) {
    // 极端情况:内容含三单引号,改用 base64,保证 100% 可运行
    const b64 = Buffer.from(body, "utf8").toString("base64");
    replacement = `__import__("base64").b64decode("${b64}").decode("utf-8")`;
  } else {
    replacement = `r'''${body}'''`;
  }
  return tpl.split(marker).join(replacement);
}

export async function buildCodePack(): Promise<CodePack> {
  const base = process.cwd();

  const paths: string[] = [...ROOT_FILES];
  for (const d of DIRS) await walk(d, base, paths);
  const github: CodeFile[] = [];
  for (const p of paths) {
    const f = await read(base, p);
    if (f) github.push(f);
  }

  const tpl = (await read(base, "kaggle/launch_cell.py"))?.content ?? "";
  const server = (await read(base, "kaggle/server.py"))?.content ?? "";
  const ai = (await read(base, "kaggle/ai_engine.py"))?.content ?? "";
  const math = (await read(base, "kaggle/quant_math.py"))?.content ?? "";
  const index = (await read(base, "public/terminal/index.html"))?.content ?? "";
  const status = (await read(base, "kaggle/status_cell.py"))?.content ?? "";
  const llm = (await read(base, "kaggle/llm_cell.py"))?.content ?? "";
  let cell1 = embed(tpl, "__SERVER_PY__", server);
  cell1 = embed(cell1, "__AI_PY__", ai);
  cell1 = embed(cell1, "__MATH_PY__", math);
  cell1 = embed(cell1, "__INDEX_HTML__", index);
  cell1 = embed(cell1, "__INDICATORS_PY__", (await read(base, "kaggle/indicators.py"))?.content ?? "");
  cell1 = embed(cell1, "__REFLECT_PY__", (await read(base, "kaggle/reflect.py"))?.content ?? "");
  cell1 = embed(cell1, "__PERSIST_PY__", (await read(base, "kaggle/persist.py"))?.content ?? "");

  const mk = (p: string, content: string): CodeFile => ({ path: p, content, lines: countLines(content) });
  const kaggle: CodeFile[] = [
    mk("Cell 0 · 双 T4 部署 Qwen3-32B 推理大模型(vLLM 张量并行,先运行)", llm),
    mk("Cell 1 · 一键启动终端 + AI 引擎 + 自动备份 + 公网网址", cell1),
    mk("Cell 2 · 查看状态 / 双卡利用率 / 停止(可选)", status),
  ];

  return { github, kaggle, generatedAt: Date.now() };
}
