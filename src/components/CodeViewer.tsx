"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { copyText } from "@/lib/clipboard";
import type { CodeFile, CodePack } from "@/lib/codePack";

type Tab = "github" | "kaggle";

export default function CodeViewer({ pack }: { pack: CodePack }) {
  const [tab, setTab] = useState<Tab>("github");
  const [idx, setIdx] = useState<Record<Tab, number>>({ github: 0, kaggle: 0 });
  const [done, setDone] = useState<Record<string, boolean>>({}); // 已提交到 GitHub 的文件
  const [repo, setRepo] = useState("");
  const [flash, setFlash] = useState("");

  useEffect(() => {
    try {
      setDone(JSON.parse(localStorage.getItem("gh_done") || "{}"));
      setRepo(localStorage.getItem("gh_repo") || "");
    } catch {
      /* ignore */
    }
  }, []);

  const files: CodeFile[] = tab === "github" ? pack.github : pack.kaggle;
  const cur = files[Math.min(idx[tab], files.length - 1)];
  const doneCount = useMemo(() => pack.github.filter((f) => done[f.path]).length, [pack.github, done]);

  const say = (t: string) => {
    setFlash(t);
    setTimeout(() => setFlash(""), 1600);
  };
  const copy = async (text: string, label: string) => say((await copyText(text)) ? `✔ 已复制${label}` : "✘ 复制失败,请手动选中复制");

  const copyAll = () => {
    if (tab === "kaggle") return copy(pack.kaggle.map((f, i) => `# %% ===== ${f.path} =====\n${f.content}`).join("\n\n"), "全部 Kaggle 代码");
    return copy(pack.github.map((f) => `// ===== ${f.path} =====\n${f.content}`).join("\n\n"), `全部 ${pack.github.length} 个文件`);
  };

  const toggleDone = (p: string) => {
    const n = { ...done, [p]: !done[p] };
    setDone(n);
    localStorage.setItem("gh_done", JSON.stringify(n));
  };

  const ghLink = (f: CodeFile) => {
    const r = repo.trim().replace(/^https?:\/\/github\.com\//, "").replace(/\.git$/, "").replace(/\/$/, "");
    if (!r) return "https://github.com/new";
    return done[f.path]
      ? `https://github.com/${r}/edit/main/${f.path}`
      : `https://github.com/${r}/new/main?filename=${encodeURIComponent(f.path)}`;
  };

  return (
    <div className="flex h-screen w-screen flex-col bg-[#070b17] text-[#d7e0f0]">
      {/* 标签 */}
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

      {/* 步骤说明 */}
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
                placeholder="你的用户名/仓库名,例如 zhangsan/biotech-quant"
                className="w-96 rounded-md border border-[#243049] bg-[#0b1226] px-3 py-1 text-sm outline-none focus:border-sky-400"
              />
              <span className="text-sm text-slate-400">已标记提交 {doneCount} / {pack.github.length}</span>
            </div>
            <div>1. 标「新增」的文件:点右侧「在 GitHub 编辑」→ 打开 Create new file 页面(文件名已填好),粘贴代码,Commit。</div>
            <div>2. 提交一个就点一下左侧的「新增」徽标,变成「已提交」(之后再点编辑会走覆盖流程)。也可以直接点「复制本类全部代码」整体保存留档。</div>
            <div>3. 在自己电脑上运行:<code className="rounded bg-[#0b1226] px-1.5">npm install</code> → 配好 .env 的 DATABASE_URL → <code className="rounded bg-[#0b1226] px-1.5">npx drizzle-kit push</code> → <code className="rounded bg-[#0b1226] px-1.5">npm run build &amp;&amp; npm start</code>。</div>
          </>
        ) : (
          <>
            <div>1. 打开 Kaggle → New Notebook → 右侧 Settings → <b className="text-amber-300">Internet 设为 ON</b>(行情和公网网址都需要联网)。</div>
            <div>2. 复制 <b className="text-emerald-400">Cell 1</b>,粘贴到第一个代码单元,点运行(约 20~40 秒)。输出里出现 <code className="rounded bg-[#0b1226] px-1.5">https://xxxx.trycloudflare.com</code> 就是量化终端网址。</div>
            <div>3. 想看状态 / 重新打印网址 / 停止服务,再复制 Cell 2 运行。会话保持开启,网站就一直在线(每 1 秒刷新行情)。</div>
          </>
        )}
      </div>

      {/* 主体 */}
      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[26rem] shrink-0 flex-col gap-3 border-r border-[#16203a] p-3">
          <button onClick={copyAll} className="rounded-xl bg-[#18c47d] py-4 text-lg font-bold text-[#04130c] hover:brightness-110">
            复制本类全部代码
          </button>
          <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto pr-1">
            {files.map((f, i) => {
              const active = i === Math.min(idx[tab], files.length - 1);
              const isDone = tab === "github" && done[f.path];
              return (
                <div
                  key={f.path}
                  onClick={() => setIdx({ ...idx, [tab]: i })}
                  className={`flex cursor-pointer items-center justify-between gap-2 rounded-lg px-3 py-2.5 font-mono text-[13px] ${active ? "bg-[#1b2742]" : "hover:bg-[#101936]"}`}
                >
                  <span className="truncate" title={f.path}>{f.path}</span>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      if (tab === "github") toggleDone(f.path);
                    }}
                    className={`shrink-0 rounded px-2 py-0.5 text-[12px] font-bold ${
                      isDone ? "bg-[#26324d] text-slate-400" : "bg-[#0f3a2a] text-[#3fe59a]"
                    }`}
                    title={tab === "github" ? "点击切换:新增 / 已提交" : ""}
                  >
                    {tab === "github" ? (isDone ? "已提交" : "新增") : "新增"}
                  </button>
                </div>
              );
            })}
          </div>
        </aside>

        <main className="flex min-w-0 flex-1 flex-col">
          {cur && (
            <>
              <div className="flex items-center gap-3 border-b border-[#16203a] bg-[#0a1020] px-5 py-3">
                <span className="truncate font-mono text-lg font-bold">{cur.path}</span>
                <div className="flex-1" />
                <span className="text-sm text-slate-400">{cur.lines} 行</span>
                <button onClick={() => copy(cur.path, "文件名")} className="rounded-lg bg-[#1d2740] px-4 py-2 text-sm font-bold hover:bg-[#26324d]">
                  复制文件名
                </button>
                {tab === "github" && (
                  <a href={ghLink(cur)} target="_blank" rel="noreferrer" className="rounded-lg bg-[#1d2740] px-4 py-2 text-sm font-bold hover:bg-[#26324d]">
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
