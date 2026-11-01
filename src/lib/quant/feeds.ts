import { SYMBOLS } from "./config";
import { marketState, zonedToMs } from "./market";

export interface RawQuote {
  code: string;
  price: number;
  prevClose: number;
  open: number;
  high: number;
  low: number;
  quoteTime: number; // 交易所最近一次行情时间(ms)
  feed: "sina" | "tencent";
}

export interface FeedResult {
  quotes: Record<string, RawQuote>;
  fx: number | null;
}

const TIMEOUT = 2500;

async function getText(url: string, headers: Record<string, string>): Promise<string> {
  const res = await fetch(url, {
    headers: { "User-Agent": "Mozilla/5.0 (quant-terminal)", ...headers },
    cache: "no-store",
    signal: AbortSignal.timeout(TIMEOUT),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const buf = await res.arrayBuffer();
  return new TextDecoder("gb18030").decode(buf);
}

const num = (s: string | undefined) => {
  const v = parseFloat(s ?? "");
  return Number.isFinite(v) ? v : 0;
};

/** 新浪行情:美股 gb_xxx、A 股 szXXXXXX、汇率 fx_susdcny,一次请求全部返回。 */
export async function fetchSina(): Promise<FeedResult> {
  const list = [...SYMBOLS.map((s) => s.sina), "fx_susdcny"].join(",");
  const text = await getText(`https://hq.sinajs.cn/list=${list}`, {
    Referer: "https://finance.sina.com.cn",
  });
  const map: Record<string, string[]> = {};
  for (const m of text.matchAll(/hq_str_(\w+)="([^"]*)"/g)) map[m[1]] = m[2].split(",");

  const now = Date.now();
  const quotes: Record<string, RawQuote> = {};
  for (const s of SYMBOLS) {
    const f = map[s.sina];
    if (!f || f.length < 10) continue;
    if (s.market === "US") {
      let price = num(f[1]);
      const prev = num(f[26]);
      // 盘前/盘后用扩展时段价(新浪 f[21]),其余时段用常规价
      const st = marketState("US", now);
      const ext = num(f[21]);
      if ((st === "pre" || st === "post") && ext > 0) price = ext;
      const t = Date.parse((f[3] ?? "").replace(" ", "T") + "+08:00");
      if (price <= 0) continue;
      quotes[s.code] = {
        code: s.code,
        price,
        prevClose: prev > 0 ? prev : price,
        open: num(f[5]) || price,
        high: num(f[6]) || price,
        low: num(f[7]) || price,
        quoteTime: Number.isFinite(t) ? t : now,
        feed: "sina",
      };
    } else {
      let price = num(f[3]);
      const prev = num(f[2]);
      if (price <= 0) price = prev; // 集合竞价前现价为 0
      if (price <= 0) continue;
      const t = Date.parse(`${f[30]}T${f[31]}+08:00`);
      quotes[s.code] = {
        code: s.code,
        price,
        prevClose: prev > 0 ? prev : price,
        open: num(f[1]) || price,
        high: num(f[4]) || price,
        low: num(f[5]) || price,
        quoteTime: Number.isFinite(t) ? t : now,
        feed: "sina",
      };
    }
  }
  const fxRow = map["fx_susdcny"];
  const fx = fxRow && num(fxRow[1]) > 5 ? num(fxRow[1]) : null;
  return { quotes, fx };
}

/** 腾讯行情(备用源)。US 时间为美东墙上时间,A 股为北京时间。 */
export async function fetchTencent(): Promise<FeedResult> {
  const text = await getText(`https://qt.gtimg.cn/q=${SYMBOLS.map((s) => s.tencent).join(",")}`, {
    Referer: "https://gu.qq.com",
  });
  const quotes: Record<string, RawQuote> = {};
  const now = Date.now();
  for (const s of SYMBOLS) {
    const m = text.match(new RegExp(`v_${s.tencent}="([^"]*)"`));
    if (!m) continue;
    const f = m[1].split("~");
    const price = num(f[3]);
    if (price <= 0) continue;
    const ts = f[30] ?? "";
    let t = now;
    const a = ts.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/);
    const b = ts.match(/^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$/);
    const mm = a ?? b;
    if (mm) {
      const tz = s.market === "US" ? "America/New_York" : "Asia/Shanghai";
      t = zonedToMs(+mm[1], +mm[2], +mm[3], +mm[4], +mm[5], +mm[6], tz);
    }
    quotes[s.code] = {
      code: s.code,
      price,
      prevClose: num(f[4]) || price,
      open: num(f[5]) || price,
      high: num(f[33]) || price,
      low: num(f[34]) || price,
      quoteTime: t,
      feed: "tencent",
    };
  }
  return { quotes, fx: null };
}

/** 汇率备用源:Yahoo CNY=X。 */
export async function fetchYahooFx(): Promise<number | null> {
  const res = await fetch("https://query1.finance.yahoo.com/v8/finance/chart/CNY=X?interval=1m&range=1d", {
    headers: { "User-Agent": "Mozilla/5.0" },
    cache: "no-store",
    signal: AbortSignal.timeout(TIMEOUT),
  });
  if (!res.ok) return null;
  const j = (await res.json()) as { chart?: { result?: { meta?: { regularMarketPrice?: number } }[] } };
  const p = j.chart?.result?.[0]?.meta?.regularMarketPrice;
  return typeof p === "number" && p > 5 ? p : null;
}
