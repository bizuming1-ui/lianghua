# ==========================================================================
#  Cell 2(可选):查看运行状态 / 双卡利用率 / AI 决策 / 重新打印网址 / 看日志
#  需要关闭量化服务时,把 STOP 改成 True;STOP_LLM 改成 True 会同时释放两张 GPU
# ==========================================================================
import os, json, subprocess, urllib.request

ROOT = os.environ.get("QUANT_ROOT", "/kaggle/working/quant")
PORT = int(os.environ.get("QUANT_PORT", "8000"))
STOP = False
STOP_LLM = False

try:
    s = json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/state?since=9999999999999" % PORT, timeout=3))
    print("后端运行中 · 行情源:", s["feed"]["name"], "· 延迟", s["feed"]["latency"], "ms")
    for q in s["quotes"]:
        print("  %-7s %-10s %+7.2f%%  %s  [%s]" % (q["code"], q["price"], q["pct"] or 0, q["stateLabel"], q["feed"]))
    a, st = s["account"], s["stats"]
    print("账户权益 ¥%.2f · 现金 ¥%.2f · 成交 %d 笔 · 胜率 %s · 自动模式 %s" % (
        a["equity"], a["cash"], st["trades"], "--" if st["winRate"] is None else "%.1f%%" % st["winRate"], a.get("autoMode")))
except Exception as ex:
    print("后端未运行:", ex, "→ 请先运行 Cell 1")

try:
    ai = json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/ai/state" % PORT, timeout=3))
    print("\n大模型:", "在线 " + ai["llm"]["model"] if ai["llm"]["up"] else "未就绪(先运行 Cell 0)", "· 速度", ai["llm"]["tps"], "tok/s")
    for g in ai["gpus"]:
        print("  GPU%d %s  利用率 %3d%%  显存 %d/%d MiB" % (g["i"], g["name"], g["util"], g["memUsed"], g["memTotal"]))
    for c, d in ai["decisions"].items():
        print("  AI·%-7s %-4s→%-4s 置信 %.2f  %s" % (c, d["action"], d["finalAction"], d["confidence"], d.get("thesis", "") or d.get("error", "")))
    br = ai.get("brain") or {}
    print("  推理占空比(近2分钟) %.0f%% · 当前任务:%s · 每次决策 %s 次自洽投票 · 已循环 %s 轮" % ((br.get("duty") or 0) * 100, br.get("task"), br.get("samples"), br.get("cycles")))
    rf = ai.get("reflect") or {}
    if rf:
        print("  反思 %d 次 · 教训 %d 条 · %s" % (rf.get("count", 0), len(rf.get("lessons", [])), (rf.get("adapt") or {}).get("note", "")))
        for l in rf.get("lessons", [])[-3:]:
            print("    ·", l["text"])
    sc = ai["score"]
    print("  AI 决策复盘:已验证 %d 条;BUY 命中率 %s;平均净收益 %s bp" % (
        sc["n"], "--" if sc["buy_rate"] is None else "%.0f%%" % (sc["buy_rate"] * 100), "--" if sc["avg_net_bp"] is None else "%+.1f" % sc["avg_net_bp"]))
except Exception as ex:
    print("AI 状态不可用:", ex)

try:
    pst = json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/persist" % PORT, timeout=3))
    c = pst["cloud"]
    print("\n数据备份:本地快照", "✔" if pst["local"]["ok"] else "✘", "· 云备份", ("✔ 已启用" + ("" if c["ok"] else " (异常:%s)" % c["err"])) if c["enabled"] else "✘ 未配置 GITHUB_TOKEN(重开 Kaggle 数据会丢)", "· 启动恢复:", (pst.get("restored") or {}).get("detail") or "无")
except Exception as ex:
    print("备份状态不可用:", ex)

try:
    print("\n网址:", open(os.path.join(ROOT, "url.txt")).read())
except Exception:
    print("\n尚无网址记录")

print("\n--- server.log 末尾 ---")
try:
    print("".join(open(os.path.join(ROOT, "server.log")).readlines()[-8:]))
except Exception:
    pass
print("--- vllm.log 末尾 ---")
try:
    print("".join(open(os.path.join(ROOT, "vllm.log"), errors="ignore").readlines()[-5:]))
except Exception:
    pass

if STOP:
    subprocess.run("pkill -f '[q]uant/server.py'; pkill -f '[c]loudflared tunnel'; pkill -f '[l]ocalhost.run'", shell=True)
    print("量化服务已停止")
if STOP_LLM:
    subprocess.run("pkill -f 'vllm.entrypoints'", shell=True)
    print("大模型已停止,GPU 已释放")
