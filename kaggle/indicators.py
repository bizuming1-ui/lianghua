"""
全指标引擎(numpy)——全部由真实 K 线计算,没有任何编造数据。

两层用法:
  1) compute_all(bars)     最新一根 K 线上的 40+ 个指标:数值 + 方向信号(+1 看多 / -1 看空 / 0 中性) + 文字解释,
                           并给出"教科书规则投票"。
  2) learn_weights(bars,H) 对每个指标做"样本外"检验:训练段估计 IC(指标值与未来 H 根收益的相关),
                           只保留训练段显著的指标,用 IC 加权合成复合分,再在测试段验证复合分的 IC / 命中率 / 扣费净期望。
                           => 系统不是迷信某个指标,而是让数据告诉我们:这只标的当前哪些指标真的有预测力。

bars: [[t_ms, open, high, low, close, volume], ...](时间升序)
"""
import math

import numpy as np


def _a(x):
    return np.asarray(x, dtype=float)


# ----------------------------- 基础序列 -----------------------------
def ema(x, n):
    x = _a(x)
    out = np.empty_like(x)
    if len(x) == 0:
        return out
    a = 2.0 / (n + 1.0)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def wilder(x, n):
    x = _a(x)
    out = np.empty_like(x)
    if len(x) == 0:
        return out
    out[0] = x[0]
    a = 1.0 / n
    for i in range(1, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def sma(x, n):
    x = _a(x)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    cs = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1:] = (cs[n:] - cs[:-n]) / n
    out[:n - 1] = np.cumsum(x[:n - 1]) / np.arange(1, n)
    return out


def rolling_max(x, n):
    x = _a(x)
    return np.array([x[max(0, i - n + 1): i + 1].max() for i in range(len(x))])


def rolling_min(x, n):
    x = _a(x)
    return np.array([x[max(0, i - n + 1): i + 1].min() for i in range(len(x))])


def rolling_std(x, n):
    x = _a(x)
    out = np.zeros(len(x))
    for i in range(len(x)):
        w = x[max(0, i - n + 1): i + 1]
        out[i] = w.std() if len(w) > 1 else 0.0
    return out


def true_range(h, l, c):
    pc = np.r_[c[0], c[:-1]]
    return np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))


def _safe(a, b, default=0.0):
    return np.where(np.abs(b) > 1e-12, a / np.where(np.abs(b) > 1e-12, b, 1.0), default)


# ----------------------------- 指标序列(全部返回与 bars 等长的数组) -----------------------------
def rsi_series(c, n):
    d = np.diff(c, prepend=c[0])
    up, dn = wilder(np.maximum(d, 0), n), wilder(np.maximum(-d, 0), n)
    rs = _safe(up, dn, 1e9)
    return np.where(dn < 1e-12, np.where(up < 1e-12, 50.0, 100.0), 100.0 - 100.0 / (1.0 + rs))


def stoch_kd(h, l, c, n=9, k_s=3, d_s=3):
    hh, ll = rolling_max(h, n), rolling_min(l, n)
    rsv = _safe(c - ll, hh - ll, 0.5) * 100.0
    k = np.empty_like(rsv)
    d = np.empty_like(rsv)
    k[0] = d[0] = 50.0
    for i in range(1, len(rsv)):
        k[i] = (k_s - 1) / k_s * k[i - 1] + 1.0 / k_s * rsv[i]
        d[i] = (d_s - 1) / d_s * d[i - 1] + 1.0 / d_s * k[i]
    return k, d, 3 * k - 2 * d, rsv


def adx_series(h, l, c, n=14):
    up = np.diff(h, prepend=h[0])
    dn = -np.diff(l, prepend=l[0])
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    atr = wilder(true_range(h, l, c), n)
    pdi = 100 * _safe(wilder(pdm, n), atr)
    mdi = 100 * _safe(wilder(mdm, n), atr)
    dx = 100 * _safe(np.abs(pdi - mdi), pdi + mdi)
    return wilder(dx, n), pdi, mdi


def supertrend(h, l, c, n=10, mult=3.0):
    atr = wilder(true_range(h, l, c), n)
    hl2 = (h + l) / 2.0
    ub, lb = hl2 + mult * atr, hl2 - mult * atr
    fu, fl = ub.copy(), lb.copy()
    dirn = np.ones(len(c))
    for i in range(1, len(c)):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        if dirn[i - 1] == 1:
            dirn[i] = -1 if c[i] < fl[i] else 1
        else:
            dirn[i] = 1 if c[i] > fu[i] else -1
    return dirn, np.where(dirn > 0, fl, fu)


def psar(h, l, af0=0.02, af_max=0.2):
    n = len(h)
    sar = np.empty(n)
    trend = np.ones(n)
    if n < 3:
        return trend, h.copy()
    up = h[1] >= h[0]
    ep = h[0] if up else l[0]
    s = l[0] if up else h[0]
    af = af0
    sar[0] = s
    trend[0] = 1 if up else -1
    for i in range(1, n):
        s = s + af * (ep - s)
        if up:
            s = min(s, l[i - 1], l[i - 2] if i > 1 else l[i - 1])
            if l[i] < s:
                up, s, ep, af = False, ep, l[i], af0
            elif h[i] > ep:
                ep, af = h[i], min(af + af0, af_max)
        else:
            s = max(s, h[i - 1], h[i - 2] if i > 1 else h[i - 1])
            if h[i] > s:
                up, s, ep, af = True, ep, h[i], af0
            elif l[i] < ep:
                ep, af = l[i], min(af + af0, af_max)
        sar[i] = s
        trend[i] = 1 if up else -1
    return trend, sar


def aroon_osc(h, l, n=25):
    out = np.zeros(len(h))
    for i in range(len(h)):
        wh, wl = h[max(0, i - n): i + 1], l[max(0, i - n): i + 1]
        since_h = len(wh) - 1 - int(np.argmax(wh))
        since_l = len(wl) - 1 - int(np.argmin(wl))
        out[i] = 100.0 * (since_l - since_h) / n
    return out


def cci_series(h, l, c, n=20):
    tp = (h + l + c) / 3.0
    m = sma(tp, n)
    md = np.array([np.abs(tp[max(0, i - n + 1): i + 1] - m[i]).mean() for i in range(len(tp))])
    return _safe(tp - m, 0.015 * md)


def mfi_series(h, l, c, v, n=14):
    tp = (h + l + c) / 3.0
    mf = tp * v
    d = np.diff(tp, prepend=tp[0])
    pos, neg = np.where(d > 0, mf, 0.0), np.where(d < 0, mf, 0.0)
    out = np.full(len(c), 50.0)
    for i in range(n, len(c)):
        p, q = pos[i - n + 1: i + 1].sum(), neg[i - n + 1: i + 1].sum()
        out[i] = 100.0 if q <= 0 and p > 0 else (50.0 if q <= 0 else 100.0 - 100.0 / (1.0 + p / q))
    return out


def obv_series(c, v):
    d = np.sign(np.diff(c, prepend=c[0]))
    return np.cumsum(d * v)


def cmf_series(h, l, c, v, n=20):
    mfm = _safe((c - l) - (h - c), h - l)
    mfv = mfm * v
    out = np.zeros(len(c))
    for i in range(len(c)):
        a = max(0, i - n + 1)
        sv = v[a: i + 1].sum()
        out[i] = mfv[a: i + 1].sum() / sv if sv > 0 else 0.0
    return out


def session_vwap(t, h, l, c, v):
    day = np.cumsum(np.r_[0, (np.diff(t) > 4 * 3600 * 1000).astype(int)])
    tp = (h + l + c) / 3.0
    out = c.copy()
    for d in np.unique(day):
        m = day == d
        cv, cp = np.cumsum(v[m]), np.cumsum(tp[m] * v[m])
        out[m] = np.where(cv > 0, cp / np.where(cv > 0, cv, 1.0), c[m])
    return out


def linreg_t(c, n=30):
    out = np.zeros(len(c))
    x = np.arange(n, dtype=float)
    xm = x.mean()
    sxx = ((x - xm) ** 2).sum()
    for i in range(n - 1, len(c)):
        y = np.log(c[i - n + 1: i + 1])
        ym = y.mean()
        b = ((x - xm) * (y - ym)).sum() / sxx
        res = y - (ym + b * (x - xm))
        se = math.sqrt(max((res ** 2).sum() / (n - 2), 1e-18) / sxx)
        out[i] = b / se if se > 0 else 0.0
    return out


def ichimoku(h, l):
    tenkan = (rolling_max(h, 9) + rolling_min(l, 9)) / 2
    kijun = (rolling_max(h, 26) + rolling_min(l, 26)) / 2
    sa = (tenkan + kijun) / 2
    sb = (rolling_max(h, 52) + rolling_min(l, 52)) / 2
    return tenkan, kijun, sa, sb


def build_series(bars):
    """返回 (序列字典 S, 基础数组 B)。S 里的每一项都是与 bars 等长的 numpy 数组。"""
    B = _a(bars)
    t, o, h, l, c, v = (B[:, i] for i in range(6))
    n = len(c)
    S = {}
    r1 = np.diff(np.log(c), prepend=np.log(c[0]))
    S["ret"] = r1
    for k in (5, 10, 20, 30, 60, 120):
        S["ma%d" % k] = sma(c, k)
    for k in (8, 12, 21, 26, 55, 100, 200):
        S["ema%d" % k] = ema(c, k)
    macd = S["ema12"] - S["ema26"]
    sig = ema(macd, 9)
    S["macd"], S["macd_sig"], S["macd_hist"] = macd, sig, macd - sig
    for k in (6, 14, 24):
        S["rsi%d" % k] = rsi_series(c, k)
    rs14 = S["rsi14"]
    lo_r, hi_r = rolling_min(rs14, 14), rolling_max(rs14, 14)
    S["stochrsi"] = _safe(rs14 - lo_r, hi_r - lo_r, 0.5) * 100
    S["k"], S["d"], S["j"], S["rsv"] = stoch_kd(h, l, c, 9)
    hh14, ll14 = rolling_max(h, 14), rolling_min(l, 14)
    S["willr"] = -100 * _safe(hh14 - c, hh14 - ll14, 0.5)
    S["cci"] = cci_series(h, l, c, 20)
    S["roc12"] = np.r_[np.zeros(12), c[12:] / c[:-12] - 1] * 100 if n > 12 else np.zeros(n)
    S["mom10"] = np.r_[np.zeros(10), c[10:] - c[:-10]] if n > 10 else np.zeros(n)
    S["trix"] = np.r_[0, np.diff(ema(ema(ema(c, 15), 15), 15))] / np.maximum(c, 1e-9) * 100
    S["adx"], S["pdi"], S["mdi"] = adx_series(h, l, c, 14)
    S["st_dir"], S["st_line"] = supertrend(h, l, c, 10, 3.0)
    S["sar_dir"], S["sar"] = psar(h, l)
    S["aroon"] = aroon_osc(h, l, 25)
    S["tenkan"], S["kijun"], S["spana"], S["spanb"] = ichimoku(h, l)
    S["atr14"] = wilder(true_range(h, l, c), 14)
    bm, bs = sma(c, 20), rolling_std(c, 20)
    S["bb_mid"], S["bb_up"], S["bb_lo"] = bm, bm + 2 * bs, bm - 2 * bs
    S["bb_pctb"] = _safe(c - S["bb_lo"], S["bb_up"] - S["bb_lo"], 0.5)
    S["bb_bw"] = _safe(S["bb_up"] - S["bb_lo"], bm)
    em20, atr10 = ema(c, 20), wilder(true_range(h, l, c), 10)
    S["kc_up"], S["kc_lo"] = em20 + 2 * atr10, em20 - 2 * atr10
    S["dc_up"], S["dc_lo"] = rolling_max(h, 20), rolling_min(l, 20)
    S["dc_pos"] = _safe(c - S["dc_lo"], S["dc_up"] - S["dc_lo"], 0.5)
    S["zscore20"] = _safe(c - bm, bs)
    S["linreg_t"] = linreg_t(c, 30)
    S["mfi"] = mfi_series(h, l, c, v, 14)
    S["obv"] = obv_series(c, v)
    S["obv_ma"] = ema(S["obv"], 20)
    S["cmf"] = cmf_series(h, l, c, v, 20)
    S["vwap"] = session_vwap(t, h, l, c, v)
    vm = sma(v, 20)
    S["volratio"] = _safe(v, vm, 1.0)
    S["force"] = ema(np.diff(c, prepend=c[0]) * v, 13)
    S["rv20"] = np.sqrt(sma(r1 ** 2, 20))
    return S, B


# ----------------------------- 方向化特征(正=看多;符号由数据在训练段决定)-----------------------------
def feature_dict(S, B):
    c, v = B[:, 4], B[:, 5]
    sg = np.maximum(S["rv20"], 1e-6)
    F = {}
    F["EMA8/21 趋势"] = (S["ema8"] - S["ema21"]) / c / sg
    F["EMA21/55 趋势"] = (S["ema21"] - S["ema55"]) / c / sg
    F["价格-EMA55"] = (c - S["ema55"]) / c / sg
    F["MA5/20"] = (S["ma5"] - S["ma20"]) / c / sg
    F["MACD 柱"] = S["macd_hist"] / c / sg
    F["MACD 线"] = S["macd"] / c / sg
    F["ADX·DI 差"] = (S["pdi"] - S["mdi"]) * S["adx"] / 100.0 / 25.0
    F["SuperTrend"] = S["st_dir"]
    F["抛物线 SAR"] = S["sar_dir"]
    F["Aroon 振荡"] = S["aroon"] / 50.0
    cloud_top, cloud_bot = np.maximum(S["spana"], S["spanb"]), np.minimum(S["spana"], S["spanb"])
    F["一目均衡云"] = np.where(c > cloud_top, 1.0, np.where(c < cloud_bot, -1.0, 0.0)) + np.sign(S["tenkan"] - S["kijun"]) * 0.5
    F["回归斜率 t"] = np.clip(S["linreg_t"] / 5.0, -3, 3)
    for k in (6, 14, 24):
        F["RSI%d" % k] = (S["rsi%d" % k] - 50.0) / 25.0
    F["StochRSI"] = (S["stochrsi"] - 50.0) / 25.0
    F["KDJ-J"] = (S["j"] - 50.0) / 40.0
    F["KDJ K-D"] = (S["k"] - S["d"]) / 10.0
    F["威廉 %R"] = (S["willr"] + 50.0) / 25.0
    F["CCI20"] = S["cci"] / 100.0
    F["ROC12"] = S["roc12"] / 100.0 / sg
    F["TRIX"] = S["trix"] / 100.0 / sg
    F["布林 %B"] = (S["bb_pctb"] - 0.5) * 2.0
    F["布林带宽变化"] = np.r_[0, np.diff(S["bb_bw"])] / np.maximum(S["bb_bw"], 1e-9)
    F["Keltner 位置"] = _safe(c - (S["kc_up"] + S["kc_lo"]) / 2, S["kc_up"] - S["kc_lo"])
    F["唐奇安位置"] = (S["dc_pos"] - 0.5) * 2.0
    F["Z-score20"] = np.clip(S["zscore20"], -4, 4) / 2.0
    F["MFI14"] = (S["mfi"] - 50.0) / 25.0
    F["CMF20"] = S["cmf"] * 4.0
    F["OBV 动量"] = _safe(S["obv"] - S["obv_ma"], np.maximum(np.abs(S["obv_ma"]), 1.0)) * 10.0
    F["VWAP 偏离"] = (c - S["vwap"]) / c / sg
    F["量比×方向"] = np.sign(S["ret"]) * np.clip(S["volratio"], 0, 4) - np.sign(S["ret"])
    F["力度指数"] = _safe(S["force"], np.maximum(sma(v, 20) * c * sg, 1.0))
    for k, nm in ((1, "1"), (5, "5"), (20, "20")):
        m = np.zeros(len(c))
        if len(c) > k:
            m[k:] = np.log(c[k:] / c[:-k])
        F["动量%s" % nm] = m / (sg * math.sqrt(k))
    for name in list(F):
        F[name] = np.nan_to_num(np.clip(F[name], -6, 6), nan=0.0, posinf=0.0, neginf=0.0)
    return F


# ----------------------------- 最新值 + 规则投票 -----------------------------
def _sg(v):
    return 1 if v > 0 else (-1 if v < 0 else 0)


def compute_all(bars):
    """最新一根 K 线上的全部指标(含规则信号)。bars 至少 60 根。"""
    if len(bars) < 60:
        return {"ok": False, "reason": "K 线不足(%d<60)" % len(bars)}
    S, B = build_series(bars)
    c = B[:, 4]
    i = len(c) - 1
    px = float(c[i])
    L = lambda k: float(S[k][i])  # noqa: E731
    rows = []

    def add(group, name, value, sig, note, fmt="%.2f"):
        rows.append({"g": group, "name": name, "v": (fmt % value) if value is not None else "--", "raw": None if value is None else float(value),
                     "sig": int(sig), "note": note})

    # 趋势
    add("趋势", "MA5/MA20", L("ma5") - L("ma20"), _sg(L("ma5") - L("ma20")), "MA5 在 MA20 之%s" % ("上" if L("ma5") > L("ma20") else "下"), "%+.4f")
    add("趋势", "EMA8/EMA21", L("ema8") - L("ema21"), _sg(L("ema8") - L("ema21")), "短期趋势%s" % ("向上" if L("ema8") > L("ema21") else "向下"), "%+.4f")
    add("趋势", "EMA21/EMA55", L("ema21") - L("ema55"), _sg(L("ema21") - L("ema55")), "中期趋势%s" % ("向上" if L("ema21") > L("ema55") else "向下"), "%+.4f")
    add("趋势", "价格 vs EMA200", px - L("ema200"), _sg(px - L("ema200")), "价格在长期均线%s" % ("上方" if px > L("ema200") else "下方"), "%+.4f")
    add("趋势", "MACD 柱", L("macd_hist"), _sg(L("macd_hist")), "MACD 柱为%s" % ("正" if L("macd_hist") > 0 else "负"), "%+.5f")
    adx = L("adx")
    add("趋势", "ADX(14)", adx, _sg(L("pdi") - L("mdi")) if adx >= 20 else 0, "趋势强度%s(+DI %.0f / -DI %.0f)" % ("强" if adx >= 25 else "弱", L("pdi"), L("mdi")), "%.1f")
    add("趋势", "SuperTrend(10,3)", L("st_line"), L("st_dir"), "SuperTrend 方向%s" % ("多" if L("st_dir") > 0 else "空"), "%.3f")
    add("趋势", "抛物线 SAR", L("sar"), L("sar_dir"), "SAR 在价格%s" % ("下方(多)" if L("sar_dir") > 0 else "上方(空)"), "%.3f")
    add("趋势", "Aroon 振荡", L("aroon"), 1 if L("aroon") > 50 else (-1 if L("aroon") < -50 else 0), "Aroon %.0f" % L("aroon"), "%+.0f")
    ct, cb = max(L("spana"), L("spanb")), min(L("spana"), L("spanb"))
    add("趋势", "一目均衡表", px - (ct if px > ct else cb if px < cb else px), 1 if px > ct else (-1 if px < cb else 0),
        "价格在云%s" % ("上" if px > ct else "下" if px < cb else "中"), "%+.3f")
    add("趋势", "回归斜率 t(30)", L("linreg_t"), 1 if L("linreg_t") > 2 else (-1 if L("linreg_t") < -2 else 0), "对数价格线性回归 t=%.1f" % L("linreg_t"), "%+.1f")
    # 动量 / 摆动
    r6, r14, r24 = L("rsi6"), L("rsi14"), L("rsi24")
    add("动量", "RSI(6)", r6, 1 if r6 < 20 else (-1 if r6 > 80 else 0), "超卖<20 / 超买>80", "%.1f")
    add("动量", "RSI(14)", r14, 1 if r14 < 30 else (-1 if r14 > 70 else 0), "超卖<30 / 超买>70", "%.1f")
    add("动量", "RSI(24)", r24, 1 if r24 < 35 else (-1 if r24 > 65 else 0), "超卖<35 / 超买>65", "%.1f")
    add("动量", "StochRSI", L("stochrsi"), 1 if L("stochrsi") < 15 else (-1 if L("stochrsi") > 85 else 0), "StochRSI", "%.0f")
    j = L("j")
    add("动量", "KDJ(9,3,3) J", j, 1 if j < 0 else (-1 if j > 100 else 0), "K=%.0f D=%.0f J=%.0f" % (L("k"), L("d"), j), "%.0f")
    add("动量", "威廉 %R(14)", L("willr"), 1 if L("willr") < -80 else (-1 if L("willr") > -20 else 0), "%R", "%.0f")
    add("动量", "CCI(20)", L("cci"), 1 if L("cci") < -100 else (-1 if L("cci") > 100 else 0), "CCI", "%.0f")
    add("动量", "ROC(12) %", L("roc12"), _sg(L("roc12")) if abs(L("roc12")) > 0.05 else 0, "12 根涨跌幅", "%+.2f")
    add("动量", "TRIX(15) %", L("trix"), _sg(L("trix")), "TRIX 三重平滑动量", "%+.4f")
    # 波动
    add("波动", "布林 %B", L("bb_pctb"), 1 if L("bb_pctb") < 0.05 else (-1 if L("bb_pctb") > 0.95 else 0), "布林带内位置 %.0f%%" % (L("bb_pctb") * 100), "%.2f")
    add("波动", "布林带宽", L("bb_bw") * 100, 0, "带宽%s(挤压后常有方向性突破)" % ("收窄" if S["bb_bw"][i] < np.percentile(S["bb_bw"][-120:], 20) else "正常"), "%.2f%%")
    add("波动", "Keltner 通道", px - L("kc_up") if px > L("kc_up") else (px - L("kc_lo") if px < L("kc_lo") else 0.0),
        -1 if px > L("kc_up") else (1 if px < L("kc_lo") else 0), "价格%s通道" % ("突破上沿" if px > L("kc_up") else "跌破下沿" if px < L("kc_lo") else "在"), "%+.3f")
    add("波动", "唐奇安(20)位置", L("dc_pos") * 100, 1 if px >= L("dc_up") else (-1 if px <= L("dc_lo") else 0), "20 根区间位置(创新高/新低=突破)", "%.0f%%")
    add("波动", "ATR(14) %", L("atr14") / px * 100, 0, "每根平均真实波幅", "%.3f%%")
    add("波动", "已实现波动(20)", L("rv20") * 100, 0, "20 根收益标准差", "%.3f%%")
    add("波动", "Z-score(20)", L("zscore20"), 1 if L("zscore20") < -2 else (-1 if L("zscore20") > 2 else 0), "偏离 20 根均值的标准差倍数", "%+.2f")
    # 量价
    add("量价", "MFI(14)", L("mfi"), 1 if L("mfi") < 20 else (-1 if L("mfi") > 80 else 0), "资金流量指数", "%.0f")
    add("量价", "CMF(20)", L("cmf"), 1 if L("cmf") > 0.1 else (-1 if L("cmf") < -0.1 else 0), "钱德资金流", "%+.3f")
    add("量价", "OBV 动量", L("obv") - L("obv_ma"), _sg(L("obv") - L("obv_ma")), "OBV 在其 EMA20 之%s" % ("上" if L("obv") > L("obv_ma") else "下"), "%+.0f")
    add("量价", "VWAP 偏离", (px / L("vwap") - 1) * 100, _sg(px - L("vwap")), "价格在当日 VWAP %s" % ("上方" if px > L("vwap") else "下方"), "%+.3f%%")
    add("量价", "量比", L("volratio"), 0, "当前量 / 20 根均量", "%.2f")
    add("量价", "力度指数", L("force"), _sg(L("force")), "价量合力", "%+.0f")

    votes = [r["sig"] for r in rows if r["sig"] != 0]
    bull, bear = sum(1 for x in votes if x > 0), sum(1 for x in votes if x < 0)
    trend_rows = [r["sig"] for r in rows if r["g"] == "趋势"]
    mom_rows = [r["sig"] for r in rows if r["g"] == "动量"]
    return {"ok": True, "n_bars": len(bars), "price": px, "rows": rows,
            "vote": {"bull": bull, "bear": bear, "net": bull - bear, "total": len(rows),
                     "trend_net": sum(trend_rows), "mom_net": sum(mom_rows)}}


# ----------------------------- 样本外学习 -----------------------------
def _corr(a, b):
    if len(a) < 5 or np.std(a) < 1e-12 or np.std(b) < 1e-12:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def learn_weights(bars, H=6, cost_rt=0.0012, min_t=1.5):
    """
    对每个指标:训练段(前 70%)的 IC → 显著(|t|≥min_t)才保留,权重 = IC 的符号 × |IC|(自动判定"顺势"还是"逆势")。
    复合分 = Σ w_i·z_i / Σ|w_i|。测试段(后 30%,中间留 H 根 embargo)不重叠抽样验证复合分的 IC、命中率、做多扣费净期望。
    """
    if len(bars) < 400:
        return {"ok": False, "reason": "K 线不足(%d<400)" % len(bars)}
    S, B = build_series(bars)
    F = feature_dict(S, B)
    c = B[:, 4]
    lc = np.log(c)
    n = len(c)
    warm = 210 if n > 700 else 60
    idx = np.arange(warm, n - H)
    fwd = lc[idx + H] - lc[idx]
    sig = np.maximum(S["rv20"][idx], 1e-6)
    y = np.clip(fwd / (sig * math.sqrt(H)), -6, 6)
    split = int(len(idx) * 0.7)
    tr = np.arange(0, split - H)
    te = np.arange(split, len(idx))
    if len(tr) < 150 or len(te) < 60:
        return {"ok": False, "reason": "可用样本过少"}
    names = list(F)
    # 标准化(仅用训练段统计量,避免泄漏)
    Z = {}
    for k in names:
        x = F[k][idx]
        mu, sd = x[tr].mean(), x[tr].std() + 1e-9
        Z[k] = (x - mu) / sd
    neff = max(10, len(tr) // H)  # 重叠收益的有效样本数
    table, w = [], {}
    for k in names:
        ic = _corr(Z[k][tr], y[tr])
        t = ic * math.sqrt(max(neff - 2, 1)) / math.sqrt(max(1 - ic * ic, 1e-9))
        ic_te = _corr(Z[k][te][::H], y[te][::H])
        keep = abs(t) >= min_t
        if keep:
            w[k] = ic
        table.append({"name": k, "ic_train": ic, "t_train": float(t), "ic_test": ic_te, "kept": bool(keep),
                      "dir": "顺势" if ic > 0 else "逆势"})
    table.sort(key=lambda r: -abs(r["t_train"]))
    out = {"ok": True, "H": H, "n_bars": n, "n_feat": len(names), "n_kept": len(w), "table": table[:24], "composite": None}
    if not w:
        out["reason"] = "训练段没有任何指标达到显著水平(|t|≥%.1f)——本标的当前没有可靠的指标优势" % min_t
        return out
    den = sum(abs(v) for v in w.values())
    comp = sum(w[k] * Z[k] for k in w) / den
    sel = np.arange(0, len(te), H)
    ps, ys, fs = comp[te][sel], y[te][sel], fwd[te][sel]
    ic = _corr(ps, ys)
    ns = len(ps)
    ic_t = ic * math.sqrt(max(ns - 2, 1)) / math.sqrt(max(1 - ic * ic, 1e-9))
    nz = ys != 0
    hit_n = int(nz.sum())
    hit = float((np.sign(ps[nz]) == np.sign(ys[nz])).mean()) if hit_n else None
    thr = 0.5 * float(np.std(comp[tr]))
    lm = ps > thr
    nets = fs[lm] - cost_rt
    # 最新复合分(用全样本标准化统计量保持与训练一致:沿用训练段 mu/sd)
    zl = 0.0
    for k in w:
        x = F[k][idx]
        mu, sd = x[tr].mean(), x[tr].std() + 1e-9
        zl += w[k] * (F[k][-1] - mu) / sd
    zl /= den
    contrib = sorted(((k, float(w[k] * (F[k][-1] - F[k][idx][tr].mean()) / (F[k][idx][tr].std() + 1e-9) / den)) for k in w), key=lambda z: -abs(z[1]))[:5]
    out["composite"] = {
        "now": float(zl), "now_pctile": float((comp[tr] < zl).mean()), "thr": thr,
        "oos_n": int(ns), "oos_ic": ic, "oos_ic_t": float(ic_t), "oos_hit": hit,
        "oos_long_n": int(lm.sum()), "oos_long_net_bp": float(nets.mean() * 1e4) if lm.sum() else 0.0,
        "oos_long_win": float((nets > 0).mean()) if lm.sum() else None,
        "reliable": bool(ic > 0 and hit is not None and hit >= 0.5), "significant": bool(ic_t >= 1.65),
        "contrib": contrib, "weights": {k: float(v) for k, v in sorted(w.items(), key=lambda z: -abs(z[1]))[:12]},
    }
    return out


def summary_text(allr, learned):
    """给 LLM 的紧凑中文摘要(只含真实计算值)。"""
    L = []
    if allr and allr.get("ok"):
        v = allr["vote"]
        L.append("规则投票:看多%d / 看空%d / 共%d 项(趋势组净%+d, 动量组净%+d)" % (v["bull"], v["bear"], v["total"], v["trend_net"], v["mom_net"]))
        strong = [r for r in allr["rows"] if r["sig"] != 0]
        L.append("触发信号:" + ("; ".join("%s%s(%s)" % (r["name"], "多" if r["sig"] > 0 else "空", r["v"]) for r in strong[:14]) or "无"))
        keyv = {r["name"]: r["v"] for r in allr["rows"]}
        L.append("关键读数:RSI14=%s ADX=%s 布林%%B=%s MFI=%s CCI=%s ATR%%=%s VWAP偏离=%s" % (
            keyv.get("RSI(14)"), keyv.get("ADX(14)"), keyv.get("布林 %B"), keyv.get("MFI(14)"), keyv.get("CCI(20)"), keyv.get("ATR(14) %"), keyv.get("VWAP 偏离")))
    if learned and learned.get("ok"):
        cp = learned.get("composite")
        if cp:
            L.append("学习复合分(从%d个指标中保留%d个训练段显著者, 预测未来%d根): 当前%+.2f(历史分位%.0f%%); 样本外IC=%+.3f(t=%.2f) 命中%s 做多扣费净期望%+.1fbp(n=%d,胜率%s); 主要贡献:%s; %s" % (
                learned["n_feat"], learned["n_kept"], learned["H"], cp["now"], cp["now_pctile"] * 100, cp["oos_ic"], cp["oos_ic_t"],
                "--" if cp["oos_hit"] is None else "%.1f%%" % (cp["oos_hit"] * 100), cp["oos_long_net_bp"], cp["oos_long_n"],
                "--" if cp["oos_long_win"] is None else "%.0f%%" % (cp["oos_long_win"] * 100),
                "、".join("%s%+.2f" % (a, b) for a, b in cp["contrib"][:4]),
                "样本外可靠" if cp["reliable"] else "样本外不可靠(不应据此交易)"))
        else:
            L.append("学习复合分:" + learned.get("reason", "不可用"))
    return "\n".join(L)
