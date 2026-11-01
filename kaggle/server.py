#!/usr/bin/env python3
"""
生物科技ETF 量化终端 - Kaggle / 本地 Python 版后端(零第三方依赖)

与 Next.js 版(src/lib/quant/*)实现同一套接口:
  GET  /               量化终端页面(index.html)
  GET  /api/state      行情 + 指标 + 账户 + 持仓 + 胜率 + 最近成交(1 秒轮询)
  GET  /api/trades     成交日志分页
  POST /api/trade      模拟下单   {symbol, side, qty}
  POST /api/auto       自动策略开关 {enabled}
  POST /api/reset      重置账户(10000 元)
  GET  /api/health     健康检查

行情源:新浪(主) -> 腾讯(备);全部失败时才使用明确标注的"模拟行情"。
存储:SQLite(默认 /kaggle/working/quant.db,否则当前目录)。
"""
import json
import math
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# 前端单文件:Kaggle 单元把它与 server.py 放在同目录;GitHub 仓库里位于 public/terminal/
INDEX_PATH = next(
    (p for p in (os.path.join(HERE, "index.html"),
                 os.path.join(HERE, "..", "public", "terminal", "index.html")) if os.path.exists(p)),
    os.path.join(HERE, "index.html"),
)
DB_PATH = os.environ.get("QUANT_DB") or (
    "/kaggle/working/quant.db" if os.path.isdir("/kaggle/working") else os.path.join(HERE, "quant.db")
)
PORT = int(os.environ.get("PORT", "8000"))

# ----------------------------- 配置(与 config.ts 保持一致) -----------------------------
SYMBOLS = [
    {"code": "IBB", "name": "生物科技指数ETF-iShares Nasdaq", "short": "iShares Nasdaq Biotech", "market": "US",
     "currency": "USD", "lot": 1, "decimals": 2, "sina": "gb_ibb", "tencent": "usIBB", "base": 217.6},
    {"code": "XBI", "name": "美国生科ETF-SPDR", "short": "SPDR S&P Biotech", "market": "US",
     "currency": "USD", "lot": 1, "decimals": 2, "sina": "gb_xbi", "tencent": "usXBI", "base": 156.0},
    {"code": "159502", "name": "标普生物科技ETF嘉实", "short": "嘉实标普生物科技", "market": "CN",
     "currency": "CNY", "lot": 100, "decimals": 3, "sina": "sz159502", "tencent": "sz159502", "base": 1.62},
]
INITIAL_CAPITAL = 10000.0
FEE_RATE = 0.0003
TICK_MAX = 1800
POLL_S = 1.0
DEFAULT_FX = 7.2
MA_FAST, MA_SLOW, RSI_N, BOLL_N, BOLL_K = 30, 120, 60, 120, 2
TAKE_PROFIT, STOP_LOSS = 0.012, 0.008
AUTO_COOLDOWN_S, AUTO_CASH_FRACTION = 30.0, 0.5

# ----------------------------- 时区 / 市场时段 -----------------------------
CN_TZ = timezone(timedelta(hours=8))
try:
    from zoneinfo import ZoneInfo
    US_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    try:
        from dateutil import tz as _tz
        US_TZ = _tz.gettz("America/New_York") or timezone(timedelta(hours=-5))
    except Exception:
        US_TZ = timezone(timedelta(hours=-5))
TZ = {"US": US_TZ, "CN": CN_TZ}
STATE_LABEL = {"open": "交易中", "pre": "盘前", "post": "盘后", "lunch": "午休", "closed": "休市"}


def market_state(market, now_s):
    d = datetime.fromtimestamp(now_s, TZ[market])
    if d.weekday() >= 5:
        return "closed"
    t = d.hour * 60 + d.minute
    if market == "US":
        if 570 <= t < 960:
            return "open"
        if 240 <= t < 570:
            return "pre"
        if 960 <= t < 1200:
            return "post"
        return "closed"
    if 570 <= t < 690 or 780 <= t < 900:
        return "open"
    if 690 <= t < 780:
        return "lunch"
    if 555 <= t < 570:
        return "pre"
    return "closed"


def zoned_ms(y, mo, d, hh, mi, ss, market):
    return datetime(y, mo, d, hh, mi, ss, tzinfo=TZ[market]).timestamp() * 1000


# ----------------------------- 行情源 -----------------------------
def http_text(url, headers):
    h = {"User-Agent": "Mozilla/5.0 (quant-terminal)"}
    h.update(headers)
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=2.5) as r:
        return r.read().decode("gb18030", "replace")


def num(s):
    try:
        v = float(s)
        return v if math.isfinite(v) else 0.0
    except Exception:
        return 0.0


def parse_cn_time(date_s, time_s, fallback_ms):
    try:
        y, mo, d = [int(x) for x in date_s.split("-")]
        hh, mi, ss = [int(x) for x in time_s.split(":")]
        return zoned_ms(y, mo, d, hh, mi, ss, "CN")
    except Exception:
        return fallback_ms


def fetch_sina():
    lst = ",".join([s["sina"] for s in SYMBOLS] + ["fx_susdcny"])
    text = http_text("https://hq.sinajs.cn/list=" + lst, {"Referer": "https://finance.sina.com.cn"})
    mp = {}
    for m in re.finditer(r'hq_str_(\w+)="([^"]*)"', text):
        mp[m.group(1)] = m.group(2).split(",")
    now_ms = time.time() * 1000
    quotes = {}
    for s in SYMBOLS:
        f = mp.get(s["sina"])
        if not f or len(f) < 10:
            continue
        if s["market"] == "US":
            price = num(f[1])
            prev = num(f[26]) if len(f) > 26 else 0
            st = market_state("US", time.time())
            ext = num(f[21]) if len(f) > 21 else 0
            if st in ("pre", "post") and ext > 0:
                price = ext
            if price <= 0:
                continue
            try:
                ds, ts = f[3].split(" ")
                qt = parse_cn_time(ds, ts, now_ms)
            except Exception:
                qt = now_ms
            quotes[s["code"]] = {"code": s["code"], "price": price, "prevClose": prev if prev > 0 else price,
                                 "open": num(f[5]) or price, "high": num(f[6]) or price, "low": num(f[7]) or price,
                                 "quoteTime": qt, "feed": "sina"}
        else:
            price = num(f[3])
            prev = num(f[2])
            if price <= 0:
                price = prev
            if price <= 0:
                continue
            quotes[s["code"]] = {"code": s["code"], "price": price, "prevClose": prev if prev > 0 else price,
                                 "open": num(f[1]) or price, "high": num(f[4]) or price, "low": num(f[5]) or price,
                                 "quoteTime": parse_cn_time(f[30], f[31], now_ms) if len(f) > 31 else now_ms,
                                 "feed": "sina"}
    fxr = mp.get("fx_susdcny")
    fx = num(fxr[1]) if fxr and len(fxr) > 1 and num(fxr[1]) > 5 else None
    return {"quotes": quotes, "fx": fx}


def fetch_tencent():
    text = http_text("https://qt.gtimg.cn/q=" + ",".join(s["tencent"] for s in SYMBOLS),
                     {"Referer": "https://gu.qq.com"})
    quotes = {}
    now_ms = time.time() * 1000
    for s in SYMBOLS:
        m = re.search(r'v_%s="([^"]*)"' % re.escape(s["tencent"]), text)
        if not m:
            continue
        f = m.group(1).split("~")
        if len(f) < 35:
            continue
        price = num(f[3])
        if price <= 0:
            continue
        ts = f[30]
        qt = now_ms
        a = re.match(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$", ts)
        b = re.match(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$", ts)
        mm = a or b
        if mm:
            qt = zoned_ms(*[int(x) for x in mm.groups()], s["market"])
        quotes[s["code"]] = {"code": s["code"], "price": price, "prevClose": num(f[4]) or price,
                             "open": num(f[5]) or price, "high": num(f[33]) or price, "low": num(f[34]) or price,
                             "quoteTime": qt, "feed": "tencent"}
    return {"quotes": quotes, "fx": None}


def fetch_yahoo_fx():
    req = urllib.request.Request("https://query1.finance.yahoo.com/v8/finance/chart/CNY=X?interval=1m&range=1d",
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=2.5) as r:
        j = json.loads(r.read().decode("utf-8"))
    p = j["chart"]["result"][0]["meta"]["regularMarketPrice"]
    return p if isinstance(p, (int, float)) and p > 5 else None


# ----------------------------- 指标(与 indicators.ts 一致) -----------------------------
def rsi_calc(prices, n):
    if len(prices) < n + 1:
        return None
    w = prices[-(n + 1):]
    up = dn = 0.0
    for i in range(1, len(w)):
        d = w[i] - w[i - 1]
        if d > 0:
            up += d
        else:
            dn -= d
    if up + dn == 0:
        return 50.0
    return 100.0 * up / (up + dn)


def analyze(prices):
    sig = {"ready": False, "warm": len(prices), "need": MA_SLOW, "maFast": None, "maSlow": None, "rsi": None,
           "bollUp": None, "bollMid": None, "bollLow": None, "score": 0, "action": "HOLD", "reasons": []}
    if len(prices) < MA_SLOW:
        return sig
    last = prices[-1]
    ma_fast = sum(prices[-MA_FAST:]) / MA_FAST
    ma_slow = sum(prices[-MA_SLOW:]) / MA_SLOW
    bw = prices[-BOLL_N:]
    bm = sum(bw) / len(bw)
    bs = math.sqrt(sum((v - bm) ** 2 for v in bw) / len(bw))
    r = rsi_calc(prices, RSI_N)
    sig.update(ready=True, maFast=ma_fast, maSlow=ma_slow, rsi=r, bollMid=bm,
               bollUp=bm + BOLL_K * bs, bollLow=bm - BOLL_K * bs)
    score = 0
    rel = (ma_fast - ma_slow) / ma_slow
    if rel > 0.0002:
        score += 1
        sig["reasons"].append("MA30 在 MA120 之上(趋势向上)")
    elif rel < -0.0002:
        score -= 1
        sig["reasons"].append("MA30 在 MA120 之下(趋势向下)")
    if r is not None:
        if r < 30:
            score += 1
            sig["reasons"].append("RSI 超卖(<30)")
        elif r > 70:
            score -= 1
            sig["reasons"].append("RSI 超买(>70)")
    if bs > 0:
        if last < sig["bollLow"]:
            score += 1
            sig["reasons"].append("价格跌破布林下轨")
        elif last > sig["bollUp"]:
            score -= 1
            sig["reasons"].append("价格突破布林上轨")
    sig["score"] = score
    sig["action"] = "BUY" if score >= 2 else ("SELL" if score <= -1 else "HOLD")
    return sig


# ----------------------------- 状态 / 数据库 -----------------------------
LOCK = threading.RLock()
E = {
    "epoch": int(time.time() * 1000), "ticks": {}, "quotes": {}, "signals": {},
    "fx": {"rate": DEFAULT_FX, "src": "default", "at": 0},
    "feed": {"name": "-", "ok": False, "latency": 0, "lastOkAt": 0, "error": "starting"},
    "auto": False, "last_auto": {},
}
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row


def r2(v):
    return math.floor(v * 100 + 0.5) / 100


def r4(v):
    return math.floor(v * 10000 + 0.5) / 10000


def init_db():
    with LOCK:
        DB.executescript(
            """
            CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY, cash REAL, initial REAL, auto INTEGER, started_at INTEGER);
            CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY, qty INTEGER, cost REAL);
            CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, symbol TEXT, side TEXT,
              qty INTEGER, price REAL, currency TEXT, fx REAL, amount REAL, fee REAL, pnl REAL, pnl_pct REAL,
              source TEXT, feed TEXT, note TEXT);
            """
        )
        if not DB.execute("SELECT 1 FROM account WHERE id=1").fetchone():
            DB.execute("INSERT INTO account VALUES(1,?,?,0,?)", (INITIAL_CAPITAL, INITIAL_CAPITAL, int(time.time() * 1000)))
        DB.commit()
        E["auto"] = bool(DB.execute("SELECT auto FROM account WHERE id=1").fetchone()[0])


def trade_row(r):
    return {"id": r["id"], "ts": r["ts"], "symbol": r["symbol"], "side": r["side"], "qty": r["qty"], "price": r["price"],
            "currency": r["currency"], "fx": r["fx"], "amountCny": r["amount"], "feeCny": r["fee"], "pnlCny": r["pnl"],
            "pnlPct": r["pnl_pct"], "source": r["source"], "feed": r["feed"], "note": r["note"] or ""}


def fx_for(sym):
    return 1.0 if sym["currency"] == "CNY" else E["fx"]["rate"]


def execute(code, side, qty, source):
    sym = next((s for s in SYMBOLS if s["code"] == code), None)
    if not sym:
        return {"ok": False, "error": "未知标的"}
    if not isinstance(qty, int) or qty <= 0:
        return {"ok": False, "error": "数量必须是正整数"}
    if qty % sym["lot"] != 0:
        return {"ok": False, "error": "%s 最小交易单位为 %d" % (code, sym["lot"])}
    with LOCK:
        q = E["quotes"].get(code)
        if not q or q["price"] <= 0:
            return {"ok": False, "error": "暂无行情,无法成交"}
        price = q["price"]
        fx = fx_for(sym)
        amount = r2(price * qty * fx)
        fee = r2(amount * FEE_RATE)
        now_ms = int(time.time() * 1000)
        notes = []
        st = market_state(sym["market"], time.time())
        if st != "open":
            notes.append("%s·按最近价成交" % STATE_LABEL[st])
        if q["feed"] == "sim":
            notes.append("模拟行情")
        if q["feed"] == "stale":
            notes.append("行情延迟")
        acc = DB.execute("SELECT * FROM account WHERE id=1").fetchone()
        pos = DB.execute("SELECT * FROM positions WHERE symbol=?", (code,)).fetchone()
        cur_qty = pos["qty"] if pos else 0
        cur_cost = pos["cost"] if pos else 0.0
        pnl = pnl_pct = None
        if side == "BUY":
            need = r2(amount + fee)
            if acc["cash"] + 1e-9 < need:
                return {"ok": False, "error": "资金不足:需要 ¥%s,可用 ¥%s" % (need, r2(acc["cash"]))}
            DB.execute("UPDATE account SET cash=? WHERE id=1", (r4(acc["cash"] - need),))
            DB.execute("INSERT INTO positions(symbol,qty,cost) VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty, cost=excluded.cost",
                       (code, cur_qty + qty, r4(cur_cost + need)))
        else:
            if cur_qty < qty:
                return {"ok": False, "error": "持仓不足:持有 %d,欲卖 %d" % (cur_qty, qty)}
            proceeds = r2(amount - fee)
            cost_part = cur_cost * (qty / cur_qty)
            pnl = r2(proceeds - cost_part)
            pnl_pct = pnl / cost_part if cost_part > 0 else 0.0
            DB.execute("UPDATE account SET cash=? WHERE id=1", (r4(acc["cash"] + proceeds),))
            DB.execute("UPDATE positions SET qty=?, cost=? WHERE symbol=?",
                       (cur_qty - qty, 0 if cur_qty - qty == 0 else r4(cur_cost - cost_part), code))
        cur = DB.execute(
            "INSERT INTO trades(ts,symbol,side,qty,price,currency,fx,amount,fee,pnl,pnl_pct,source,feed,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_ms, code, side, qty, price, sym["currency"], fx, amount, fee, pnl, pnl_pct, source, q["feed"], ";".join(notes)))
        DB.commit()
        row = DB.execute("SELECT * FROM trades WHERE id=?", (cur.lastrowid,)).fetchone()
        return {"ok": True, "trade": trade_row(row)}


def run_strategy():
    now = time.time()
    acc = DB.execute("SELECT cash FROM account WHERE id=1").fetchone()
    cash = acc["cash"]
    poss = {r["symbol"]: r for r in DB.execute("SELECT * FROM positions")}
    for s in SYMBOLS:
        q = E["quotes"].get(s["code"])
        sig = E["signals"].get(s["code"])
        if not q or not sig or not sig["ready"]:
            continue
        if q["feed"] not in ("sina", "tencent"):
            continue
        if market_state(s["market"], now) != "open":
            continue
        if now - E["last_auto"].get(s["code"], 0) < AUTO_COOLDOWN_S:
            continue
        fx = fx_for(s)
        pos = poss.get(s["code"])
        if pos and pos["qty"] > 0:
            mv = q["price"] * fx * pos["qty"]
            ret = (mv - pos["cost"]) / pos["cost"] if pos["cost"] > 0 else 0
            if ret >= TAKE_PROFIT or ret <= -STOP_LOSS or sig["action"] == "SELL":
                r = execute(s["code"], "SELL", pos["qty"], "auto")
                if r["ok"]:
                    E["last_auto"][s["code"]] = now
                    cash += r["trade"]["amountCny"]
        elif sig["action"] == "BUY":
            unit = q["price"] * fx * (1 + FEE_RATE) * s["lot"]
            lots = int((cash * AUTO_CASH_FRACTION) // unit)
            if lots >= 1:
                r = execute(s["code"], "BUY", lots * s["lot"], "auto")
                if r["ok"]:
                    E["last_auto"][s["code"]] = now
                    cash -= r["trade"]["amountCny"] + r["trade"]["feeCny"]


# ----------------------------- 轮询线程 -----------------------------
def apply_real(sym, raw, now_ms):
    prev = E["quotes"].get(sym["code"])
    if prev and prev["feed"] == "sim":
        E["ticks"][sym["code"]] = []
    E["quotes"][sym["code"]] = dict(raw, lastOk=now_ms)


def apply_fallback(sym, now_ms):
    q = E["quotes"].get(sym["code"])
    if q and q["feed"] != "sim":
        if now_ms - q["lastOk"] > 10000:
            q["feed"] = "stale"
        return
    base = q["price"] if q else sym["base"]
    price = base * (1 + (random.random() - 0.5) * 0.0012)
    price = round(price, sym["decimals"])
    E["quotes"][sym["code"]] = {
        "code": sym["code"], "price": price, "prevClose": q["prevClose"] if q else sym["base"],
        "open": q["open"] if q else sym["base"], "high": max(q["high"] if q else price, price),
        "low": min(q["low"] if q else price, price), "quoteTime": now_ms, "feed": "sim", "lastOk": now_ms}


def poll():
    t0 = time.time()
    res, name, err = None, "sina", ""
    try:
        res = fetch_sina()
    except Exception as ex:
        err = "sina: %s" % ex

    def missing():
        return [s for s in SYMBOLS if not res or s["code"] not in res["quotes"]]

    if not res or missing():
        try:
            t = fetch_tencent()
            if not res:
                res, name = t, "tencent"
            else:
                for s in missing():
                    if s["code"] in t["quotes"]:
                        res["quotes"][s["code"]] = t["quotes"][s["code"]]
                name = "sina+tencent"
        except Exception as ex:
            err += " tencent: %s" % ex
    now_ms = int(time.time() * 1000)
    with LOCK:
        got = bool(res and res["quotes"])
        if got:
            E["feed"] = {"name": name, "ok": True, "latency": now_ms - int(t0 * 1000), "lastOkAt": now_ms, "error": ""}
        else:
            E["feed"] = dict(E["feed"], name=name, ok=False, latency=now_ms - int(t0 * 1000), error=err or "no data")
        if res and res.get("fx"):
            E["fx"] = {"rate": res["fx"], "src": "sina", "at": now_ms}
    if not (res and res.get("fx")) and now_ms - E["fx"]["at"] > 60000:
        E["fx"]["at"] = now_ms
        try:
            y = fetch_yahoo_fx()
            if y:
                E["fx"] = {"rate": y, "src": "yahoo", "at": now_ms}
        except Exception:
            pass
    sec = now_ms // 1000 * 1000
    with LOCK:
        for s in SYMBOLS:
            raw = res["quotes"].get(s["code"]) if res else None
            if raw:
                apply_real(s, raw, now_ms)
            else:
                apply_fallback(s, now_ms)
            q = E["quotes"][s["code"]]
            arr = E["ticks"].setdefault(s["code"], [])
            if not arr or arr[-1][0] < sec:
                arr.append([sec, q["price"]])
                if len(arr) > TICK_MAX:
                    del arr[: len(arr) - TICK_MAX]
                E["signals"][s["code"]] = analyze([x[1] for x in arr])
        if E["auto"]:
            run_strategy()


def poll_loop():
    while True:
        t = time.time()
        try:
            poll()
        except Exception as ex:  # 轮询失败不应让线程退出
            print("poll error:", ex, file=sys.stderr)
        time.sleep(max(0.05, POLL_S - (time.time() - t)))


# ----------------------------- 对外查询 -----------------------------
def build_state(since):
    now_ms = int(time.time() * 1000)
    with LOCK:
        acc = DB.execute("SELECT * FROM account WHERE id=1").fetchone()
        poss = DB.execute("SELECT * FROM positions").fetchall()
        st = DB.execute(
            "SELECT count(*) n, sum(side='BUY') buys, sum(side='SELL') sells, "
            "sum(side='SELL' AND pnl>0) wins, coalesce(sum(pnl),0) realized, coalesce(sum(fee),0) fees FROM trades").fetchone()
        n, buys, sells, wins = st["n"] or 0, st["buys"] or 0, st["sells"] or 0, st["wins"] or 0
        recent = [trade_row(r) for r in DB.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 40")]
        quotes = []
        for s in SYMBOLS:
            q = E["quotes"].get(s["code"])
            state = market_state(s["market"], time.time())
            quotes.append({
                "code": s["code"], "name": s["name"], "short": s["short"], "market": s["market"],
                "currency": s["currency"], "lot": s["lot"], "decimals": s["decimals"],
                "price": q["price"] if q else None, "prevClose": q["prevClose"] if q else None,
                "open": q["open"] if q else None, "high": q["high"] if q else None, "low": q["low"] if q else None,
                "change": (q["price"] - q["prevClose"]) if q else None,
                "pct": ((q["price"] / q["prevClose"] - 1) * 100) if q and q["prevClose"] > 0 else None,
                "quoteTime": q["quoteTime"] if q else None, "feed": q["feed"] if q else "none",
                "state": state, "stateLabel": STATE_LABEL[state]})
        mv = cost = 0.0
        pos_out = []
        for p in poss:
            if p["qty"] <= 0:
                continue
            s = next(x for x in SYMBOLS if x["code"] == p["symbol"])
            q = E["quotes"].get(p["symbol"])
            fx = fx_for(s)
            value = q["price"] * fx * p["qty"] if q else 0.0
            mv += value
            cost += p["cost"]
            pos_out.append({"symbol": p["symbol"], "qty": p["qty"], "costCny": p["cost"],
                            "avgPrice": p["cost"] / (p["qty"] * fx), "mvCny": value, "pnlCny": value - p["cost"],
                            "pnlPct": (value - p["cost"]) / p["cost"] if p["cost"] > 0 else 0})
        equity = acc["cash"] + mv
        ticks = {s["code"]: [t for t in E["ticks"].get(s["code"], []) if t[0] > since] for s in SYMBOLS}
        return {
            "ts": now_ms, "epoch": E["epoch"], "quotes": quotes, "ticks": ticks, "signals": E["signals"],
            "fx": E["fx"], "feed": E["feed"],
            "account": {"cash": acc["cash"], "initial": acc["initial"], "equity": equity, "marketValue": mv,
                        "unrealized": mv - cost, "realized": st["realized"], "fees": st["fees"],
                        "returnPct": (equity / acc["initial"] - 1) * 100, "auto": bool(acc["auto"]),
                        "startedAt": acc["started_at"]},
            "positions": pos_out,
            "stats": {"trades": n, "buys": buys, "sells": sells, "wins": wins, "losses": sells - wins,
                      "winRate": (wins / sells * 100) if sells > 0 else None},
            "trades": recent}


def list_trades(offset, limit):
    with LOCK:
        total = DB.execute("SELECT count(*) FROM trades").fetchone()[0]
        rows = DB.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return {"total": total, "rows": [trade_row(r) for r in rows]}


def set_auto(enabled):
    with LOCK:
        DB.execute("UPDATE account SET auto=? WHERE id=1", (1 if enabled else 0,))
        DB.commit()
        E["auto"] = bool(enabled)


def reset_account():
    with LOCK:
        DB.execute("DELETE FROM trades")
        DB.execute("DELETE FROM positions")
        DB.execute("UPDATE account SET cash=?, initial=?, auto=0, started_at=? WHERE id=1",
                   (INITIAL_CAPITAL, INITIAL_CAPITAL, int(time.time() * 1000)))
        DB.commit()
        E["auto"] = False
        E["last_auto"] = {}


# ----------------------------- HTTP -----------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/site", "/index.html"):
                with open(INDEX_PATH, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._json(build_state(int(float(q.get("since", ["0"])[0] or 0))))
            if u.path == "/api/trades":
                off = max(0, int(q.get("offset", ["0"])[0] or 0))
                lim = min(100000, max(1, int(q.get("limit", ["50"])[0] or 50)))
                return self._json(list_trades(off, lim))
            if u.path == "/api/health":
                return self._json({"ok": True})
            return self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception as ex:
            return self._json({"error": str(ex)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return self._json({"ok": False, "error": "请求格式错误"}, 400)
        try:
            if u.path == "/api/trade":
                side = str(body.get("side", "")).upper()
                if side not in ("BUY", "SELL"):
                    return self._json({"ok": False, "error": "side 必须是 BUY 或 SELL"}, 400)
                qty = body.get("qty")
                try:
                    qty = int(qty)
                except Exception:
                    qty = 0
                r = execute(str(body.get("symbol", "")), side, qty, "manual")
                return self._json(r, 200 if r["ok"] else 400)
            if u.path == "/api/auto":
                set_auto(bool(body.get("enabled")))
                return self._json({"ok": True, "enabled": bool(body.get("enabled"))})
            if u.path == "/api/reset":
                reset_account()
                return self._json({"ok": True})
            return self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception as ex:
            return self._json({"ok": False, "error": str(ex)}, 500)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    init_db()
    threading.Thread(target=poll_loop, daemon=True).start()
    httpd = Server(("0.0.0.0", PORT), Handler)
    print("量化终端已启动: http://127.0.0.1:%d  (数据库 %s)" % (PORT, DB_PATH), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
