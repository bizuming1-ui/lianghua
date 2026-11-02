"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { copyText } from "@/lib/clipboard";
import type { CodePack } from "@/lib/codePack";

/** 浏览器模拟器:地址栏 + 网址 + 打开网站 + 进入复制代码界面 + 右上角"复制代码"。 */
export default function Simulator() {
  const [origin, setOrigin] = useState("");
  const [menu, setMenu] = useState(false);
  const [toast, setToast] = useState("");
  const [nonce, setNonce] = useState(0);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => setOrigin(window.location.origin), []);
  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenu(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const url = `${origin}/site`;
  const say = (t: string) => {
    setToast(t);
    setTimeout(() => setToast(""), 2200);
  };

  async function copyFromPack(kind: "kaggle0" | "kaggle1" | "github") {
    setMenu(false);
    say("正在读取代码…");
    try {
      const pack = (await (await fetch("/api/code", { cache: "no-store" })).json()) as CodePack;
      let text: string;
      if (kind === "kaggle0") text = pack.kaggle[0].content;
      else if (kind === "kaggle1") text = pack.kaggle[1].content;
      else text = pack.github.map((f) => `// ===== ${f.path} =====\n${f.content}`).join("\n\n");
      const ok = await copyText(text);
      say(ok ? (kind === "kaggle0" ? "✔ 已复制 Kaggle Cell 0(双 T4 大模型,先运行)" : kind === "kaggle1" ? "✔ 已复制 Kaggle Cell 1(终端 + AI,第二个运行)" : `✔ 已复制 GitHub 全部代码(${pack.github.length} 个文件)`) : "✘ 复制失败,请进入复制代码界面手动复制");
    } catch {
      say("✘ 读取代码失败");
    }
  }

  return (
    <div className="flex h-screen w-screen flex-col bg-black text-slate-200">
      {/* 标签栏 */}
      <div className="flex items-end gap-2 bg-[#05080d] px-3 pt-2">
        <div className="mr-2 flex items-center gap-1.5 pb-2">
          <i className="h-3 w-3 rounded-full bg-[#ff5f57]" />
          <i className="h-3 w-3 rounded-full bg-[#febc2e]" />
          <i className="h-3 w-3 rounded-full bg-[#28c840]" />
        </div>
        <div className="flex items-center gap-2 rounded-t-lg border border-b-0 border-[#1b2738] bg-[#0b121b] px-4 py-2 text-sm">
          <span className="h-2 w-2 rounded-full bg-emerald-400 shadow-[0_0_8px_#34d399]" />
          生物科技ETF 量化终端
        </div>
        <div className="flex-1" />
        {/* 右上角:复制代码 */}
        <div className="relative pb-1.5" ref={menuRef}>
          <button
            onClick={() => setMenu((v) => !v)}
            className="rounded-lg bg-gradient-to-r from-sky-500 to-blue-600 px-4 py-1.5 text-sm font-bold text-white shadow-[0_0_18px_rgba(14,165,233,.45)] hover:brightness-110"
          >
            📋 复制代码 ▾
          </button>
          {menu && (
            <div className="absolute right-0 top-full z-20 mt-1 w-72 overflow-hidden rounded-xl border border-[#1b2738] bg-[#0b121b] shadow-2xl">
              <button onClick={() => copyFromPack("kaggle0")} className="block w-full px-4 py-3 text-left text-sm hover:bg-[#12202f]">
                <b className="text-fuchsia-400">① 复制 Kaggle Cell 0 · 双 T4 大模型</b>
                <span className="block text-xs text-slate-400">先运行:Qwen3-32B · vLLM 张量并行,两张卡同时推理</span>
              </button>
              <button onClick={() => copyFromPack("kaggle1")} className="block w-full border-t border-[#1b2738] px-4 py-3 text-left text-sm hover:bg-[#12202f]">
                <b className="text-emerald-400">② 复制 Kaggle Cell 1 · 终端 + AI + 自动备份</b>
                <span className="block text-xs text-slate-400">再运行:行情 + 模拟交易 + AI 决策 + 公网网址</span>
              </button>
              <button onClick={() => copyFromPack("github")} className="block w-full border-t border-[#1b2738] px-4 py-3 text-left text-sm hover:bg-[#12202f]">
                <b className="text-sky-400">一键复制 GitHub 全部代码</b>
                <span className="block text-xs text-slate-400">所有文件按路径分段拼接</span>
              </button>
              <Link href="/code" className="block border-t border-[#1b2738] px-4 py-3 text-sm hover:bg-[#12202f]">
                <b className="text-amber-300">进入复制代码界面 →</b>
                <span className="block text-xs text-slate-400">按文件逐个复制 / 在 GitHub 编辑</span>
              </Link>
            </div>
          )}
        </div>
      </div>

      {/* 工具栏 / 地址栏 */}
      <div className="flex items-center gap-2 border-y border-[#1b2738] bg-[#0b121b] px-3 py-2">
        <button className="rounded-md px-2 py-1 text-slate-500" disabled>←</button>
        <button className="rounded-md px-2 py-1 text-slate-500" disabled>→</button>
        <button onClick={() => setNonce((n) => n + 1)} className="rounded-md px-2 py-1 text-lg hover:bg-[#12202f]" title="刷新">⟳</button>
        <div className="flex flex-1 items-center gap-2 rounded-full border border-[#1b2738] bg-black px-4 py-1.5 font-mono text-sm">
          <span className="text-emerald-400">🔒</span>
          <input readOnly value={url} onFocus={(e) => e.currentTarget.select()} className="w-full bg-transparent text-sky-200 outline-none" />
        </div>
        <button
          onClick={async () => say((await copyText(url)) ? "✔ 网址已复制" : "✘ 复制失败")}
          className="rounded-md border border-[#1b2738] px-3 py-1.5 text-sm hover:border-sky-400"
        >
          复制网址
        </button>
        <a href="/site" target="_blank" rel="noreferrer" className="rounded-md border border-[#1b2738] px-3 py-1.5 text-sm hover:border-sky-400">
          打开网站 ↗
        </a>
        <Link href="/code" className="rounded-md bg-emerald-500 px-4 py-1.5 text-sm font-bold text-black hover:brightness-110">
          进入复制代码界面
        </Link>
      </div>

      {/* 网站视口 */}
      <div className="relative min-h-0 flex-1 bg-black">
        <iframe key={nonce} src="/site" title="量化终端" className="h-full w-full border-0 bg-black" />
        {toast && (
          <div className="absolute bottom-6 left-1/2 -translate-x-1/2 rounded-full border border-sky-500/40 bg-[#07111b] px-5 py-2 text-sm shadow-xl">{toast}</div>
        )}
      </div>
    </div>
  );
}
