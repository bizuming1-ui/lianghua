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
import indicators as ind
import reflect

LLM_URL = os.environ.get("QUANT_LLM_URL", "http://127.0.0.1:8001/v1")
LLM_NAME = os.environ.get("QUANT_LLM_NAME", "quant-llm")
AI_INTERVAL = float(os.environ.get("QUANT_AI_INTERVAL", "25"))   # 开市时同一标的两次分析的最短间隔(秒)
AI_CLOSED_INTERVAL = float(os.environ.get("QUANT_AI_CLOSED_INTERVAL", "180"))
ALWAYS = os.environ.get("QUANT_AI_ALWAYS", "1") != "0"           # 1=只要大模型在线就持续推理(让双卡一直工作)
MAX_TOKENS = int(os.environ.get("QUANT_AI_MAX_TOKENS", "3000"))
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
def samples():
    """每次决策的独立推理次数(自洽性投票)。32B 更慢,默认 2 次;14B 默认 3 次。可用 QUANT_AI_SAMPLES 覆盖。"""
    v = os.environ.get("QUANT_AI_SAMPLES")
    if v:
        return max(1, min(6, int(v)))
    m = (llm_health().get("root") or os.environ.get("QUANT_LLM_MODEL", "")).lower()
    return 2 if ("32b" in m or "30b" in m) else 3


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


SINA_US = {"IBB": "ibb", "XBI": "xbi"}


def sina_us_bars(sym, tf):
    """新浪美股 K 线(第三数据源:Yahoo 被限流、东方财富不可达时的兜底;日线为全历史)。"""
    base = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20_k=/US_MinKService."
    url = base + ("getDailyK?symbol=%s&type=daily" % sym if tf == "1d" else "getMinK?symbol=%s&type=%d" % (sym, {"1m": 1, "5m": 5, "15m": 15}[tf]))
    req = urllib.request.Request(url, headers=dict(UA, Referer="https://finance.sina.com.cn"))
    with urllib.request.urlopen(req, timeout=10) as r:
        txt = r.read().decode("utf-8", "replace")
    m = re.search(r"\((\[.*\])\)", txt, re.S)
    if not m:
        raise ValueError("sina us bars: bad payload")
    arr = json.loads(m.group(1))
    out = []
    for x in arr:
        d = x["d"]
        if tf == "1d":
            t = (datetime.strptime(d[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=12)).timestamp() * 1000
        else:
            t = datetime.strptime(d, "%Y-%m-%d %H:%M:%S").replace(tzinfo=US_TZ).timestamp() * 1000
        out.append([int(t), float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"]), float(x.get("v") or 0)])
    return out[-3000:] if tf != "1d" else out[-800:]


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
                if not b and code in SINA_US and tf in ("1m", "5m", "15m", "1d"):
                    try:
                        b, src = sina_us_bars(SINA_US[code], tf), "sina"
                    except Exception as ex:
                        log("新浪美股K线失败:", code, tf, ex)
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


_llm_cache = {"at": 0, "up": False, "model": "", "root": ""}


def llm_health():
    if time.time() - _llm_cache["at"] < 4:
        return _llm_cache
    up, model, root = False, "", ""
    try:
        j = http_json(LLM_URL + "/models", timeout=2, retries=0)
        m0 = (j.get("data") or [{}])[0]
        up, model, root = True, m0.get("id", ""), m0.get("root", "") or ""
    except Exception:
        pass
    _llm_cache.update(at=time.time(), up=up, model=model, root=root)
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


def llm_multi(messages, max_tokens, thinking, n, on_text=None, timeout=1800):
    """一次请求 n 条独立推理(vLLM 的 n 参数,同批次并行,共享提示词前缀缓存;张量并行使两张卡同时计算)。"""
    if n <= 1:
        return [llm_stream(messages, max_tokens, thinking, on_text, timeout)]
    body = {"model": LLM_NAME, "messages": messages, "max_tokens": max_tokens, "temperature": 0.7, "top_p": 0.92,
            "top_k": 30, "n": n, "stream": True, "chat_template_kwargs": {"enable_thinking": bool(thinking)}}
    req = urllib.request.Request(LLM_URL + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    text, reas, fin, nt = [""] * n, [""] * n, [None] * n, [0] * n
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d == "[DONE]":
                break
            j = json.loads(d)
            for ch in j.get("choices") or []:
                i = int(ch.get("index", 0))
                if i >= n:
                    continue
                delta = ch.get("delta") or {}
                rc = delta.get("reasoning_content") or delta.get("reasoning") or ""
                pc = delta.get("content") or ""
                reas[i] += rc
                text[i] += pc
                if rc or pc:
                    nt[i] += 1
                    if i == 0 and on_text and nt[0] % 4 == 0:
                        on_text(("<think>" + reas[0] + "</think>" if reas[0] and not text[0] else "") + text[0], sum(nt))
                if ch.get("finish_reason"):
                    fin[i] = ch["finish_reason"]
    return [(("<think>" + reas[i] + "</think>" if reas[i] else "") + text[i], fin[i], nt[i]) for i in range(n)]


SYSTEM_PROMPT = (
    "你是一名严谨的量化基金经理兼金融数学研究员,正在为一个【模拟盘】做下一步操作决策。"
    "标的:IBB、XBI(美股生物科技ETF)与159502(嘉实标普生物科技ETF,跟踪标普生物科技精选行业指数,与XBI高度同源)。\n"
    "硬性规则:\n"
    "1) 只能使用用户消息里给出的数据与数字;缺失就写缺失,严禁编造价格、新闻或统计量。\n"
    "2) 只做多或空仓(BUY 开多 / SELL 平掉已有多仓 / HOLD 观望)。没有统计优势时必须 HOLD,宁可错过也不要凑交易。\n"
    "3) 必须先检验:样本外是否有优势(IC、命中率及其置信下界)、预期收益是否显著大于往返成本、市场状态(方差比)是否支持该类策略、"
    "宏观与大盘(VIX、美债、美元、SPY/QQQ/XLV)是否逆风、消息面是否有重大风险,再给结论。\n"
    "4) 手续费是固定费用:每笔(买或卖)成交金额<¥10000 固定¥5,≥¥10000 按万五;本金只有¥10000,所以一次往返≈¥10,必须赚得比成本多得多。\n"
    "5) 用户消息里有【历史教训】(来自你自己的真实复盘)和【自适应状态】,必须逐条核对本次是否重蹈覆辙;发现与教训冲突就 HOLD。\n"
    "6) 指标(规则投票/学习复合分)只是证据而不是结论:样本外不可靠的指标不得作为买入理由。\n"
    "7) 思考要精炼(尽量不超过 700 字),最后只输出一个 JSON 对象,不要输出其他文字。"
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
        self.reflector = reflect.Reflector()
        self.inds = {}
        self._icache = {}
        self._lcache = {}
        self.brief = {"at": 0.0, "text": ""}
        self.reflect_log = []
        self.dlog = []
        self.blk = threading.Lock()
        self.active, self.busy_since, self.busy_log = 0, 0.0, []
        self.cycles, self.task = 0, "启动中"
        self.save_path = os.path.join(os.environ.get("QUANT_SAVE_DIR", "/kaggle/working" if os.path.isdir("/kaggle/working") else "."), "ai_decisions.jsonl")

    # ---------- 基础 ----------
    def sym(self, code):
        return next(s for s in self.symbols if s["code"] == code)

    def cost_rt(self, code, notional=None):
        """往返成本(小数):2×(固定/万五手续费 ÷ 名义金额 + 滑点)。固定 ¥5 手续费对小仓位非常昂贵。"""
        m = self.sym(code)["market"]
        if notional is None:
            try:
                notional = max(2000.0, self.h["account"]()["equity"] * 0.9)
            except Exception:
                notional = 9000.0
        return 2.0 * (self.h["fee"](notional) / notional + SLIP[m])

    def start(self):
        threading.Thread(target=self._bg_loop, daemon=True).start()
        threading.Thread(target=self._brain_loop, daemon=True).start()

    # ---------- 后台:数据与统计检验 ----------
    def _bg_loop(self):
        while True:
            try:
                for s in self.symbols:
                    self.refresh_study(s["code"])
                    for tf in ("5m", "1d"):
                        try:
                            self.inds.setdefault(s["code"], {})[tf] = self.indicators(s["code"], tf)
                        except Exception as ex:
                            log("indicators", s["code"], tf, ex)
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
        nom = max(2000.0, acc["equity"] * 0.9)
        L.append("【交易成本】每笔手续费:成交金额<¥10000 固定¥5,≥¥10000 按万五。按名义金额≈¥%.0f 计:单边手续费 %.1fbp + 保守滑点 %.1fbp => 往返成本 %.1fbp。只有预期净优势显著大于成本才值得交易。"
                 % (nom, self.h["fee"](nom) / nom * 1e4, SLIP[market] * 1e4, self.cost_rt(code, nom) * 1e4))
        for tf, nm in (("5m", "5分钟"), ("1d", "日线")):
            ib = (self.inds.get(code) or {}).get(tf)
            if ib and ib.get("ok"):
                L.append("【多指标引擎·%s】" % nm + ind.summary_text(ib["all"], ib["learned"]).replace("\n", " | "))
        ad = self.reflector.adapt()
        L.append("【自适应状态】%s;当前置信度门槛≥%.2f,预期优势门槛≥%.1f×成本%s" % (ad["note"], ad["min_conf"], ad["edge_mult"], "(连亏冷静期中)" if ad["paused"] else ""))
        ls = self.reflector.lessons_text()
        L.append("【历史教训(来自你自己的真实复盘,必须逐条核对)】\n" + (ls or "(暂无,尚无足够已结算的决策)"))
        if self.brief.get("text"):
            L.append("【宏观简报(你 %d 分钟前写的)】%s" % ((time.time() - self.brief["at"]) / 60, self.brief["text"]))
        L.append(OUTPUT_SPEC)
        return "\n".join(L)

    # ---------- 数学闸门 ----------
    def gate(self, code, d, price, p_start, votes=None):
        """返回 (通过?, 检查列表, 仓位信息)。所有检查由代码执行,大模型无法越过。"""
        checks = []

        def chk(name, ok, detail):
            checks.append({"name": name, "ok": bool(ok), "detail": detail})
            return ok
        s = self.sym(code)
        st = self.studies.get(code) or {}
        acc = self.h["account"]()
        q = self.h["quote"](code)
        si = session_info(s["market"])
        strat = d.get("strategy", "none")
        ad = self.reflector.adapt()
        conf_cal, ncal = self.reflector.calibrate(d["confidence"])
        chk("AI 方向", d["action"] == "BUY", "AI 建议 %s(策略 %s)" % (d["action"], strat))
        if votes:
            chk("多次采样投票一致", votes.get("agree", False), "投票 %s(共 %d 次独立推理,BUY 需≥%d 票且无分歧否决)" % (votes.get("tally"), votes.get("n", 1), votes.get("need", 1)))
        chk("置信度≥%.2f(自适应)" % ad["min_conf"], conf_cal >= ad["min_conf"],
            "自称 %.2f → 复盘校准 %.2f(同档历史样本 %d)。%s" % (d["confidence"], conf_cal, ncal, ad["note"]))
        chk("非冷静期", not ad["paused"], "连亏冷静期剩 %d 秒" % ad["pause_left_s"] if ad["paused"] else "正常")
        chk("行情为真实源", bool(q) and q["feed"] in ("sina", "tencent"), "报价源 %s" % (q["feed"] if q else "无"))
        chk("处于连续竞价", si is not None, "非交易时段" if si is None else "已开盘%d分/距收盘%d分" % si)
        if si is not None:
            edge_min = 8 if strat == "swing" else 10
            chk("避开开收盘 %d 分钟" % edge_min, si[0] >= edge_min and (si[1] >= edge_min or strat == "swing"), "开盘后%d分/距收盘%d分" % si)
        stop = clamp(d.get("stop_pct"), 0.15, 4.0, 0.6) / 100.0
        target = clamp(d.get("target_pct"), 0.0, 10.0, 1.0) / 100.0
        if p_start and price:
            drift = abs(price / p_start - 1)
            chk("决策期间价格未漂移", drift <= max(0.0025, 0.4 * stop), "推理开始 %s → 现在 %s,漂移 %.2f%%(推理耗时越长,越要确认价格没跑)" % (f(p_start, 3), f(price, 3), drift * 100))
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
        # 多指标共识:学习复合分(样本外可靠才参与)+ 规则投票不极端看空
        ik = "1d" if strat == "swing" else "5m"
        ib = (self.inds.get(code) or {}).get(ik) or {}
        cp = ((ib.get("learned") or {}).get("composite"))
        if cp and cp.get("reliable"):
            chk("学习复合分不反对", cp["now"] >= 0, "复合分 %+.2f(样本外IC %+.3f)" % (cp["now"], cp["oos_ic"]))
        else:
            chk("学习复合分不反对", True, "复合分样本外不可靠/不可用,本项不参与(不因它放行也不因它否决)")
        vt = (ib.get("all") or {}).get("vote")
        if vt:
            chk("规则指标投票不极端看空", vt["net"] > -0.45 * vt["total"], "看多%d/看空%d/共%d" % (vt["bull"], vt["bear"], vt["total"]))
        # 仓位:固定手续费使"小仓位"不划算 → 用单笔风险预算决定(通常接近满仓),且凯利期望必须为正
        p_eff = 0.5 + (best[2] - 0.5) * 0.7  # 向 0.5 收缩 30%,抵御估计误差
        kel = qm.kelly_fraction(p_eff, target / stop) if stop > 0 and target > 0 else 0.0
        frac = min(0.95, 0.015 / stop) if (kel > 0 and stop > 0) else 0.0
        fx = self.h["fx"](s)
        unit = (price or 0) * fx * s["lot"]
        notional = min(acc["equity"] * frac, acc["cash"] * 0.98)
        chk("资金≥一手", unit > 0 and notional >= unit, "可用名义 ¥%.2f vs 一手 ¥%.2f" % (notional, unit))
        cost_bp = self.cost_rt(code, max(notional, unit, 1.0)) * 1e4
        chk("预期净优势≥%.1f×成本" % ad["edge_mult"], best[1] >= ad["edge_mult"] * cost_bp,
            "预期 %+.1fbp vs 往返成本 %.1fbp(固定手续费¥5×2÷名义¥%.0f + 滑点)×%.1f=%.1fbp" % (best[1], cost_bp, max(notional, unit), ad["edge_mult"], ad["edge_mult"] * cost_bp))
        chk("模型上涨概率≥52%", best[2] >= 0.52, "%.1f%%" % (best[2] * 100))
        chk("今日AI亏损未超3%本金", acc.get("ai_pnl_24h", 0) > -0.03 * acc["initial"], "近24h AI 已实现 ¥%.2f" % acc.get("ai_pnl_24h", 0))
        pos = acc["positions"].get(code)
        chk("当前无持仓(不加仓)", not (pos and pos["qty"] > 0), "已持有" if pos and pos["qty"] > 0 else "空仓")
        chk("冷却≥5分钟", time.time() - self.last_trade.get(code, 0) >= 300, "距上次AI成交 %d 秒" % min(99999, time.time() - self.last_trade.get(code, 0)))
        chk("目标≥2×往返成本", target * 1e4 >= 2 * cost_bp, "目标 %.1fbp vs %.1fbp" % (target * 1e4, 2 * cost_bp))
        chk("凯利期望为正", kel > 0, "p_eff=%.3f, 目标/止损=%.2f, 凯利 %.3f, 风险预算仓位 %.0f%% 权益(名义 ¥%.0f)" % (p_eff, target / stop if stop else 0, kel, frac * 100, notional))
        ok = all(c["ok"] for c in checks)
        return ok, checks, {"stop": stop, "target": target, "frac": frac, "edge_bp": best[1], "p_model": best[2], "model": best[0],
                            "cost_bp": cost_bp, "notional": notional, "conf_cal": conf_cal}

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

    def _busy(self, delta):
        with self.blk:
            now = time.time()
            if self.active == 0 and delta > 0:
                self.busy_since = now
            self.active = max(0, self.active + delta)
            if self.active == 0 and self.busy_since:
                self.busy_log.append((self.busy_since, now))
                self.busy_since = 0.0
            while self.busy_log and self.busy_log[0][1] < now - 600:
                self.busy_log.pop(0)

    def duty(self, window=120.0):
        """最近 window 秒里,至少有一个推理请求在跑的时间占比(= 双卡处于计算状态的占比)。"""
        with self.blk:
            now = time.time()
            lo = now - window
            tot = sum(max(0.0, b - max(a, lo)) for a, b in self.busy_log if b > lo)
            if self.active and self.busy_since:
                tot += now - max(self.busy_since, lo)
            return min(1.0, tot / window)

    def _brain_loop(self):
        """大脑循环:只要大模型在线就一直有活干 —— 到期标的分析 > 复盘反思 > 宏观简报 > 对最久未分析的标的深度再分析。"""
        while True:
            try:
                if not llm_health()["up"]:
                    self.task = "等待大模型就绪(Cell 0)"
                    time.sleep(3)
                    continue
                want = ALWAYS or self.h["auto_mode"]() == 2 or time.time() - self.last_view < 90 or self.force
                if not want:
                    self.task = "空闲(AI_ALWAYS=0 且无人查看)"
                    self.wake.wait(5)
                    self.wake.clear()
                    continue
                codes = [s["code"] for s in self.symbols if self._due(s["code"])]
                codes.sort(key=lambda c: (c not in self.force, self.h["market_state"](c) != "open", self.decisions.get(c, {}).get("ts", 0)))
                if codes:
                    self.task = "分析 " + "/".join(codes)
                    ths = [threading.Thread(target=self.analyze, args=(c,), daemon=True) for c in codes]
                    for t in ths:
                        t.start()
                    for t in ths:
                        t.join()
                elif self.reflector.need_reflection():
                    self.task = "复盘反思"
                    self.run_reflection()
                elif time.time() - self.brief["at"] > 600:
                    self.task = "撰写宏观简报"
                    self.run_brief()
                else:
                    c = min(self.symbols, key=lambda s: (self.h["market_state"](s["code"]) != "open", self.decisions.get(s["code"], {}).get("ts", 0)))["code"]
                    self.task = "持续推理 " + c
                    self.analyze(c)
                self.cycles += 1
            except Exception as ex:
                self.last_err = "loop: %s" % ex
                log("loop error", ex)
                time.sleep(2)

    def norm_dec(self, d):
        action = str(d.get("action", "HOLD")).upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"
        return {"action": action, "strategy": str(d.get("strategy", "none")).lower(),
                "confidence": clamp(d.get("confidence"), 0, 1, 0.0), "horizon_min": int(clamp(d.get("horizon_min"), 5, 1440, 30)),
                "stop_pct": clamp(d.get("stop_pct"), 0.15, 4, 0.6), "target_pct": clamp(d.get("target_pct"), 0, 10, 1.0),
                "thesis": str(d.get("thesis", ""))[:200], "factors": (d.get("factors") or [])[:8], "risks": (d.get("risks") or [])[:4]}

    def aggregate(self, ds):
        """自洽性投票:BUY 需 ≥⌈2n/3⌉ 票;SELL(降风险)需 ≥⌈n/2⌉ 票;否则 HOLD。"""
        n = len(ds)
        tally = {}
        for d in ds:
            tally[d["action"]] = tally.get(d["action"], 0) + 1
        need_buy, need_sell = math.ceil(2 * n / 3), math.ceil(n / 2)
        action = "HOLD"
        if tally.get("BUY", 0) >= need_buy and tally.get("BUY", 0) > tally.get("SELL", 0):
            action = "BUY"
        elif tally.get("SELL", 0) >= need_sell and tally.get("SELL", 0) > tally.get("BUY", 0):
            action = "SELL"
        ag = [d for d in ds if d["action"] == action] or ds
        med = lambda k: sorted(x[k] for x in ag)[len(ag) // 2]  # noqa: E731
        best = max(ag, key=lambda x: x["confidence"])
        strat_cnt = {}
        for x in ag:
            strat_cnt[x["strategy"]] = strat_cnt.get(x["strategy"], 0) + 1
        dec = {"action": action, "strategy": max(strat_cnt, key=strat_cnt.get), "confidence": sum(x["confidence"] for x in ag) / len(ag),
               "horizon_min": int(med("horizon_min")), "stop_pct": med("stop_pct"), "target_pct": med("target_pct"),
               "thesis": best["thesis"], "factors": best["factors"], "risks": best["risks"]}
        agree = action != "HOLD" or max(tally.values()) == n
        votes = {"tally": tally, "n": n, "need": need_buy, "agree": bool(action == "HOLD" or tally.get(action, 0) >= (need_buy if action == "BUY" else need_sell)),
                 "samples": [{"action": d["action"], "conf": round(d["confidence"], 2)} for d in ds]}
        del agree
        return dec, votes

    def analyze(self, code):
        self.force.discard(code)
        t0 = time.time()
        k = samples()
        lv = {"running": True, "startedAt": int(t0 * 1000), "tail": "", "tokens": 0, "samples": k}
        self.live[code] = lv
        rec = {"ts": int(t0 * 1000), "code": code, "action": "HOLD", "finalAction": "HOLD", "confidence": 0.0, "error": ""}
        try:
            q0 = self.h["quote"](code)
            p_start = q0["price"] if q0 else 0
            prompt = self.snapshot(code)
            msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]

            def on_text(txt, n):
                lv["tail"] = txt[-900:]
                lv["tokens"] = n
            self._busy(1)
            try:
                outs = llm_multi(msgs, MAX_TOKENS, THINKING, k, on_text)
            finally:
                self._busy(-1)
            decs, thinks, ntok, truncated = [], [], 0, 0
            for full, finish, nt in outs:
                ntok += nt
                think, ans = split_think(full)
                d = extract_json(ans) if ans else None
                if d is None:  # 推理被截断或格式错误:关闭思考模式快速重答一次
                    truncated += 1
                    self._busy(1)
                    try:
                        full2, _, n2 = llm_stream(msgs + [{"role": "user", "content": "请直接输出最终 JSON(不要再思考)。"}], 700, False)
                    finally:
                        self._busy(-1)
                    d = extract_json(split_think(full2)[1] or full2)
                    ntok += n2
                if d is not None:
                    decs.append(self.norm_dec(d))
                    thinks.append(think)
            if not decs:
                raise ValueError("模型未返回有效 JSON")
            dec, votes = self.aggregate(decs)
            action = dec["action"]
            think = thinks[0] if thinks else ""
            q = self.h["quote"](code)
            price = q["price"] if q else 0
            ok, checks, info = self.gate(code, dec, price, p_start, votes)
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
                "confCal": info.get("conf_cal"), "votes": votes,
                "horizonMin": dec["horizon_min"], "stopPct": dec["stop_pct"], "targetPct": dec["target_pct"],
                "thesis": dec["thesis"], "factors": dec["factors"], "risks": dec["risks"],
                "gate": {"pass": ok, "checks": checks, "info": info}, "price": price, "think": think, "thinkChars": len(think),
                "latencyS": round(time.time() - t0, 1), "tokens": ntok, "truncated": truncated > 0, "mode": mode,
            })
            self.tps = ntok / max(time.time() - t0, 1e-6)
            if final == "BUY" and mode == 2:
                exec_note = self.execute_buy(code, dec, info, price)
            elif final == "SELL" and mode == 2:
                r = self.h["place"](code, "SELL", pos["qty"], "ai", "AI 平仓: " + rec["thesis"][:40])
                exec_note = "已平仓" if r.get("ok") else "平仓失败:%s" % r.get("error")
                if r.get("ok"):
                    self.note_close(code, "AI 主动平仓", r["trade"], self.plans.pop(code, None))
                    self.last_trade[code] = time.time()
            elif final != "HOLD":
                exec_note = "AI 模式未开启:仅给出建议,未下单"
            rec["exec"] = exec_note
            if action in ("BUY", "SELL") and price > 0:
                cost = (info["cost_bp"] / 1e4) if action == "BUY" and info.get("cost_bp") else self.cost_rt(code)
                self.pending.append({"ts": time.time(), "code": code, "action": action, "p0": price, "h": dec["horizon_min"] * 60, "cost": cost,
                                     "hi": price, "lo": price, "conf": dec["confidence"], "strategy": dec["strategy"], "horizon_min": dec["horizon_min"],
                                     "thesis": dec["thesis"], "factors": dec["factors"][:4], "edge_bp": info.get("edge_bp"), "model": info.get("model", "")})
        except Exception as ex:
            rec["error"] = str(ex)[:200]
            self.last_err = "analyze %s: %s" % (code, ex)
            log("analyze error", code, ex)
        finally:
            lv["running"] = False
            with self.lock:
                self.decisions[code] = rec
                self.history.insert(0, {k_: rec.get(k_) for k_ in ("ts", "code", "action", "finalAction", "confidence", "thesis", "exec", "error", "price")})
                del self.history[60:]
                self.dlog.append({k_: v for k_, v in rec.items() if k_ != "think"})
                del self.dlog[:-300]
            try:
                with open(self.save_path, "a", encoding="utf-8") as fp:
                    fp.write(json.dumps({k_: v for k_, v in rec.items() if k_ != "think"}, ensure_ascii=False) + "\n")
            except Exception:
                pass
            self.h["dirty"]()

    # ---------- 反思 / 简报(空闲算力的有价值用法)----------
    def run_reflection(self):
        lv = {"running": True, "startedAt": int(time.time() * 1000), "tail": "", "tokens": 0, "samples": 1}
        self.live["_reflect"] = lv
        try:
            msgs = [{"role": "system", "content": "你是严谨、诚实、不护短的量化复盘官。只依据给出的真实结果下结论,发现自己的错误要直说。"},
                    {"role": "user", "content": self.reflector.reflection_prompt()}]

            def on_text(txt, n):
                lv["tail"] = txt[-900:]
                lv["tokens"] = n
            self._busy(1)
            try:
                full, _, n = llm_stream(msgs, 2200, THINKING, on_text)
            finally:
                self._busy(-1)
            d = extract_json(split_think(full)[1] or "")
            if d is None:
                self._busy(1)
                try:
                    full2, _, _ = llm_stream(msgs + [{"role": "user", "content": "请直接输出最终 JSON(不要再思考)。"}], 600, False)
                finally:
                    self._busy(-1)
                d = extract_json(split_think(full2)[1] or full2)
            added = self.reflector.apply_reflection(d) if d else 0
            self.reflect_log.insert(0, {"ts": int(time.time() * 1000), "verdict": (d or {}).get("verdict", "解析失败"), "added": added,
                                        "mistakes": ((d or {}).get("mistakes") or [])[:4]})
            del self.reflect_log[20:]
        except Exception as ex:
            self.last_err = "reflect: %s" % ex
            self.reflector.last_reflect_at = time.time()
        finally:
            lv["running"] = False
            self.h["dirty"]()

    def run_brief(self):
        self.brief["at"] = time.time()
        try:
            cv = self.ctx["v"]
            if not cv:
                return
            txt = "; ".join("%s %s(1日%s%%,5日%s%%,20日%s%%,60日z=%s)" % (k, f(v["last"], 2), f(v["r1"] * 100, 2, True), f(v["r5"] * 100, 2, True), f(v["r20"] * 100, 2, True), f(v["z60"], 1, True)) for k, v in cv.items())
            nw = (self.news.get("XBI", {}).get("v") or []) + (self.news.get("IBB", {}).get("v") or [])
            titles = " | ".join(n["title"] for n in sorted(nw, key=lambda x: -x["ts"])[:6]) or "无"
            msgs = [{"role": "system", "content": "你是宏观策略分析师。只使用给出的数据,不编造。"},
                    {"role": "user", "content": "【宏观与大盘·日线】%s\n【新闻标题】%s\n请用不超过150字的中文,概括对美股生物科技ETF(XBI/IBB)与159502 未来1-3天的整体风险偏好判断(偏多/中性/偏空)及最关键的2个理由。" % (txt, titles)}]
            self._busy(1)
            try:
                full, _, _ = llm_stream(msgs, 500, False)
            finally:
                self._busy(-1)
            self.brief = {"at": time.time(), "text": split_think(full)[1].strip() or full.strip()}
        except Exception as ex:
            self.last_err = "brief: %s" % ex
        finally:
            self.h["dirty"]()

    def note_close(self, code, why, trade, plan):
        """AI 平仓后记入反思账本。"""
        try:
            pnl = trade.get("pnlCny")
            if pnl is None:
                return
            plan = plan or {}
            self.reflector.record_close({"ts": time.time(), "code": code, "why": why, "pnl": pnl, "pnl_pct": trade.get("pnlPct") or 0.0,
                                         "hold_s": int(time.time() - plan.get("entry", time.time())), "thesis": plan.get("thesis", "")})
        except Exception as ex:
            log("note_close", ex)

    def execute_buy(self, code, dec, info, price):
        s = self.sym(code)
        acc = self.h["account"]()
        fx = self.h["fx"](s)
        unit = price * fx * s["lot"]
        budget = min(acc["equity"] * info["frac"], acc["cash"])
        lots = int(budget // unit)
        while lots >= 1 and lots * unit + self.h["fee"](lots * unit) > acc["cash"] + 1e-9:
            lots -= 1  # 固定手续费也要从现金里扣
        if lots < 1:
            return "资金不足一手(预算 ¥%.2f,单手 ¥%.2f,另需手续费)" % (budget, unit)
        r = self.h["place"](code, "BUY", lots * s["lot"], "ai", "AI买入[%s] %s" % (dec["strategy"], info["model"]))
        if not r.get("ok"):
            return "下单失败:%s" % r.get("error")
        swing = dec["strategy"] == "swing"
        self.plans[code] = {"stop": price * (1 - info["stop"]), "target": price * (1 + info["target"]),
                            "expire": time.time() + dec["horizon_min"] * 60, "swing": swing, "entry": time.time(),
                            "thesis": dec.get("thesis", "")}
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
                        self.note_close(code, why, r["trade"], self.plans.pop(code, None))
                        self.last_trade[code] = now
        keep = []
        for p in self.pending:
            q = self.h["quote"](p["code"])
            if q and q["feed"] in ("sina", "tencent"):
                p["hi"], p["lo"] = max(p.get("hi", p["p0"]), q["price"]), min(p.get("lo", p["p0"]), q["price"])
            if now - p["ts"] < p["h"]:
                keep.append(p)
                continue
            if not q:
                continue
            ret = q["price"] / p["p0"] - 1
            sgn = 1.0 if p["action"] == "BUY" else -1.0
            self.reflector.record_outcome({
                "ts": p["ts"], "code": p["code"], "action": p["action"], "conf": p.get("conf", 0.0), "strategy": p.get("strategy", ""),
                "p0": p["p0"], "p1": q["price"], "ret": ret * sgn, "net": ret * sgn - p["cost"],
                "mfe": (p.get("hi", p["p0"]) / p["p0"] - 1) if sgn > 0 else (1 - p.get("lo", p["p0"]) / p["p0"]),
                "mae": (p.get("lo", p["p0"]) / p["p0"] - 1) if sgn > 0 else (1 - p.get("hi", p["p0"]) / p["p0"]) * -1,
                "horizon_min": p.get("horizon_min", 0), "thesis": p.get("thesis", ""), "factors": p.get("factors", []),
                "edge_bp": p.get("edge_bp"), "model": p.get("model", "")})
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
        return {"available": True, "llm": {"up": lh["up"], "model": lh.get("root") or lh["model"], "tps": round(self.tps, 1), "name": LLM_NAME},
                "gpus": gpu_stats(), "mode": {0: "off", 1: "rule", 2: "ai"}[self.h["auto_mode"]()], "interval": AI_INTERVAL,
                "decisions": decs, "live": self.live, "history": self.history[:30], "score": sc, "studies": studies,
                "plans": {c: {k: v for k, v in p.items()} for c, p in self.plans.items()},
                "ctx": self.ctx["v"], "err": self.last_err, "now": int(time.time() * 1000),
                "reflect": dict(self.reflector.state(), log=self.reflect_log[:5]), "briefing": self.brief,
                "brain": {"duty": round(self.duty(120), 3), "duty30": round(self.duty(30), 3), "active": self.active, "samples": samples() if lh["up"] else 0,
                          "task": self.task, "cycles": self.cycles, "always": ALWAYS},
                "ind": {c: {tf: ({"vote": v["all"]["vote"], "price": v["all"]["price"],
                                  "comp": ({k: v["learned"]["composite"][k] for k in ("now", "now_pctile", "oos_ic", "oos_hit", "reliable", "significant", "oos_long_net_bp")}
                                           if (v.get("learned") or {}).get("composite") else None),
                                  "kept": (v.get("learned") or {}).get("n_kept"), "nfeat": (v.get("learned") or {}).get("n_feat")}
                                 if v and v.get("ok") else None) for tf, v in d.items()} for c, d in self.inds.items()},
                "data": {("%s:%s" % k): v for k, v in self.data.meta.items() if isinstance(k, tuple) and len(k) == 2}}

    def history_bars(self, code, tf):
        if code not in DATA_MAP or tf not in TF_SEC:
            raise ValueError("bad code/tf")
        b = self.data.bars(code, tf)
        meta = self.data.meta.get((code, tf), {})
        return {"code": code, "tf": tf, "bars": b, "src": meta.get("src"), "ok": meta.get("ok", False), "err": meta.get("err", "")}

    # ---------- 指标(全部)----------
    def _overlay(self, code, tf, b):
        """把实时价叠加到最后一根 K 线(未收盘的那根),指标即刻反映最新价。"""
        q = self.h["quote"](code)
        if not q or tf == "1d" or not b or q["feed"] not in ("sina", "tencent"):
            return b
        last = list(b[-1])
        if q["quoteTime"] < last[0] + TF_SEC[tf] * 1000 * 1.5:
            px = q["price"]
            last[2], last[3], last[4] = max(last[2], px), min(last[3], px), px
            return b[:-1] + [last]
        return b

    def indicators(self, code, tf):
        if code not in DATA_MAP or tf not in TF_SEC:
            raise ValueError("bad code/tf")
        b = self.data.bars(code, tf)
        if len(b) < 60:
            return {"ok": False, "code": code, "tf": tf, "error": "K 线不足(%d<60)" % len(b)}
        b2 = self._overlay(code, tf, b)
        key = (code, tf, len(b2), b2[-1][0], round(b2[-1][4], 4))
        c = self._icache.get(key)
        if not c:
            c = ind.compute_all(b2)
            self._icache = {k: v for k, v in self._icache.items() if k[:2] != (code, tf)}
            self._icache[key] = c
        H = {"1m": 10, "5m": 6, "15m": 4, "1d": 3}[tf]
        lk = (code, tf)
        lc = self._lcache.get(lk)
        if not lc or lc["key"] != (len(b), b[-1][0]) or time.time() - lc["at"] > 300:
            lc = {"key": (len(b), b[-1][0]), "at": time.time(), "v": ind.learn_weights(b, H, self.cost_rt(code))}
            self._lcache[lk] = lc
        meta = self.data.meta.get((code, tf), {})
        return {"ok": bool(c.get("ok")), "code": code, "tf": tf, "all": c, "learned": lc["v"], "src": meta.get("src"), "H": H, "at": int(time.time() * 1000)}

    # ---------- 持久化 ----------
    def export_state(self):
        with self.lock:
            return {"decisions_log": list(self.dlog[-300:]), "history": list(self.history[:60]), "score": dict(self.score),
                    "plans": {c: dict(p) for c, p in self.plans.items()}, "pending": [dict(p) for p in self.pending][-200:],
                    "last_trade": dict(self.last_trade), "reflect": self.reflector.export(), "brief": dict(self.brief),
                    "reflect_log": list(self.reflect_log[:20])}

    def import_state(self, d):
        with self.lock:
            self.dlog = list(d.get("decisions_log") or [])[-300:]
            for rec in self.dlog:
                if rec.get("code"):
                    self.decisions[rec["code"]] = rec
            self.history = list(d.get("history") or [])[:60]
            if d.get("score"):
                self.score.update(d["score"])
            self.plans = dict(d.get("plans") or {})
            self.pending = list(d.get("pending") or [])
            self.last_trade = dict(d.get("last_trade") or {})
            self.brief = dict(d.get("brief") or self.brief)
            self.reflect_log = list(d.get("reflect_log") or [])[:20]
        self.reflector.load(d.get("reflect"))

    def reset_learning(self):
        """重置账户时:清掉持仓计划与待评估项;复盘教训与校准数据保留(那是'经验',不是账户)。"""
        with self.lock:
            self.plans.clear()
            self.pending.clear()
            self.last_trade.clear()
