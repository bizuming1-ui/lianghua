import { BOLL_K, BOLL_N, MA_FAST, MA_SLOW, RSI_N } from "./config";

export interface Signal {
  ready: boolean;
  warm: number; // 已采集样本数
  need: number; // 预热需要的样本数
  maFast: number | null;
  maSlow: number | null;
  rsi: number | null;
  bollUp: number | null;
  bollMid: number | null;
  bollLow: number | null;
  score: number;
  action: "BUY" | "SELL" | "HOLD";
  reasons: string[];
}

function mean(a: number[]): number {
  let s = 0;
  for (const v of a) s += v;
  return s / a.length;
}

function std(a: number[], m: number): number {
  let s = 0;
  for (const v of a) s += (v - m) * (v - m);
  return Math.sqrt(s / a.length);
}

/** Wilder 之外的简化 RSI:最近 n 个变化量的涨跌幅均值比;无波动时返回 50。 */
export function rsi(prices: number[], n: number): number | null {
  if (prices.length < n + 1) return null;
  const w = prices.slice(prices.length - n - 1);
  let up = 0;
  let dn = 0;
  for (let i = 1; i < w.length; i++) {
    const d = w[i] - w[i - 1];
    if (d > 0) up += d;
    else dn -= d;
  }
  if (up + dn === 0) return 50;
  return (100 * up) / (up + dn);
}

/**
 * 综合评分(-3 ~ +3):
 *  趋势:MA30 相对 MA120 上穿/下穿(±1,偏离 < 0.02% 视为平)
 *  动量:RSI60 < 30 超卖 +1;> 70 超买 -1
 *  通道:价格跌破布林下轨 +1;突破上轨 -1
 * 评分 >= 2 → BUY;<= -1 → SELL;否则 HOLD。
 */
export function analyze(prices: number[]): Signal {
  const need = MA_SLOW;
  const sig: Signal = {
    ready: false,
    warm: prices.length,
    need,
    maFast: null,
    maSlow: null,
    rsi: null,
    bollUp: null,
    bollMid: null,
    bollLow: null,
    score: 0,
    action: "HOLD",
    reasons: [],
  };
  if (prices.length < need) return sig;

  const last = prices[prices.length - 1];
  const maFast = mean(prices.slice(-MA_FAST));
  const maSlow = mean(prices.slice(-MA_SLOW));
  const bw = prices.slice(-BOLL_N);
  const bm = mean(bw);
  const bs = std(bw, bm);
  const r = rsi(prices, RSI_N);

  sig.ready = true;
  sig.maFast = maFast;
  sig.maSlow = maSlow;
  sig.rsi = r;
  sig.bollMid = bm;
  sig.bollUp = bm + BOLL_K * bs;
  sig.bollLow = bm - BOLL_K * bs;

  let score = 0;
  const rel = (maFast - maSlow) / maSlow;
  if (rel > 0.0002) {
    score += 1;
    sig.reasons.push("MA30 在 MA120 之上(趋势向上)");
  } else if (rel < -0.0002) {
    score -= 1;
    sig.reasons.push("MA30 在 MA120 之下(趋势向下)");
  }
  if (r !== null) {
    if (r < 30) {
      score += 1;
      sig.reasons.push("RSI 超卖(<30)");
    } else if (r > 70) {
      score -= 1;
      sig.reasons.push("RSI 超买(>70)");
    }
  }
  if (bs > 0) {
    if (last < sig.bollLow) {
      score += 1;
      sig.reasons.push("价格跌破布林下轨");
    } else if (last > sig.bollUp) {
      score -= 1;
      sig.reasons.push("价格突破布林上轨");
    }
  }
  sig.score = score;
  sig.action = score >= 2 ? "BUY" : score <= -1 ? "SELL" : "HOLD";
  return sig;
}
