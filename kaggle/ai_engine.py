"""
AI 推理决策引擎(Kaggle T4×2 上的 vLLM 大模型 + 量化数学闸门)

数据流(全部真实数据,绝不编造):
  Yahoo Finance(美股/宏观 K 线、新闻标题) + 东方财富(159502 K 线) + 新浪/腾讯(秒级实时价,由 server.py 提供)
    -> quant_math:波动率 / 状态识别 / 岭回归 + 样本外检验 / 跨市场隔夜模型
    -> 提示词 -> vLLM(Qwen3 思考模式,双卡张量并行)-> JSON 决策
    -> 数学闸门(样本外必须有统计优势、预期净收益 > 成本×1.5、置信度、风控、冷却)-> 下单
原则:大模型只能"否决/缩小",不能越过数学闸门"无中生有"。
"""
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import quant_math as qm

LLM_URL = os.environ.get("QUANT_LLM_URL", "http://127.0.0.1:8001/v1")
LLM_NAME = os.environ.get("QUANT_LLM_NAME", "quant-llm")
AI_INTERVAL = float(os.environ.get("QUANT_AI_INTERVAL", "90"))
AI_CLOSED_INTERVAL = 600.0
MAX_TOKENS = int(os.environ.get("QUANT_AI_MAX_TOKENS", "3500"))
THINKING = os.environ.get("QUANT_AI_THINKING", "1") != "0"

CN_TZ = timezone(timedelta(hours=8))
try:
    from zoneinfo import ZoneInfo
    US_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    US_TZ = timezone(timedelta(hours=-5))
TZ = {"US": US_TZ, "CN": CN_TZ}

DATA_MAP = {"IBB": {"yahoo": "IBB", "em": "105.IBB"}, "XBI": {"yahoo": "XBI", "em": "107.XBI"}, "159502": {"em": "0.159502"}}
TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1d": 86400}
YH = {"1m": ("1m", "5d"), "5m": ("5m", "60d"), "15m": ("15m", "60d"), "1d": ("1d", "2y")}
EM = {"1m": (1, 2400), "5m": (5, 3000), "15m": (15, 2000), "1d": (101, 600)}
TTL = {"1m": 15, "5m": 45, "15m": 90, "1d": 900}
CTX = {"SPY": ("SPY", "107.SPY"), "QQQ": ("QQQ", "105.QQQ"), "VIX": ("^VIX", None), "美债10Y收益率": ("^TNX", "171.US10Y"),
       "美元指数": ("DX-Y.NYB", None), "XLV医疗": ("XLV", "107.XLV"), "USDCNY": ("CNY=X", "133.USDCNH")}
SLIP = {"US": 0.0002, "CN": 0.0005}  # 单边滑点假设(仅用于闸门的保守成本估计)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def log(*a):
    print("[ai]", *a, file=sys.stderr, flush=True)


def http_json(url, timeout=8, headers=None, retries=2):
    err = None
    for i in range(retries + 1):
        try:
            h = dict(UA)
            h.update(headers or {})
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as ex:
            err = ex
            time.sleep(0.4 * (i + 1))
    raise err


# ----------------------------- K 线数据 -----------------------------
_yh_block = 0.0


def yahoo_bars(sym, interval, rng):
    global _yh_block
    if time.time() < _yh_block:
        raise RuntimeError("yahoo 冷却中(近期被限流)")
    j, err = None, None
    for host in ("query2", "query1"):  # 429 时换主机重试
        try:
            j = http_json("https://%s.finance.yahoo.com/v8/finance/chart/%s?interval=%s&range=%s&includePrePost=false" % (
                host, urllib.parse.quote(sym), interval, rng), retries=0)
            break
        except Exception as ex:
            err = ex
            time.sleep(1.0)
    if j is None:
        _yh_block = time.time() + 120
        raise err
    res = j["chart"]["result"][0]
    ts, q = res.get("timestamp") or [], res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(ts):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append([int(t) * 1000, float(o), float(h), float(l), float(c), float(q["volume"][i] or 0)])
    return out


def em_bars(secid, klt, lmt):
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s&klt=%d&fqt=1&lmt=%d&end=20500101"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57") % (secid, klt, lmt)
    kl = http_json(url)["data"]["klines"]
    out = []
    for line in kl:
        p = line.split(",")
        s = p[0]
        if len(s) > 10:  # 分钟线:东方财富时间为"K 线结束时刻",换算成开始时刻
            d = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=CN_TZ)
            t = d.timestamp() * 1000 - klt * 60000
        else:
            # 日线:取标注日期的 12:00 UTC,这样无论按北京还是纽约时区换算,日期都与标注一致
            t = (datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=12)).timestamp() * 1000
        out.append([int(t), float(p[1]), float(p[3]), float(p[4]), float(p[2]), float(p[5])])
    return out


class Data:
    def __init__(self):
        self.cache = {}
        self.locks = {}
        self.meta = {}  # 数据源状态
        self.glock = threading.Lock()

    def _lock(self, key):
        with self.glock:
            return self.locks.setdefault(key, threading.Lock())

    def bars(self, code, tf, force=False):
        key = (code, tf)
        c = self.cache.get(key)
        if c and not force and time.time() - c[0] < TTL[tf]:
            return c[1]
        with self._lock(key):
            c = self.cache.get(key)
            if c and not force and time.time() - c[0] < TTL[tf]:
                return c[1]
            try:
                m = DATA_MAP[code]
                b, src = [], ""
                if "yahoo" in m:
                    try:
                        b, src = yahoo_bars(m["yahoo"], *YH[tf]), "yahoo"
                    except Exception as ex:
                        log("yahoo 失败,改用东方财富:", code, tf, ex)
                if not b and "em" in m:
                    b, src = em_bars(m["em"], *EM[tf]), "eastmoney"
                if not b:
                    raise ValueError("empty")
                self.cache[key] = (time.time(), b)
                self.meta[key] = {"src": src, "ok": True, "n": len(b), "at": int(time.time() * 1000), "err": ""}
                return b
            except Exception as ex:
                self.meta[key] = dict(self.meta.get(key) or {}, ok=False, err=str(ex)[:120])
                return c[1] if c else []

    def raw(self, sym, interval, rng, ttl, em=None):
        key = ("raw", sym, interval, rng)
        c = self.cache.get(key)
        if c and time.time() - c[0] < ttl:
            return c[1]
        with self._lock(key):
            try:
                try:
                    b = yahoo_bars(sym, interval, rng)
                except Exception:
                    if not em:
                        raise
                    b = em_bars(em, 101, 150)
                self.cache[key] = (time.time(), b)
                return b
            except Exception:
                return c[1] if c else []


def news_titles(sym):
    try:
        j = http_json("https://query2.finance.yahoo.com/v1/finance/search?q=%s&quotesCount=0&newsCount=6" % urllib.parse.quote(sym))
        out = []
        for n in j.get("news", [])[:6]:
            ts = n.get("providerPublishTime")
            out.append({"title": n.get("title", ""), "pub": n.get("publisher", ""), "ts": int(ts) * 1000 if ts else 0})
        return out
    except Exception:
        return []


# ----------------------------- 时段 -----------------------------
def session_info(market, now=None):
    """返回 (距开盘分钟, 距收盘分钟) ;非交易中返回 None。"""
    d = datetime.fromtimestamp(now or time.time(), TZ[market])
    if d.weekday() >= 5:
        return None
    t = d.hour * 60 + d.minute
    if market == "US":
        return (t - 570, 960 - t) if 570 <= t < 960 else None
    if 570 <= t < 690:
        return (t - 570, (690 - t) + 120)
    if 780 <= t < 900:
        return (t - 570 - 90, 900 - t)
    return None


def prev_weekday(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def next_weekday(d):
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def f(v, d=2, sign=False):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "--"
    return (("%+." + str(d) + "f") % v) if sign else (("%." + str(d) + "f") % v)


def fmt_bj(ms):
    return datetime.fromtimestamp(ms / 1000, CN_TZ).strftime("%m-%d %H:%M:%S")


# ----------------------------- JSON 解析 -----------------------------
def extract_json(s):
    i = s.find("{")
    while i != -1:
        depth, instr, esc = 0, False, False
        for j in range(i, len(s)):
            ch = s[j]
            if instr:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
            elif ch == '"':
                instr = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[i:j + 1])
                    except Exception:
                        break
        i = s.find("{", i + 1)
    return None


def split_think(full):
    if "</think>" in full:
        a, b = full.split("</think>", 1)
        return a.replace("<think>", "").strip(), b.strip()
    return full.replace("<think>", "").strip(), ""


def clamp(v, lo, hi, default):
    try:
        v = float(v)
        if math.isnan(v):
            return default
        return max(lo, min(hi, v))
    except Exception:
        return default


# ----------------------------- GPU / LLM 状态 -----------------------------
_gpu_cache = {"at": 0, "v": []}


def gpu_stats():
    if time.time() - _gpu_cache["at"] < 1.2:
        return _gpu_cache["v"]
    v = []
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3).stdout
        for line in out.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            v.append({"i": int(p[0]), "name": p[1], "util": float(p[2]), "memUsed": float(p[3]), "memTotal": float(p[4])})
    except Exception:
        v = []
    _gpu_cache.update(at=time.time(), v=v)
    return v


_llm_cache = {"at": 0, "up": False, "model": ""}


def llm_health():
    if time.time() - _llm_cache["at"] < 4:
        return _llm_cache
    up, model = False, ""
    try:
        j = http_json(LLM_URL + "/models", timeout=2, retries=0)
        up, model = True, (j.get("data") or [{}])[0].get("id", "")
    except Exception:
        pass
    _llm_cache.update(at=time.time(), up=up, model=model)
    return _llm_cache


def llm_stream(messages, max_tokens, thinking, on_text=None, timeout=900):
    body = {"model": LLM_NAME, "messages": messages, "max_tokens": max_tokens, "temperature": 0.5, "top_p": 0.9,
            "top_k": 20, "stream": True, "chat_template_kwargs": {"enable_thinking": bool(thinking)}}
    req = urllib.request.Request(LLM_URL + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    text, reasoning, finish, ntok = "", "", None, 0
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d == "[DONE]":
                break
            j = json.loads(d)
            ch = (j.get("choices") or [{}])[0]
            delta = ch.get("delta") or {}
            rc = delta.get("reasoning_content") or delta.get("reasoning") or ""
            if rc:
                reasoning += rc
            piece = delta.get("content") or ""
            text += piece
            if piece or rc:
                ntok += 1
                if on_text and ntok % 4 == 0:
                    on_text(("<think>" + reasoning + "</think>" if reasoning and not text else "") + text, ntok)
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    full = ("<think>" + reasoning + "</think>" if reasoning else "") + text
    return full, finish, ntok


SYSTEM_PROMPT = (
    "你是一名严谨的量化基金经理兼金融数学研究员,正在为一个【模拟盘】做下一步操作决策。"
    "标的:IBB、XBI(美股生物科技ETF)与159502(嘉实标普生物科技ETF,跟踪标普生物科技精选行业指数,与XBI高度同源)。\n"
    "硬性规则:\n"
    "1) 只能使用用户消息里给出的数据与数字;缺失就写缺失,严禁编造价格、新闻或统计量。\n"
    "2) 只做多或空仓(BUY 开多 / SELL 平掉已有多仓 / HOLD 观望)。没有统计优势时必须 HOLD,宁可错过也不要凑交易。\n"
    "3) 必须先检验:样本外是否有优势(IC、命中率及其置信下界)、预期收益是否显著大于往返成本、市场状态(方差比)是否支持该类策略、"
    "宏观与大盘(VIX、美债、美元、SPY/QQQ/XLV)是否逆风、消息面是否有重大风险,再给结论。\n"
    "4) 思考要精炼(尽量不超过 700 字),最后只输出一个 JSON 对象,不要输出其他文字。"
)

OUTPUT_SPEC = (
    "【输出格式】仅输出 JSON:\n"
    '{"action":"BUY|SELL|HOLD","strategy":"intraday|swing|none","confidence":0到1之间的小数,'
    '"horizon_min":持有分钟数(整数,5~1440),"stop_pct":止损幅度(%,如0.6),"target_pct":止盈幅度(%,如1.2),'
    '"thesis":"不超过80字的中文结论","factors":[{"factor":"因素","direction":"bull|bear|neutral","weight":0到1}],'
    '"risks":["最多3条风险"]}\n'
    "说明:intraday=日内(收盘前平仓,依据5分钟模型);swing=隔夜/多日(依据日线模型或159502的隔夜联动模型)。"
    "confidence 是你认为该方向在 horizon 内、扣除成本后仍盈利的概率。"
)


class Engine:
    def __init__(self, host):
        self.h = host
        self.data = Data()
        self.symbols = host["symbols"]
        self.lock = threading.RLock()
        self.studies = {}
        self.ctx = {"at": 0, "v": {}}
        self.news = {}
        self.decisions = {}
        self.history = []
        self.live = {}
        self.plans = {}
        self.pending = []
        self.score = {"n": 0, "buy_hit": 0, "buy_n": 0, "sum_ret": 0.0, "sell_n": 0, "sell_hit": 0}
        self.last_view = 0.0
        self.force = set()
        self.last_trade = {}
        self.tps = 0.0
        self.last_err = ""
        self.wake = threading.Event()
        self.save_path = os.path.join(os.environ.get("QUANT_SAVE_DIR", "/kaggle/working" if os.path.isdir("/kaggle/working") else "."), "ai_decisions.jsonl")

    # ---------- 基础 ----------
    def sym(self, code):
        return next(s for s in self.symbols if s["code"] == code)

    def cost_rt(self, code):
        m = self.sym(code)["market"]
        return 2.0 * (self.h["fee_rate"] + SLIP[m])

    def start(self):
        threading.Thread(target=self._bg_loop, daemon=True).start()
        threading.Thread(target=self._analysis_loop, daemon=True).start()

    # ---------- 后台:数据与统计检验 ----------
    def _bg_loop(self):
        while True:
            try:
                for s in self.symbols:
                    self.refresh_study(s["code"])
                if time.time() - self.ctx["at"] > 600:
                    self.refresh_ctx()
                for s in self.symbols:
                    if s["market"] == "US" and time.time() - self.news.get(s["code"], {}).get("at", 0) > 900:
                        self.news[s["code"]] = {"at": time.time(), "v": news_titles(s["code"])}
            except Exception as ex:
                self.last_err = "bg: %s" % ex
                log("bg error", ex)
            time.sleep(30)

    def refresh_ctx(self):
        v = {}
        for name, (ysym, esym) in CTX.items():
            time.sleep(0.4)
            b = self.data.raw(ysym, "1d", "6mo", 300, esym)
            if len(b) >= 25:
                c = [x[4] for x in b]
                last = c[-1]
                z = None
                w = c[-60:]
                mu = sum(w) / len(w)
                sd = math.sqrt(sum((x - mu) ** 2 for x in w) / len(w))
                if sd > 0:
                    z = (last - mu) / sd
                v[name] = {"last": last, "r1": last / c[-2] - 1, "r5": last / c[-6] - 1, "r20": last / c[-21] - 1, "z60": z,
                           "date": datetime.fromtimestamp(b[-1][0] / 1000, timezone.utc).strftime("%m-%d")}
        self.ctx = {"at": time.time(), "v": v}

    def refresh_study(self, code):
        b5, b1d = self.data.bars(code, "5m"), self.data.bars(code, "1d")
        key = (b5[-1][0] if b5 else 0, b1d[-1][0] if b1d else 0, len(b5), len(b1d))
        st = self.studies.get(code)
        if st and st["key"] == key and time.time() - st["at"] < 180:
            return st
        m = self.sym(code)["market"]
        cost = self.cost_rt(code)
        res = {"key": key, "at": time.time(), "intra": None, "daily": None, "gap": None, "regime": {}, "corr": None}
        try:
            res["intra"] = qm.edge_study(b5, "intra", 6, cost, "5分钟K线") if len(b5) else None
            res["daily"] = qm.edge_study(b1d, "daily", 1, cost, "日K线") if len(b1d) else None
            if len(b5) > 200:
                c5 = [x[4] for x in b5[-1500:]]
                vr = qm.variance_ratio(c5, 6)
                res["regime"]["vr5m"] = vr
                res["regime"]["label5m"] = qm.regime_label(vr)
                res["regime"]["trend5m"] = qm.trend_tstat(c5, 60)
                res["regime"]["atr5m_pct"] = qm.atr_last([x[2] for x in b5], [x[3] for x in b5], [x[4] for x in b5]) / b5[-1][4] * 100
            if len(b1d) > 60:
                res["regime"]["atr1d_pct"] = qm.atr_last([x[2] for x in b1d], [x[3] for x in b1d], [x[4] for x in b1d]) / b1d[-1][4] * 100
                c = [x[4] for x in b1d]
                res["regime"]["rng20"] = (c[-1] - min(c[-20:])) / max(1e-9, max(c[-20:]) - min(c[-20:]))
                res["regime"]["vr1d"] = qm.variance_ratio(c, 5)
            if code == "159502":
                res["gap"] = self.gap_model(b1d)
            if code in ("IBB", "XBI"):
                other = "XBI" if code == "IBB" else "IBB"
                a = [(x[0], x[4]) for x in self.data.bars(code, "5m")[-400:]]
                o = [(x[0], x[4]) for x in self.data.bars(other, "5m")[-400:]]
                cr, n = qm.corr_aligned(a, o)
                res["corr"] = {"vs": other, "corr": cr, "n": n}
        except Exception as ex:
            res["err"] = str(ex)
            log("study error", code, ex)
        self.studies[code] = res
        return res

    def gap_model(self, cn_daily):
        """隔夜联动:159502 当日收益 ~ α + β·(前一美股交易日 XBI 收益)。"""
        xb = self.data.bars("XBI", "1d")
        if len(xb) < 80 or len(cn_daily) < 80:
            return None
        us = {}
        for i in range(1, len(xb)):
            d = datetime.fromtimestamp(xb[i][0] / 1000, US_TZ).date()
            us[d] = xb[i][4] / xb[i - 1][4] - 1
        us_dates = sorted(us)
        xs, ys = [], []
        for i in range(1, len(cn_daily)):
            D = datetime.fromtimestamp(cn_daily[i][0] / 1000, CN_TZ).date()
            prior = [d for d in us_dates[-400:] if d < D]
            if not prior:
                continue
            xs.append(us[prior[-1]])
            ys.append(cn_daily[i][4] / cn_daily[i - 1][4] - 1)
        r = qm.ols(xs[-250:], ys[-250:])
        if not r:
            return None
        # 当前应参照的美股交易日
        now = datetime.now(CN_TZ)
        t = now.hour * 60 + now.minute
        if now.weekday() < 5 and t < 900:
            nxt = now.date()  # 今日 A 股会话(未开/进行中)
        else:
            nxt = next_weekday(now.date())  # 今日已收盘 / 周末 -> 下一 A 股会话
        need = prev_weekday(nxt)
        r_us = us.get(need)
        out = {"alpha": r["alpha"], "beta": r["beta"], "t": r["t"], "r2": r["r2"], "n": r["n"], "resid_sd": r["resid_sd"],
               "session": nxt.isoformat(), "us_ref_date": need.isoformat(), "us_ref_ret": r_us}
        if r_us is None:
            out["note"] = "参照美股交易日(%s)的 XBI 数据尚不可用,隔夜联动信号无效" % need
            return out
        exp_full = r["alpha"] + r["beta"] * r_us
        realized = 0.0
        q = self.h["quote"]("159502")
        if nxt == now.date() and q and q["prevClose"] > 0 and self.h["market_state"]("159502") in ("open", "lunch"):
            realized = q["price"] / q["prevClose"] - 1
        out["expected_full"] = exp_full
        out["realized_so_far"] = realized
        out["expected_remaining"] = exp_full - realized
        out["p_up"] = qm.norm_cdf((exp_full - realized) / max(r["resid_sd"], 1e-9))
        return out

    # ---------- 提示词 ----------
    def study_text(self, s, name):
        if not s or not s.get("ok"):
            return "%s: 不可用(%s)" % (name, (s or {}).get("reason", "无数据"))
        o, l = s["oos"], s["live"]
        hit = "--" if o["hit"] is None else "%.1f%%(90%%下界%.1f%%)" % (o["hit"] * 100, o["hit_low90"] * 100)
        lw = "--" if o["long_win"] is None else "%.0f%%" % (o["long_win"] * 100)
        return ("%s[预测未来%d根]: 预测收益%sbp (σ=%sbp), 上涨概率%.1f%%; 样本外(%d个不重叠样本)IC=%s(t=%s), 方向命中%s, 基准上涨率%.1f%%, "
                "做多信号扣费后净期望%sbp/笔(n=%d,胜率%s); 主要驱动:%s; 往返成本%.1fbp; 结论:%s"
                % (name, s["H"], f(l["mu_h_bp"], 1, True), f(l["sigma_h_bp"], 1), l["p_up"] * 100, o["n"], f(o["ic"], 3, True), f(o["ic_t"], 2),
                   hit, o["base_up"] * 100, f(o["long_net_bp"], 1, True), o["n_long"], lw,
                   "、".join("%s%+.2f" % (a, b) for a, b in l["contrib"]), s["cost_rt_bp"],
                   "样本外有方向性优势" + ("且显著" if s["significant"] else "但统计不显著") if s["reliable"] else "样本外无可靠优势"))

    def snapshot(self, code):
        s = self.sym(code)
        q = self.h["quote"](code)
        st = self.refresh_study(code)
        now = time.time()
        market = s["market"]
        acc = self.h["account"]()
        pos = acc["positions"].get(code)
        ticks = self.h["ticks"](code, 1000)
        L = []
        L.append("【标的】%s %s | 币种 %s | 最小单位 %d | 市场状态: %s" % (code, s["name"], s["currency"], s["lot"], self.h["market_state"](code)))
        L.append("北京时间 %s | 交易所本地时间 %s" % (fmt_bj(now * 1000), datetime.fromtimestamp(now, TZ[market]).strftime("%m-%d %H:%M")))
        si = session_info(market)
        L.append("交易时段: " + ("已开盘%d分钟, 距收盘%d分钟" % si if si else "非连续竞价时段"))
        if q:
            dec = s["decimals"]
            L.append("【实时】现价 %s | 昨收 %s | 今开 %s | 最高 %s | 最低 %s | 日内涨跌 %s%% | 报价源 %s | 报价时间 %s"
                     % (f(q["price"], dec), f(q["prevClose"], dec), f(q["open"], dec), f(q["high"], dec), f(q["low"], dec),
                        f((q["price"] / q["prevClose"] - 1) * 100, 2, True), q["feed"], fmt_bj(q["quoteTime"])))
        if len(ticks) > 60:
            p = ticks[-1][1]

            def back(sec):
                for t, px in reversed(ticks):
                    if t <= ticks[-1][0] - sec * 1000:
                        return (p / px - 1) * 1e4
                return None
            L.append("【微观·1秒采样】近1分钟 %sbp | 近5分钟 %sbp | 近15分钟 %sbp(采样点%d)"
                     % (f(back(60), 1, True), f(back(300), 1, True), f(back(900), 1, True), len(ticks)))
        rg = st.get("regime", {})
        vr = rg.get("vr5m")
        L.append("【市场状态】5分钟序列:%s%s | 趋势回归 t=%s(近60根) | ATR(5m)=%s%% | 日线ATR=%s%% | 近20日区间位置=%s | 日线5日方差比 z=%s"
                 % (rg.get("label5m", "--"), " (VR=%s,z=%s)" % (f(vr["vr"], 3), f(vr["z"], 2)) if vr else "",
                    f((rg.get("trend5m") or {}).get("t"), 2, True), f(rg.get("atr5m_pct"), 3), f(rg.get("atr1d_pct"), 2),
                    f(rg.get("rng20"), 2), f((rg.get("vr1d") or {}).get("z"), 2, True)))
        L.append("【日内统计模型】" + self.study_text(st.get("intra"), "5分钟模型"))
        L.append("【日线统计模型】" + self.study_text(st.get("daily"), "日线模型"))
        g = st.get("gap")
        if g:
            if g.get("expected_remaining") is None:
                L.append("【隔夜联动·XBI→159502】" + g.get("note", "不可用"))
            else:
                L.append("【隔夜联动·XBI→159502】回归(n=%d): 159502日收益=%s%%+%s×前一美股交易日XBI收益, β的t=%s, R²=%s; 参照美股日%s XBI收益 %s%% => 模型预期 %s 会话收益 %s%%, 已实现 %s%%, 剩余预期 %s%% (上涨概率 %.1f%%, 残差σ=%s%%)"
                         % (g["n"], f(g["alpha"] * 100, 3, True), f(g["beta"], 3), f(g["t"], 2), f(g["r2"], 3), g["us_ref_date"],
                            f(g["us_ref_ret"] * 100, 2, True), g["session"], f(g["expected_full"] * 100, 2, True),
                            f(g["realized_so_far"] * 100, 2, True), f(g["expected_remaining"] * 100, 2, True), g["p_up"] * 100, f(g["resid_sd"] * 100, 2)))
        cr = st.get("corr")
        if cr and cr.get("corr") is not None:
            L.append("【同类标的】与%s的5分钟收益相关系数 %s(n=%d)" % (cr["vs"], f(cr["corr"], 2), cr["n"]))
        for o in self.symbols:
            if o["code"] != code:
                oq = self.h["quote"](o["code"])
                if oq and oq["prevClose"] > 0:
                    L.append("【同类行情】%s 现价 %s 日内 %s%% (%s)" % (o["code"], f(oq["price"], o["decimals"]), f((oq["price"] / oq["prevClose"] - 1) * 100, 2, True), self.h["market_state"](o["code"])))
        cv = self.ctx["v"]
        if cv:
            L.append("【宏观与大盘·日线】" + "; ".join("%s %s(1日%s%%,5日%s%%,20日%s%%,60日z=%s)" % (
                k, f(v["last"], 2), f(v["r1"] * 100, 2, True), f(v["r5"] * 100, 2, True), f(v["r20"] * 100, 2, True), f(v["z60"], 1, True)) for k, v in cv.items()))
        nw = (self.news.get("XBI", {}).get("v") or []) + (self.news.get("IBB", {}).get("v") or [])
        seen, titles = set(), []
        for n in sorted(nw, key=lambda x: -x["ts"]):
            if n["title"] not in seen:
                seen.add(n["title"])
                titles.append("[%s] %s" % (fmt_bj(n["ts"])[:11] if n["ts"] else "?", n["title"]))
        L.append("【近期新闻标题(英文原文,来自 Yahoo Finance)】" + (" | ".join(titles[:6]) if titles else "无"))
        eq = acc["equity"]
        L.append("【账户】权益 ¥%s | 现金 ¥%s | 本标的持仓 %s | 今日AI已实现盈亏 ¥%s | 首次试用本金 ¥%s"
                 % (f(eq, 2), f(acc["cash"], 2), ("%d(成本¥%s)" % (pos["qty"], f(pos["cost"], 2))) if pos and pos["qty"] > 0 else "无",
                    f(acc.get("ai_pnl_24h", 0), 2, True), f(acc["initial"], 0)))
        L.append("【交易成本】手续费单边%.2fbp + 保守滑点单边%.1fbp => 往返成本 %.1fbp。只有预期净优势显著大于成本才值得交易。"
                 % (self.h["fee_rate"] * 1e4, SLIP[market] * 1e4, self.cost_rt(code) * 1e4))
        L.append(OUTPUT_SPEC)
        return "\n".join(L)

    # ---------- 数学闸门 ----------
    def gate(self, code, d, price):
        """返回 (通过?, 检查列表, 仓位信息)。"""
        checks = []

        def chk(name, ok, detail):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})
            return ok
        s = self.sym(code)
        st = self.studies.get(code) or {}
        cost_bp = self.cost_rt(code) * 1e4
        acc = self.h["account"]()
        q = self.h["quote"](code)
        si = session_info(s["market"])
        strat = d.get("strategy", "none")
        chk("AI 方向", d["action"] == "BUY", "AI 建议 %s(策略 %s)" % (d["action"], strat))
        chk("AI 置信度≥0.60", d["confidence"] >= 0.60, "置信度 %.2f" % d["confidence"])
        chk("行情为真实源", bool(q) and q["feed"] in ("sina", "tencent"), "报价源 %s" % (q["feed"] if q else "无"))
        chk("处于连续竞价", si is not None, "非交易时段" if si is None else "已开盘%d分/距收盘%d分" % si)
        if si is not None:
            edge_min = 8 if strat == "swing" else 10
            chk("避开开收盘 %d 分钟" % edge_min, si[0] >= edge_min and (si[1] >= edge_min or strat == "swing"), "开盘后%d分/距收盘%d分" % si)
        # 选择与策略匹配的统计优势
        cands = []
        if strat == "intraday":
            cands.append(("5分钟模型", st.get("intra")))
        elif strat == "swing":
            cands.append(("日线模型", st.get("daily")))
        best = None
        for name, sm in cands:
            if sm and sm.get("ok") and sm.get("reliable"):
                mu = sm["live"]["mu_h_bp"]
                p = sm["live"]["p_up"]
                if best is None or mu > best[1]:
                    best = (name, mu, p, sm["live"]["sigma_h_bp"])
        g = st.get("gap")
        if strat == "swing" and g and g.get("expected_remaining") is not None and g.get("t", 0) >= 2.0:
            mu = g["expected_remaining"] * 1e4
            if best is None or mu > best[1]:
                best = ("隔夜联动模型", mu, g["p_up"], g["resid_sd"] * 1e4)
        if not chk("样本外统计优势", best is not None, ("采用 %s" % best[0]) if best else "策略对应的模型样本外无可靠优势(IC≤0 或 命中率<50%),或策略为 none"):
            best = ("无", 0.0, 0.5, 0.0)
        chk("预期净优势≥1.5×成本", best[1] >= 1.5 * cost_bp, "预期 %+.1fbp vs 往返成本 %.1fbp×1.5=%.1fbp" % (best[1], cost_bp, 1.5 * cost_bp))
        chk("模型上涨概率≥52%", best[2] >= 0.52, "%.1f%%" % (best[2] * 100))
        chk("今日AI亏损未超3%本金", acc.get("ai_pnl_24h", 0) > -0.03 * acc["initial"], "近24h AI 已实现 ¥%.2f" % acc.get("ai_pnl_24h", 0))
        pos = acc["positions"].get(code)
        chk("当前无持仓(不加仓)", not (pos and pos["qty"] > 0), "已持有" if pos and pos["qty"] > 0 else "空仓")
        chk("冷却≥5分钟", time.time() - self.last_trade.get(code, 0) >= 300, "距上次AI成交 %d 秒" % min(99999, time.time() - self.last_trade.get(code, 0)))
        stop = clamp(d.get("stop_pct"), 0.15, 4.0, 0.6) / 100.0
        target = clamp(d.get("target_pct"), 0.0, 10.0, 1.0) / 100.0
        chk("目标≥2×往返成本", target * 1e4 >= 2 * cost_bp, "目标 %.1fbp vs %.1fbp" % (target * 1e4, 2 * cost_bp))
        p_eff = 0.5 + (best[2] - 0.5) * 0.7  # 向 0.5 收缩 30%,抵御估计误差
        frac = qm.size_fraction(p_eff, target, stop) if all(c["ok"] for c in checks) else 0.0
        chk("分数凯利仓位>0", frac > 0, "p_eff=%.3f, 目标/止损=%.2f, 仓位 %.1f%% 权益" % (p_eff, target / stop if stop else 0, frac * 100))
        ok = all(c["ok"] for c in checks)
        return ok, checks, {"stop": stop, "target": target, "frac": frac, "edge_bp": best[1], "p_model": best[2], "model": best[0]}

    # ---------- 分析与执行 ----------
    def trigger(self, code=None):
        for s in self.symbols:
            if code in (None, s["code"]):
                self.force.add(s["code"])
        self.wake.set()

    def _due(self, code):
        if code in self.force:
            return True
        last = self.decisions.get(code, {}).get("ts", 0) / 1000.0
        open_ = self.h["market_state"](code) == "open"
        return time.time() - last >= (AI_INTERVAL if open_ else AI_CLOSED_INTERVAL)

    def _analysis_loop(self):
        while True:
            try:
                want = self.h["auto_mode"]() == 2 or time.time() - self.last_view < 90 or self.force
                if want and llm_health()["up"]:
                    codes = [s["code"] for s in self.symbols if self._due(s["code"])]
                    ths = [threading.Thread(target=self.analyze, args=(c,), daemon=True) for c in codes]
                    for t in ths:
                        t.start()
                    for t in ths:
                        t.join()
            except Exception as ex:
                self.last_err = "loop: %s" % ex
                log("loop error", ex)
            self.wake.wait(5)
            self.wake.clear()

    def analyze(self, code):
        self.force.discard(code)
        t0 = time.time()
        lv = {"running": True, "startedAt": int(t0 * 1000), "tail": "", "tokens": 0}
        self.live[code] = lv
        rec = {"ts": int(t0 * 1000), "code": code, "action": "HOLD", "finalAction": "HOLD", "confidence": 0.0, "error": ""}
        try:
            prompt = self.snapshot(code)
            msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]

            def on_text(txt, n):
                lv["tail"] = txt[-900:]
                lv["tokens"] = n
            full, finish, ntok = llm_stream(msgs, MAX_TOKENS, THINKING, on_text)
            think, ans = split_think(full)
            d = extract_json(ans) if ans else None
            truncated = d is None
            if d is None:  # 推理被截断或格式错误:关闭思考模式快速重答一次
                full2, _, n2 = llm_stream(msgs + [{"role": "user", "content": "请直接输出最终 JSON(不要再思考)。"}], 700, False)
                d = extract_json(split_think(full2)[1] or full2)
                ntok += n2
            if d is None:
                raise ValueError("模型未返回有效 JSON")
            action = str(d.get("action", "HOLD")).upper()
            if action not in ("BUY", "SELL", "HOLD"):
                action = "HOLD"
            dec = {"action": action, "strategy": str(d.get("strategy", "none")).lower(),
                   "confidence": clamp(d.get("confidence"), 0, 1, 0.0), "horizon_min": int(clamp(d.get("horizon_min"), 5, 1440, 30)),
                   "stop_pct": clamp(d.get("stop_pct"), 0.15, 4, 0.6), "target_pct": clamp(d.get("target_pct"), 0, 10, 1.0)}
            q = self.h["quote"](code)
            price = q["price"] if q else 0
            ok, checks, info = self.gate(code, dec, price)
            acc = self.h["account"]()
            pos = acc["positions"].get(code)
            final, exec_note = "HOLD", ""
            mode = self.h["auto_mode"]()
            if action == "BUY" and ok:
                final = "BUY"
            elif action == "SELL" and pos and pos["qty"] > 0:
                final = "SELL"
            rec.update({
                "action": action, "finalAction": final, "strategy": dec["strategy"], "confidence": dec["confidence"],
                "horizonMin": dec["horizon_min"], "stopPct": dec["stop_pct"], "targetPct": dec["target_pct"],
                "thesis": str(d.get("thesis", ""))[:200], "factors": (d.get("factors") or [])[:8], "risks": (d.get("risks") or [])[:4],
                "gate": {"pass": ok, "checks": checks, "info": info}, "price": price, "think": think, "thinkChars": len(think),
                "latencyS": round(time.time() - t0, 1), "tokens": ntok, "truncated": truncated, "mode": mode,
            })
            dt = max(time.time() - t0, 1e-6)
            self.tps = ntok / dt
            if final == "BUY" and mode == 2:
                exec_note = self.execute_buy(code, dec, info, price)
            elif final == "SELL" and mode == 2:
                r = self.h["place"](code, "SELL", pos["qty"], "ai", "AI 平仓: " + rec["thesis"][:40])
                exec_note = "已平仓" if r.get("ok") else "平仓失败:%s" % r.get("error")
                if r.get("ok"):
                    self.plans.pop(code, None)
                    self.last_trade[code] = time.time()
            elif final != "HOLD":
                exec_note = "AI 模式未开启:仅给出建议,未下单"
            rec["exec"] = exec_note
            if action in ("BUY", "SELL") and price > 0:
                self.pending.append({"ts": time.time(), "code": code, "action": action, "p0": price, "h": dec["horizon_min"] * 60,
                                     "cost": self.cost_rt(code)})
        except Exception as ex:
            rec["error"] = str(ex)[:200]
            self.last_err = "analyze %s: %s" % (code, ex)
            log("analyze error", code, ex)
        finally:
            lv["running"] = False
            with self.lock:
                self.decisions[code] = rec
                self.history.insert(0, {k: rec.get(k) for k in ("ts", "code", "action", "finalAction", "confidence", "thesis", "exec", "error", "price")})
                del self.history[60:]
            try:
                with open(self.save_path, "a", encoding="utf-8") as fp:
                    fp.write(json.dumps({k: v for k, v in rec.items() if k != "think"}, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def execute_buy(self, code, dec, info, price):
        s = self.sym(code)
        acc = self.h["account"]()
        fx = self.h["fx"](s)
        unit = price * fx * (1 + self.h["fee_rate"]) * s["lot"]
        budget = min(acc["equity"] * info["frac"], acc["cash"])
        lots = int(budget // unit)
        if lots < 1:
            return "资金不足一手(预算 ¥%.2f < 单手 ¥%.2f)" % (budget, unit)
        r = self.h["place"](code, "BUY", lots * s["lot"], "ai", "AI买入[%s] %s" % (dec["strategy"], info["model"]))
        if not r.get("ok"):
            return "下单失败:%s" % r.get("error")
        swing = dec["strategy"] == "swing"
        self.plans[code] = {"stop": price * (1 - info["stop"]), "target": price * (1 + info["target"]),
                            "expire": time.time() + dec["horizon_min"] * 60, "swing": swing, "entry": time.time()}
        self.last_trade[code] = time.time()
        return "已买入 %d @ %s,止损 %s 止盈 %s" % (lots * s["lot"], f(price, s["decimals"]), f(self.plans[code]["stop"], s["decimals"]), f(self.plans[code]["target"], s["decimals"]))

    def on_tick(self):
        """每秒调用:AI 持仓的止损/止盈/到期/收盘前平仓 + 决策复盘评分。"""
        now = time.time()
        if self.h["auto_mode"]() == 2:
            acc = self.h["account"]()
            for code, pl in list(self.plans.items()):
                pos = acc["positions"].get(code)
                q = self.h["quote"](code)
                if not pos or pos["qty"] <= 0:
                    self.plans.pop(code, None)
                    continue
                if not q or q["feed"] not in ("sina", "tencent") or self.h["market_state"](code) != "open":
                    continue
                px, why = q["price"], ""
                si = session_info(self.sym(code)["market"])
                if px <= pl["stop"]:
                    why = "AI止损"
                elif px >= pl["target"]:
                    why = "AI止盈"
                elif now >= pl["expire"]:
                    why = "AI到期平仓"
                elif not pl["swing"] and si and si[1] <= 3:
                    why = "AI收盘前平仓"
                if why:
                    r = self.h["place"](code, "SELL", pos["qty"], "ai", why)
                    if r.get("ok"):
                        self.plans.pop(code, None)
                        self.last_trade[code] = now
        keep = []
        for p in self.pending:
            if now - p["ts"] < p["h"]:
                keep.append(p)
                continue
            q = self.h["quote"](p["code"])
            if not q:
                continue
            ret = q["price"] / p["p0"] - 1
            with self.lock:
                self.score["n"] += 1
                if p["action"] == "BUY":
                    self.score["buy_n"] += 1
                    self.score["sum_ret"] += ret - p["cost"]
                    if ret - p["cost"] > 0:
                        self.score["buy_hit"] += 1
                else:
                    self.score["sell_n"] += 1
                    if ret < 0:
                        self.score["sell_hit"] += 1
        self.pending = keep

    # ---------- 对外 ----------
    def state(self, full=False, view=True):
        if view:
            self.last_view = time.time()
        lh = llm_health()
        with self.lock:
            decs = {}
            for c, d in self.decisions.items():
                x = dict(d)
                if not full:
                    x["think"] = (x.get("think") or "")[-500:]
                decs[c] = x
            sc = dict(self.score)
        sc["buy_rate"] = (sc["buy_hit"] / sc["buy_n"]) if sc["buy_n"] else None
        sc["avg_net_bp"] = (sc["sum_ret"] / sc["buy_n"] * 1e4) if sc["buy_n"] else None
        studies = {}
        for c, st in self.studies.items():
            def brief(s):
                if not s or not s.get("ok"):
                    return {"ok": False, "reason": (s or {}).get("reason", "无数据")}
                return {"ok": True, "label": s["label"], "H": s["H"], "oos": s["oos"], "live": {k: v for k, v in s["live"].items() if k != "feat"},
                        "reliable": s["reliable"], "significant": s["significant"], "cost_rt_bp": s["cost_rt_bp"]}
            studies[c] = {"intra": brief(st.get("intra")), "daily": brief(st.get("daily")), "gap": st.get("gap"),
                          "regime": st.get("regime"), "corr": st.get("corr"), "bars5": len(self.data.cache.get((c, "5m"), (0, []))[1]),
                          "bars1d": len(self.data.cache.get((c, "1d"), (0, []))[1])}
        return {"available": True, "llm": {"up": lh["up"], "model": lh["model"], "tps": round(self.tps, 1), "name": LLM_NAME},
                "gpus": gpu_stats(), "mode": {0: "off", 1: "rule", 2: "ai"}[self.h["auto_mode"]()], "interval": AI_INTERVAL,
                "decisions": decs, "live": self.live, "history": self.history[:30], "score": sc, "studies": studies,
                "plans": {c: {k: v for k, v in p.items()} for c, p in self.plans.items()},
                "ctx": self.ctx["v"], "err": self.last_err, "now": int(time.time() * 1000),
                "data": {("%s:%s" % k): v for k, v in self.data.meta.items() if isinstance(k, tuple) and len(k) == 2}}

    def history_bars(self, code, tf):
        if code not in DATA_MAP or tf not in TF_SEC:
            raise ValueError("bad code/tf")
        b = self.data.bars(code, tf)
        meta = self.data.meta.get((code, tf), {})
        return {"code": code, "tf": tf, "bars": b, "src": meta.get("src"), "ok": meta.get("ok", False), "err": meta.get("err", "")}
