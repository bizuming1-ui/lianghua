import type { MarketId } from "./config";

const TZ: Record<MarketId, string> = {
  US: "America/New_York",
  CN: "Asia/Shanghai",
};

export function zonedParts(ts: number, tz: string) {
  const f = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    hourCycle: "h23",
    weekday: "short",
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
    second: "numeric",
  });
  const o: Record<string, string> = {};
  for (const p of f.formatToParts(new Date(ts))) o[p.type] = p.value;
  return {
    y: +o.year,
    m: +o.month,
    d: +o.day,
    hh: +o.hour,
    mm: +o.minute,
    ss: +o.second,
    wd: o.weekday,
  };
}

/** 把"某时区的墙上时间"换算成 UTC 毫秒时间戳(夏令时安全)。 */
export function zonedToMs(
  y: number,
  m: number,
  d: number,
  hh: number,
  mm: number,
  ss: number,
  tz: string,
): number {
  const guess = Date.UTC(y, m - 1, d, hh, mm, ss);
  const p = zonedParts(guess, tz);
  const asUtc = Date.UTC(p.y, p.m - 1, p.d, p.hh, p.mm, p.ss);
  return guess - (asUtc - guess);
}

export type MarketStateId = "open" | "pre" | "post" | "lunch" | "closed";

export const STATE_LABEL: Record<MarketStateId, string> = {
  open: "交易中",
  pre: "盘前",
  post: "盘后",
  lunch: "午休",
  closed: "休市",
};

export function marketState(market: MarketId, now: number): MarketStateId {
  const p = zonedParts(now, TZ[market]);
  if (p.wd === "Sat" || p.wd === "Sun") return "closed";
  const t = p.hh * 60 + p.mm;
  if (market === "US") {
    if (t >= 570 && t < 960) return "open";
    if (t >= 240 && t < 570) return "pre";
    if (t >= 960 && t < 1200) return "post";
    return "closed";
  }
  if ((t >= 570 && t < 690) || (t >= 780 && t < 900)) return "open";
  if (t >= 690 && t < 780) return "lunch";
  if (t >= 555 && t < 570) return "pre";
  return "closed";
}

export { TZ };
