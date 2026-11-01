# ==========================================================================
#  Cell 2(可选):查看运行状态 / 重新打印网址 / 看日志
#  需要关闭服务时,把最后一行的 STOP 改成 True
# ==========================================================================
import os, json, subprocess, urllib.request

ROOT = os.environ.get("QUANT_ROOT", "/kaggle/working/quant")
PORT = int(os.environ.get("QUANT_PORT", "8000"))
STOP = False

try:
    s = json.load(urllib.request.urlopen("http://127.0.0.1:%d/api/state?since=9999999999999" % PORT, timeout=3))
    print("后端运行中 · 行情源:", s["feed"]["name"], "· 延迟", s["feed"]["latency"], "ms")
    for q in s["quotes"]:
        print("  %-7s %-10s %+7.2f%%  %s  [%s]" % (q["code"], q["price"], q["pct"] or 0, q["stateLabel"], q["feed"]))
    a, st = s["account"], s["stats"]
    print("账户权益 ¥%.2f · 现金 ¥%.2f · 成交 %d 笔 · 胜率 %s" % (
        a["equity"], a["cash"], st["trades"], "--" if st["winRate"] is None else "%.1f%%" % st["winRate"]))
except Exception as ex:
    print("后端未运行:", ex, "→ 请先运行 Cell 1")

try:
    print("网址:", open(os.path.join(ROOT, "url.txt")).read())
except Exception:
    print("尚无网址记录")

print("\n--- server.log 末尾 ---")
try:
    print("".join(open(os.path.join(ROOT, "server.log")).readlines()[-8:]))
except Exception:
    pass

if STOP:
    subprocess.run("pkill -f '[q]uant/server.py'; pkill -f '[c]loudflared tunnel'; pkill -f '[l]ocalhost.run'", shell=True)
    print("已停止")
