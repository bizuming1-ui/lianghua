"""
反思引擎(Reflector)——让 AI "根据上一步操作复盘、独立思考、自我修正"。

三层机制(都落盘,Kaggle 重启后可恢复):
  1) 结果账本:每个 AI 决策到期后记录真实结果(净收益、最大有利/不利波动、是否扣费后盈利)。
     每笔 AI 平仓也记录(盈亏、平仓原因、持有时长)。
  2) 数值自适应(不依赖大模型,确定性):
       - 置信度校准:把"模型自称的置信度"按分箱映射成真实命中率(带收缩),模型吹牛会被自动打折。
       - 自适应门槛:最近 N 笔 BUY 的扣费后胜率低 -> 提高置信度/预期优势门槛;连亏 -> 冷静期。
  3) 语言反思(大模型):把最近的"决策-理由-结果"喂给模型,让它指出错误模式并写出 ≤3 条可执行教训,
     教训永久保存(最多 12 条),注入之后每一次决策的提示词。
原则:反思只能让系统"更谨慎/更聚焦",不能绕过数学闸门。
"""
import json
import math
import threading
import time

BASE_MIN_CONF = 0.60
BASE_EDGE_MULT = 1.5
MAX_LESSONS = 12


class Reflector:
    def __init__(self):
        self.lock = threading.RLock()
        self.outcomes = []      # 决策到期结果
        self.closes = []        # AI 平仓记录
        self.lessons = []       # [{ts, text, src}]
        self.reflect_count = 0
        self.last_reflect_at = 0.0
        self.settled_since = 0  # 自上次反思以来新增结果数
        self.pause_until = 0.0
        self.last_summary = ""

    # ---------- 记录 ----------
    def record_outcome(self, o):
        """o: {ts, code, action, conf, strategy, p0, p1, ret, net, mfe, mae, horizon_min, thesis, factors, edge_bp, model}"""
        with self.lock:
            self.outcomes.append(o)
            del self.outcomes[:-400]
            self.settled_since += 1

    def record_close(self, c):
        """c: {ts, code, why, pnl, pnl_pct, hold_s, thesis}"""
        with self.lock:
            self.closes.append(c)
            del self.closes[:-200]
            self.settled_since += 1
            tail = [x for x in self.closes[-3:]]
            if len(tail) == 3 and all(x["pnl"] < 0 for x in tail):
                self.pause_until = time.time() + 15 * 60  # 连亏 3 笔:冷静 15 分钟

    # ---------- 数值自适应 ----------
    def _buys(self, n=20):
        return [o for o in self.outcomes if o["action"] == "BUY"][-n:]

    def adapt(self):
        """返回当前自适应门槛。"""
        with self.lock:
            b = self._buys(20)
            n = len(b)
            wr = (sum(1 for o in b if o["net"] > 0) / n) if n else None
            min_conf, edge_mult, note = BASE_MIN_CONF, BASE_EDGE_MULT, "样本不足,使用基础门槛"
            if n >= 8 and wr is not None:
                if wr < 0.40:
                    min_conf, edge_mult, note = 0.75, 2.2, "近%d笔 BUY 扣费后胜率 %.0f%% 偏低 → 收紧(置信≥0.75, 优势≥2.2×成本)" % (n, wr * 100)
                elif wr < 0.48:
                    min_conf, edge_mult, note = 0.68, 1.8, "近%d笔 BUY 扣费后胜率 %.0f%% 一般 → 略收紧" % (n, wr * 100)
                elif wr > 0.60:
                    min_conf, edge_mult, note = 0.58, 1.4, "近%d笔 BUY 扣费后胜率 %.0f%% 良好 → 略放宽" % (n, wr * 100)
                else:
                    note = "近%d笔 BUY 扣费后胜率 %.0f%% → 保持" % (n, wr * 100)
            paused = time.time() < self.pause_until
            return {"min_conf": min_conf, "edge_mult": edge_mult, "note": note, "n": n, "win_rate": wr,
                    "paused": paused, "pause_left_s": max(0, int(self.pause_until - time.time()))}

    def calibrate(self, conf):
        """把模型自称置信度映射为经验命中率(分箱 + 向自称值收缩)。样本不足时返回原值。"""
        with self.lock:
            lo = math.floor(conf * 10) / 10.0
            xs = [o for o in self.outcomes if o["action"] == "BUY" and lo <= o["conf"] < lo + 0.1 + 1e-9]
            k = 8.0
            n = len(xs)
            if n == 0:
                return conf, 0
            hits = sum(1 for o in xs if o["net"] > 0)
            return (hits + k * conf) / (n + k), n

    def calib_table(self):
        with self.lock:
            rows = []
            for lo in (0.5, 0.6, 0.7, 0.8, 0.9):
                xs = [o for o in self.outcomes if o["action"] == "BUY" and lo <= o["conf"] < lo + 0.1 + 1e-9]
                if xs:
                    rows.append({"bin": "%.1f-%.1f" % (lo, lo + 0.1), "n": len(xs), "win": sum(1 for o in xs if o["net"] > 0) / len(xs),
                                 "avg_net_bp": sum(o["net"] for o in xs) / len(xs) * 1e4})
            return rows

    # ---------- 语言反思 ----------
    def need_reflection(self):
        with self.lock:
            return self.settled_since >= 3 and time.time() - self.last_reflect_at > 120

    def reflection_prompt(self):
        with self.lock:
            items = sorted(self.outcomes[-12:] + [dict(x, action="CLOSE") for x in self.closes[-6:]], key=lambda x: x["ts"])[-14:]
            lines = []
            for x in items:
                t = time.strftime("%m-%d %H:%M", time.gmtime(x["ts"] + 8 * 3600))
                if x["action"] == "CLOSE":
                    lines.append("- [%s] %s 平仓(%s) 盈亏¥%+.2f(%+.2f%%) 持有%d分钟;当初理由:%s" % (
                        t, x["code"], x.get("why", ""), x["pnl"], x["pnl_pct"] * 100, x["hold_s"] // 60, (x.get("thesis") or "")[:60]))
                else:
                    lines.append("- [%s] %s 判断%s(置信%.2f,策略%s,持有%d分钟,预期优势%+.1fbp[%s]) 结果:收益%+.1fbp 扣费后%+.1fbp 最大有利%+.1fbp 最大不利%+.1fbp;理由:%s" % (
                        t, x["code"], x["action"], x["conf"], x.get("strategy", ""), x.get("horizon_min", 0), x.get("edge_bp") or 0, x.get("model", ""),
                        x["ret"] * 1e4, x["net"] * 1e4, x.get("mfe", 0) * 1e4, x.get("mae", 0) * 1e4, (x.get("thesis") or "")[:60]))
            ad = self.adapt()
            old = "\n".join("%d. %s" % (i + 1, l["text"]) for i, l in enumerate(self.lessons[-8:])) or "(暂无)"
        return (
            "你是这个量化模拟盘的【复盘官】。下面是 AI 最近的决策与真实结果(已扣除手续费与滑点)。请独立思考:\n"
            "1) 哪些判断对了、为什么;哪些错了、错在数据解读还是风险控制(止损过紧/止盈过远/追涨/逆势/忽视成本)?\n"
            "2) 有没有重复出现的错误模式?(例如:在 RSI 超买时追多、在低波动日试图赚取小于成本的利润)\n"
            "3) 为了下一次决策更准,请写出最多 3 条【具体、可执行、可验证】的教训;不要空话,不要与已有教训重复,可以修订或推翻已有教训。\n\n"
            "【最近决策与结果】\n%s\n\n【系统当前自适应状态】%s\n\n【已有教训】\n%s\n\n"
            "仅输出 JSON:{\"verdict\":\"一句话总评(≤40字)\",\"mistakes\":[\"错误模式…\"],\"lessons\":[\"教训1\",\"教训2\"],\"drop\":[要作废的旧教训序号(整数,可空)]}"
            % ("\n".join(lines) or "(暂无)", ad["note"], old))

    def apply_reflection(self, d, src="LLM"):
        """d: 解析后的 JSON。返回新增教训条数。"""
        if not isinstance(d, dict):
            return 0
        added = 0
        with self.lock:
            drop = set()
            for i in d.get("drop") or []:
                try:
                    drop.add(int(i) - 1)
                except Exception:
                    pass
            base = len(self.lessons) - len(self.lessons[-8:])
            if drop:
                self.lessons = [l for j, l in enumerate(self.lessons) if (j - base) not in drop]
            for tx in (d.get("lessons") or [])[:3]:
                tx = str(tx).strip()[:160]
                if len(tx) < 6 or any(tx == l["text"] for l in self.lessons):
                    continue
                self.lessons.append({"ts": time.time(), "text": tx, "src": src})
                added += 1
            del self.lessons[:-MAX_LESSONS]
            self.last_summary = str(d.get("verdict", ""))[:80]
            self.reflect_count += 1
            self.last_reflect_at = time.time()
            self.settled_since = 0
        return added

    def lessons_text(self):
        with self.lock:
            return "\n".join("%d. %s" % (i + 1, l["text"]) for i, l in enumerate(self.lessons[-8:]))

    # ---------- 展示 / 持久化 ----------
    def state(self):
        with self.lock:
            b = self._buys(40)
            return {"lessons": [dict(l) for l in self.lessons[-8:]], "count": self.reflect_count, "last_at": int(self.last_reflect_at * 1000),
                    "verdict": self.last_summary, "adapt": self.adapt(), "calib": self.calib_table(),
                    "settled": len(self.outcomes), "closes": len(self.closes),
                    "recent_win": (sum(1 for o in b if o["net"] > 0) / len(b)) if b else None, "recent_n": len(b)}

    def export(self):
        with self.lock:
            return {"outcomes": self.outcomes[-400:], "closes": self.closes[-200:], "lessons": self.lessons, "reflect_count": self.reflect_count,
                    "last_summary": self.last_summary}

    def load(self, d):
        if not isinstance(d, dict):
            return
        with self.lock:
            self.outcomes = list(d.get("outcomes") or [])[-400:]
            self.closes = list(d.get("closes") or [])[-200:]
            self.lessons = list(d.get("lessons") or [])[-MAX_LESSONS:]
            self.reflect_count = int(d.get("reflect_count") or 0)
            self.last_summary = str(d.get("last_summary") or "")
