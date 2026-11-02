"""
量化数学内核(numpy)——所有数字都由真实历史K线计算得出,不含任何编造数据。

包含:
  * 波动率:EWMA(RiskMetrics λ=0.94)、ATR(Wilder)
  * 状态识别:Lo-MacKinlay 方差比检验(趋势/均值回归/随机游走)、线性趋势 t 统计量
  * 特征工程:波动率标准化动量、Z-score、Wilder RSI、MACD 柱、区间位置、量比、VWAP 偏离
  * 预测与验证:岭回归 + 70/30 时间切分(含 embargo)+ 不重叠样本外检验(IC、命中率、净期望、t 值、Wilson 区间)
  * 风控:分数凯利 + 单笔风险预算 + 仓位上限
结论原则:样本外没有统计优势 => 不交易。
"""
import math

import numpy as np

SQRT2 = math.sqrt(2.0)


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def _f(x):
    return np.asarray(x, dtype=float)


# ----------------------------- 基础序列函数 -----------------------------
def ema(x, n):
    x = _f(x)
    out = np.empty_like(x)
    if len(x) == 0:
        return out
    a = 2.0 / (n + 1.0)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1.0 - a) * out[i - 1]
    return out


def rolling_mean(x, n):
    x = _f(x)
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def rolling_std(x, n):
    x = _f(x)
    m = rolling_mean(x, n)
    m2 = rolling_mean(x * x, n)
    return np.sqrt(np.maximum(m2 - m * m, 0.0))


def wilder_rsi(c, n=14):
    c = _f(c)
    out = np.full(len(c), 50.0)
    if len(c) <= n:
        return out
    d = np.diff(c, prepend=c[0])
    up, dn = np.maximum(d, 0.0), np.maximum(-d, 0.0)
    au, ad = up[1:n + 1].mean(), dn[1:n + 1].mean()
    for i in range(n, len(c)):
        if i > n:
            au = (au * (n - 1) + up[i]) / n
            ad = (ad * (n - 1) + dn[i]) / n
        if ad == 0:
            out[i] = 50.0 if au == 0 else 100.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + au / ad)
    return out


def atr_last(h, l, c, n=14):
    h, l, c = _f(h), _f(l), _f(c)
    if len(c) < n + 2:
        return float("nan")
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    a = tr[1:n + 1].mean()
    for i in range(n + 1, len(tr)):
        a = (a * (n - 1) + tr[i]) / n
    return float(a)


def ewma_vol_series(r, lam=0.94, floor=1e-5):
    """每根K线收盘时已知的 EWMA 波动率(不含未来信息)。"""
    r = _f(r)
    n = len(r)
    out = np.empty(n)
    v = float(np.var(r[: min(n, 50)])) if n else 0.0
    v = max(v, floor * floor)
    for i in range(n):
        v = lam * v + (1.0 - lam) * r[i] * r[i]
        out[i] = math.sqrt(max(v, floor * floor))
    return out


# ----------------------------- 状态识别 -----------------------------
def variance_ratio(c, q):
    """Lo-MacKinlay 方差比与 z 值(同方差近似)。VR>1 趋势/动量;VR<1 均值回归。"""
    lc = np.log(_f(c))
    r = np.diff(lc)
    T = len(r)
    if T < q * 10:
        return None
    mu = r.mean()
    s1 = ((r - mu) ** 2).sum() / (T - 1)
    if s1 <= 0:
        return None
    rq = lc[q:] - lc[:-q]
    m = q * (T - q + 1) * (1.0 - q / T)
    sq = ((rq - q * mu) ** 2).sum() / m
    vr = sq / s1
    z = (vr - 1.0) / math.sqrt(2.0 * (2 * q - 1) * (q - 1) / (3.0 * q * T))
    return {"q": q, "vr": float(vr), "z": float(z)}


def trend_tstat(c, n=60):
    """对数价格对时间的线性回归:每根K线斜率(%)、t 统计量、R²。"""
    c = _f(c)[-n:]
    if len(c) < 20:
        return None
    y = np.log(c)
    x = np.arange(len(y), dtype=float)
    r = ols(x, y)
    if not r:
        return None
    return {"slope_pct": r["beta"] * 100.0, "t": r["t"], "r2": r["r2"], "n": r["n"]}


def regime_label(vr):
    if not vr:
        return "样本不足"
    if vr["z"] > 1.96:
        return "趋势/动量(方差比显著>1)"
    if vr["z"] < -1.96:
        return "均值回归(方差比显著<1)"
    return "近似随机游走(无显著趋势或回归)"


def ols(x, y):
    x, y = _f(x), _f(y)
    n = len(x)
    if n < 20:
        return None
    xm, ym = x.mean(), y.mean()
    sxx = ((x - xm) ** 2).sum()
    if sxx <= 0:
        return None
    b = ((x - xm) * (y - ym)).sum() / sxx
    a = ym - b * xm
    res = y - a - b * x
    sse = (res ** 2).sum()
    s2 = sse / (n - 2)
    se = math.sqrt(s2 / sxx) if s2 > 0 else 0.0
    sst = ((y - ym) ** 2).sum()
    return {"alpha": float(a), "beta": float(b), "t": float(b / se) if se > 0 else 0.0,
            "r2": float(1 - sse / sst) if sst > 0 else 0.0, "n": int(n), "resid_sd": float(math.sqrt(s2))}


# ----------------------------- 特征工程 -----------------------------
def feature_matrix(bars, kind):
    """bars: [[t_ms,o,h,l,c,v],...]; kind: 'intra'(5分钟) | 'daily'。"""
    B = _f(bars)
    n = len(B)
    t, h, l, c, v = B[:, 0], B[:, 2], B[:, 3], B[:, 4], B[:, 5]
    lc = np.log(c)
    r1 = np.diff(lc, prepend=lc[0])
    sig = ewma_vol_series(r1)
    ks = (1, 6, 24) if kind == "intra" else (1, 5, 20)
    cols, names = [], []
    for k in ks:
        m = np.zeros(n)
        m[k:] = lc[k:] - lc[:-k]
        cols.append(np.clip(m / (sig * math.sqrt(k)), -6, 6))
        names.append("动量%d" % k)
    sma, sd = rolling_mean(c, 20), rolling_std(c, 20)
    z = np.nan_to_num(np.where(sd > 0, (c - sma) / np.where(sd > 0, sd, 1.0), 0.0))
    cols.append(np.clip(z, -4, 4)); names.append("Z20")
    cols.append((wilder_rsi(c, 14) - 50.0) / 25.0); names.append("RSI14")
    macd = ema(c, 12) - ema(c, 26)
    hist = (macd - ema(macd, 9)) / (c * sig)
    cols.append(np.clip(hist, -6, 6)); names.append("MACD柱")
    rng = h - l
    cols.append(np.where(rng > 0, (c - l) / np.where(rng > 0, rng, 1.0) - 0.5, 0.0)); names.append("区间位置")
    vm = rolling_mean(v, 20)
    vr = np.nan_to_num(np.where(vm > 0, np.log((v + 1.0) / (vm + 1.0)), 0.0))
    cols.append(np.clip(vr, -2, 2)); names.append("量比")
    if kind == "intra":
        day = np.cumsum(np.r_[0, (np.diff(t) > 4 * 3600 * 1000).astype(int)])
        tp = (h + l + c) / 3.0 * v
        vw = c.copy()
        for d in np.unique(day):
            m = day == d
            cv, cp = np.cumsum(v[m]), np.cumsum(tp[m])
            vw[m] = np.where(cv > 0, cp / np.where(cv > 0, cv, 1.0), c[m])
        dev = np.nan_to_num((c - vw) / (c * sig))
        cols.append(np.clip(dev, -6, 6)); names.append("VWAP偏离")
    return np.column_stack(cols), names, c, sig, t


def ridge(Z, y, lam):
    ym = y.mean()
    k = Z.shape[1]
    w = np.linalg.solve(Z.T @ Z + lam * np.eye(k), Z.T @ (y - ym))
    return w, ym


def _rank(a):
    return np.argsort(np.argsort(a)).astype(float)


def _corr(a, b):
    if len(a) < 5 or np.std(a) == 0 or np.std(b) == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def wilson_low(k, n, z=1.645):
    if n == 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    s = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - s) / d


def edge_study(bars, kind, H, cost_rt, label):
    """
    在真实历史上做"预测 H 根K线后收益"的岭回归,并用严格的时间外样本检验可靠性。
    cost_rt: 往返成本(小数,如 0.0008)。
    """
    out = {"label": label, "H": H, "n_bars": len(bars), "ok": False}
    need = 500 if kind == "intra" else 150
    if len(bars) < need:
        out["reason"] = "历史样本不足(%d<%d)" % (len(bars), need)
        return out
    X, names, c, sig, t = feature_matrix(bars, kind)
    n = len(c)
    lc = np.log(c)
    warm = 60 if kind == "intra" else 30
    idx = np.arange(warm, n - H)
    fwd = lc[idx + H] - lc[idx]
    y = np.clip(fwd / (sig[idx] * math.sqrt(H)), -6, 6)
    Xi = X[idx]
    split = int(len(idx) * 0.7)
    tr = np.arange(0, split - H)  # embargo:训练集末尾去掉 H 根,避免与检验集标签重叠
    te = np.arange(split, len(idx))
    if len(tr) < 150 or len(te) < 60:
        out["reason"] = "可用样本过少"
        return out
    mu, sd = Xi[tr].mean(0), Xi[tr].std(0) + 1e-9
    Z = (Xi - mu) / sd
    w, b = ridge(Z[tr], y[tr], 0.1 * len(tr))
    pred = Z[te] @ w + b
    sel = np.arange(0, len(te), H)  # 不重叠抽样,t 值不被重叠样本虚高
    ps, ys, fs = pred[sel], y[sel], fwd[te][sel]
    nz = ys != 0
    ic = _corr(ps, ys)
    ric = _corr(_rank(ps), _rank(ys))
    ns = len(ps)
    ic_t = ic * math.sqrt(max(ns - 2, 1)) / math.sqrt(max(1 - ic * ic, 1e-9))
    hit_n = int(nz.sum())
    hit_k = int((np.sign(ps[nz]) == np.sign(ys[nz])).sum()) if hit_n else 0
    thr = 0.5 * float(np.std(Z[tr] @ w))
    lm = ps > thr
    nets = fs[lm] - cost_rt
    n_long = int(lm.sum())
    long_mean = float(nets.mean()) if n_long else 0.0
    long_t = float(nets.mean() / (nets.std(ddof=1) / math.sqrt(n_long))) if n_long > 2 and nets.std(ddof=1) > 0 else 0.0
    win_k = int((nets > 0).sum()) if n_long else 0
    # 全样本重训练用于当前预测
    mu2, sd2 = Xi.mean(0), Xi.std(0) + 1e-9
    Za = (Xi - mu2) / sd2
    wa, ba = ridge(Za, y, 0.1 * len(y))
    fit = Za @ wa + ba
    sres = float(np.std(y - fit)) or 1.0
    zl = (X[-1] - mu2) / sd2
    contrib = zl * wa
    py = float(np.clip(zl @ wa + ba, -1.5, 1.5))
    sig_last = float(sig[-1])
    mu_h = py * sig_last * math.sqrt(H)
    out.update({
        "ok": True,
        "oos": {"n": int(ns), "ic": ic, "rank_ic": ric, "ic_t": float(ic_t),
                "hit": (hit_k / hit_n) if hit_n else None, "hit_n": hit_n,
                "hit_low90": wilson_low(hit_k, hit_n) if hit_n else None,
                "base_up": float((fs > 0).mean()),
                "n_long": n_long, "long_net_bp": long_mean * 1e4, "long_t": long_t,
                "long_win": (win_k / n_long) if n_long else None,
                "train_n": int(len(tr)), "test_from_ms": int(t[idx[te[0]]])},
        "live": {"pred_z": py, "mu_h_bp": mu_h * 1e4, "sigma_h_bp": sig_last * math.sqrt(H) * 1e4,
                 "sigma_bar_bp": sig_last * 1e4, "p_up": norm_cdf(py / sres),
                 "contrib": sorted([(names[i], float(contrib[i])) for i in range(len(names))], key=lambda z_: -abs(z_[1]))[:4],
                 "feat": {names[i]: float(X[-1][i]) for i in range(len(names))}},
        "reliable": bool(ic > 0 and hit_n > 0 and hit_k / hit_n >= 0.5),
        "significant": bool(ic_t >= 1.65),
        "cost_rt_bp": cost_rt * 1e4,
    })
    return out


# ----------------------------- 风控 -----------------------------
def kelly_fraction(p, b):
    """b = 盈亏比(目标/止损)。f* = p - (1-p)/b。"""
    if b <= 0:
        return 0.0
    return max(0.0, p - (1.0 - p) / b)


def size_fraction(p_win, target_pct, stop_pct, risk_budget=0.01, kelly_mult=0.25, cap=0.4):
    """仓位占权益比例 = min(上限, 1/4 凯利, 单笔风险预算/止损幅度)。"""
    if stop_pct <= 0 or target_pct <= 0:
        return 0.0
    k = kelly_fraction(p_win, target_pct / stop_pct) * kelly_mult
    r = risk_budget / stop_pct
    return float(max(0.0, min(cap, k, r)))


def corr_aligned(a, b):
    """两个 [(t,c)] 序列按时间戳对齐后的收益相关系数。"""
    da = {t: c for t, c in a}
    xs, ys = [], []
    prev = None
    for t, c in b:
        if t in da:
            if prev is not None:
                xs.append(math.log(da[t] / prev[0]))
                ys.append(math.log(c / prev[1]))
            prev = (da[t], c)
    if len(xs) < 30:
        return None, len(xs)
    return _corr(np.array(xs), np.array(ys)), len(xs)
