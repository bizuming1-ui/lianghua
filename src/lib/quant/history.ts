// 历史 K 线(图表拖拽/缩放用):Yahoo 为主,失败回退东方财富。与 kaggle/ai_engine.py 的数据口径一致。
export type Bar = [number, number, number, number, number, number]; // t_ms,o,h,l,c,v

const TF: Record<string, { yh: [string, string]; em: [number, number]; ttl: number }> = {
  "1m": { yh: ["1m", "5d"], em: [1, 2400], ttl: 15_000 },
  "5m": { yh: ["5m", "60d"], em: [5, 3000], ttl: 45_000 },
  "15m": { yh: ["15m", "60d"], em: [15, 2000], ttl: 90_000 },
  "1d": { yh: ["1d", "2y"], em: [101, 600], ttl: 900_000 },
};
const MAP: Record<string, { yahoo?: string; em?: string }> = {
  IBB: { yahoo: "IBB", em: "105.IBB" },
  XBI: { yahoo: "XBI", em: "107.XBI" },
  "159502": { em: "0.159502" },
};

const cache = new Map<string, { at: number; bars: Bar[]; src: string }>();
let yahooBlock = 0;

async function getJson(url: string): Promise<unknown> {
  const r = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0" }, cache: "no-store", signal: AbortSignal.timeout(8000) });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function yahoo(sym: string, interval: string, range: string): Promise<Bar[]> {
  if (Date.now() < yahooBlock) throw new Error("yahoo cooldown");
  let err: unknown = new Error("yahoo");
  for (const host of ["query2", "query1"]) {
    try {
      const j = (await getJson(
        `https://${host}.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(sym)}?interval=${interval}&range=${range}&includePrePost=false`,
      )) as { chart: { result: { timestamp?: number[]; indicators: { quote: Record<string, (number | null)[]>[] } }[] } };
      const res = j.chart.result[0];
      const q = res.indicators.quote[0];
      const out: Bar[] = [];
      (res.timestamp ?? []).forEach((t, i) => {
        const o = q.open[i], h = q.high[i], l = q.low[i], c = q.close[i];
        if (o == null || h == null || l == null || c == null) return;
        out.push([t * 1000, o, h, l, c, q.volume[i] ?? 0]);
      });
      return out;
    } catch (e) {
      err = e;
    }
  }
  yahooBlock = Date.now() + 120_000;
  throw err;
}

async function eastmoney(secid: string, klt: number, lmt: number): Promise<Bar[]> {
  const j = (await getJson(
    `https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=${secid}&klt=${klt}&fqt=1&lmt=${lmt}&end=20500101&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57`,
  )) as { data: { klines: string[] } | null };
  const out: Bar[] = [];
  for (const line of j.data?.klines ?? []) {
    const p = line.split(",");
    let t: number;
    if (p[0].length > 10) t = Date.parse(p[0].replace(" ", "T") + ":00+08:00") - klt * 60_000; // 东财分钟线时间为结束时刻
    else t = Date.parse(p[0] + "T12:00:00Z"); // 日线取 12:00 UTC,北京/纽约换算日期一致
    out.push([t, +p[1], +p[3], +p[4], +p[2], +p[5]]);
  }
  return out;
}

export async function historyBars(code: string, tf: string) {
  const m = MAP[code];
  const c = TF[tf];
  if (!m || !c) throw new Error("bad code/tf");
  const key = `${code}:${tf}`;
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < c.ttl) return { code, tf, bars: hit.bars, src: hit.src, ok: true, err: "" };
  let bars: Bar[] = [];
  let src = "";
  let err = "";
  if (m.yahoo) {
    try {
      bars = await yahoo(m.yahoo, ...c.yh);
      src = "yahoo";
    } catch (e) {
      err = (e as Error).message;
    }
  }
  if (!bars.length && m.em) {
    try {
      bars = await eastmoney(m.em, ...c.em);
      src = "eastmoney";
    } catch (e) {
      err = (e as Error).message;
    }
  }
  if (bars.length) {
    cache.set(key, { at: Date.now(), bars, src });
    return { code, tf, bars, src, ok: true, err: "" };
  }
  return { code, tf, bars: hit?.bars ?? [], src: hit?.src ?? "", ok: false, err: err || "empty" };
}
