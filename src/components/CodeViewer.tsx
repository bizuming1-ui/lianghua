"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { copyText } from "@/lib/clipboard";
import type { CodeFile, CodePack } from "@/lib/codePack";

type Tab = "github" | "kaggle";
type Status = "new" | "changed" | "same" | "unknown";

const DEFAULT_REPO = "bizuming1-ui/lianghua";
const norm = (s: string) => s.replace(/\r\n/g, "\n").replace(/\s+$/g, "");

export default function CodeViewer({ pack }: { pack: CodePack }) {
  const [tab, setTab] = useState<Tab>("github");
  const [idx, setIdx] = useState<Record<Tab, number>>({ github: 0, kaggle: 0 });
  const [repo, setRepo] = useState(DEFAULT_REPO);
  const [flash, setFlash] = useState("");
  const [status, setStatus] = useState<Record<string, Status>>({});
  const [checking, setChecking] = useState(false);
  const [onlyTodo, setOnlyTodo] = useState(false);

  const repoPath = useMemo(
    () => repo.trim().replace(/^https?:\/\/github\.com\//, "").replace(/\.git$/, "").replace(/\/$/, "") || DEFAULT_REPO,
    [repo],
  );

  useEffect(() => {
    try {
      const r = localStorage.getItem("gh_repo");
      if (r) setRepo(r);
    } catch {
      /* ignore */
    }
  }, []);

  // 对比 GitHub 上的现有文件(raw 内容),标出「新增 / 有修改 / 已同步」
  const check = useCallback(async () => {
    setChecking(true);
    const out: Record<string, Status> = {};
    await Promise.all(
      pack.github.map(async (f) => {
        try {
          const r = await fetch(`https://raw.githubusercontent.com/${repoPath}/main/${f.path}?_=${Date.now()}`, { cache: "no-store" });
          if (r.status === 404) out[f.path] = "new";
          else if (!r.ok) out[f.path] = "unknown";
          else out[f.path] = norm(await r.text()) === norm(f.content) ? "same" : "changed";
        } catch {
          out[f.path] = "unknown";
        }
      }),
    );
    setStatus(out);
    setChecking(false);
  }, [pack.github, repoPath]);

  useEffect(() => {
    void check();
  }, [check]);

  const all: CodeFile[] = tab === "github" ? pack.github : pack.kaggle;
  const files = tab === "github" && onlyTodo ? all.filter((f) => status[f.path] !== "same") : all;
  const cur = files[Math.min(idx[tab], files.length - 1)];
  const todo = pack.github.filter((f) => status[f.path] === "new" || status[f.path] === "changed").length;

  const say = (t: string) => {
    setFlash(t);
    setTimeout(() => setFlash(""), 1800);
  };
  const copy = async (text: string, label: string) => say((await copyText(text)) ? `✔ 已复制${label}` : "✘ 复制失败,请手动选中复制");

  const copyAll = () => {
    if (tab === "kaggle") return copy(pack.kaggle.map((f) => `# %% ===== ${f.path} =====\n${f.content}`).join("\n\n"), "全部 Kaggle 代码");
    return copy(pack.github.map((f) => `// ===== ${f.path} =====\n${f.content}`).join("\n\n"), `全部 ${pack.github.length} 个文件`);
  };

  const ghLink = (f: CodeFile) =>
    status[f.path] === "new" || status[f.path] === undefined
      ? `https://github.com/${repoPath}/new/main?filename=${encodeURIComponent(f.path)}`
      : `https://github.com/${repoPath}/edit/main/${f.path}`;

  const badge = (f: CodeFile) => {
    const s = status[f.path];
    if (checking && !s) return ["检测中", "bg-[#26324d] text-slate-400"] as const;
    if (s === "new") return ["新增", "bg-[#0f3a2a] text-[#3fe59a]"] as const;
    if (s === "changed") return ["有修改", "bg-[#3a2c0f] text-[#f5b942]"] as const;
    if (s === "same") return ["已同步", "bg-[#26324d] text-slate-400"] as const;
    return ["未知", "bg-[#26324d] text-slate-400"] as const;
  };

  return (
    <div className="flex h-screen w-screen flex-col bg-[#070b17] text-[#d7e0f0]">
      <div className="flex items-end gap-3 bg-[#03060f] px-4 pt-3">
        {(
          [
            ["github", `① 更新到 GitHub 的代码(${pack.github.length})`],
            ["kaggle", `② 复制到 Kaggle 的代码(${pack.kaggle.length})`],
          ] as [Tab, string][]
        ).map(([k, label]) => (
          <button
            key={k}
            onClick={() => setTab(k)}
            className={`rounded-t-xl px-6 py-3 text-lg font-bold ${tab === k ? "bg-[#070b17] text-white" : "bg-[#0c1224] text-slate-400 hover:text-slate-200"}`}
          >
            {label}
          </button>
        ))}
        <div className="flex-1" />
        <Link href="/" className="mb-2 rounded-lg border border-[#243049] px-4 py-2 text-sm hover:border-sky-400">
          ← 返回浏览器模拟器
        </Link>
      </div>

      <div className="border-b border-[#16203a] bg-[#070b17] px-6 py-3 text-[15px] leading-7 text-slate-300">
        {tab === "github" ? (
          <>
            <div className="flex flex-wrap items-center gap-3">
              <span>GitHub 仓库:</span>
              <input
                value={repo}
                onChange={(e) => {
                  setRepo(e.target.value);
                  localStorage.setItem("gh_repo", e.target.value);
                }}
                className="w-80 rounded-md border border-[#243049] bg-[#0b1226] px-3 py-1 text-sm outline-none focus:border-sky-400"
              />
              <button onClick={() => void check()} className="rounded-md border border-[#243049] px-3 py-1 text-sm hover:border-sky-400">
                {checking ? "对比中…" : "重新对比 GitHub"}
              </button>
              <label className="flex items-center gap-1.5 text-sm">
                <input type="checkbox" checked={onlyTodo} onChange={(e) => setOnlyTodo(e.target.checked)} />
                只看需要更新的
              </label>
              <span className="text-sm text-amber-300">需要更新/新增 {todo} 个文件(已自动与你的仓库逐文件对比)</span>
            </div>
            <div>
              1. 点右侧「在 GitHub 编辑」:<b className="text-emerald-300">会先自动复制该文件全部内容</b>并打开 GitHub 编辑页(新增文件会打开 Create new file 且文件名已填好)。
            </div>
            <div>2. 在 GitHub 页面里全选原内容(Ctrl+A)→ 粘贴(Ctrl+V)→ 点右上角 Commit changes。提交后回来点「重新对比 GitHub」,徽标会变成「已同步」。</div>
          </>
        ) : (
          <>
            <div>1. 打开 Kaggle → New Notebook → 右侧 Settings:<b className="text-amber-300">Accelerator 选 GPU T4 ×2</b>,<b className="text-amber-300">Internet 设为 ON</b>。</div>
            <div>
              2. 先复制 <b className="text-fuchsia-400">Cell 0</b> 粘贴运行(首次约 8~20 分钟:安装 vLLM、下载 Qwen3-14B-AWQ、加载到两张卡),结尾出现 <code className="rounded bg-[#0b1226] px-1.5">✅ 两张 T4 同时工作</code> 即成功。
            </div>
            <div>
              3. 再新建一个单元,复制 <b className="text-emerald-400">Cell 1</b> 运行,输出 <code className="rounded bg-[#0b1226] px-1.5">https://xxxx.trycloudflare.com</code> 就是量化终端网址;页面右侧 AI 面板点「AI」即启用。Cell 2 可随时查看双卡利用率。
            </div>
          </>
        )}
      </div>

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[27rem] shrink-0 flex-col gap-3 border-r border-[#16203a] p-3">
          <button onClick={copyAll} className="rounded-xl bg-[#18c47d] py-4 text-lg font-bold text-[#04130c] hover:brightness-110">
            复制本类全部代码
          </button>
          <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto pr-1">
            {files.map((f, i) => {
              const active = i === Math.min(idx[tab], files.length - 1);
              const [bt, bc] = tab === "github" ? badge(f) : (["Cell", "bg-[#0f3a2a] text-[#3fe59a]"] as const);
              return (
                <div
                  key={f.path}
                  onClick={() => setIdx({ ...idx, [tab]: i })}
                  className={`flex cursor-pointer items-center justify-between gap-2 rounded-lg px-3 py-2.5 font-mono text-[13px] ${active ? "bg-[#1b2742]" : "hover:bg-[#101936]"}`}
                >
                  <span className="truncate" title={f.path}>{f.path}</span>
                  <span className={`shrink-0 rounded px-2 py-0.5 text-[12px] font-bold ${bc}`}>{bt}</span>
                </div>
              );
            })}
            {files.length === 0 && <div className="p-4 text-sm text-slate-400">所有文件都已与 GitHub 同步 🎉</div>}
          </div>
        </aside>

        <main className="flex min-w-0 flex-1 flex-col">
          {cur && (
            <>
              <div className="flex items-center gap-3 border-b border-[#16203a] bg-[#0a1020] px-5 py-3">
                <span className="truncate font-mono text-lg font-bold">{cur.path}</span>
                <div className="flex-1" />
                <span className="text-sm text-slate-400">{cur.lines} 行</span>
                {tab === "github" && (
                  <button onClick={() => copy(cur.path, "文件名")} className="rounded-lg bg-[#1d2740] px-4 py-2 text-sm font-bold hover:bg-[#26324d]">
                    复制文件名
                  </button>
                )}
                {tab === "github" && (
                  <a
                    href={ghLink(cur)}
                    target="_blank"
                    rel="noreferrer"
                    onClick={() => void copy(cur.content, "此文件并打开 GitHub")}
                    className="rounded-lg bg-[#1d2740] px-4 py-2 text-sm font-bold hover:bg-[#26324d]"
                  >
                    在 GitHub 编辑 ↗
                  </a>
                )}
                <button onClick={() => copy(cur.content, "此文件")} className="rounded-lg bg-[#0a84ff] px-5 py-2 text-sm font-bold text-white hover:brightness-110">
                  复制此文件
                </button>
              </div>
              <pre className="min-h-0 flex-1 overflow-auto whitespace-pre p-5 font-mono text-[13px] leading-6 text-[#cfe0ff]">{cur.content}</pre>
            </>
          )}
        </main>
      </div>

      {flash && (
        <div className="fixed bottom-8 left-1/2 -translate-x-1/2 rounded-full border border-emerald-400/40 bg-[#07111b] px-6 py-3 text-base font-bold shadow-2xl">{flash}</div>
      )}
    </div>
  );
}
