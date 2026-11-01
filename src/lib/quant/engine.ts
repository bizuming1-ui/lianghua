import { desc, eq, sql } from "drizzle-orm";
import { db } from "@/db";
import { account, positions, trades } from "@/db/schema";
import {
  AUTO_COOLDOWN_MS,
  AUTO_CASH_FRACTION,
  DEFAULT_FX,
  FEE_RATE,
  INITIAL_CAPITAL,
  POLL_MS,
  STOP_LOSS,
  SYMBOLS,
  TAKE_PROFIT,
  TICK_MAX,
  type SymbolDef,
} from "./config";
import { fetchSina, fetchTencent, fetchYahooFx, type FeedResult } from "./feeds";
import { analyze, type Signal } from "./indicators";
import { marketState, STATE_LABEL } from "./market";

type FeedName = "sina" | "tencent" | "sim" | "stale";

interface Quote {
  code: string;
  price: number;
  prevClose: number;
  open: number;
  high: number;
  low: number;
  quoteTime: number;
  feed: FeedName;
  lastOk: number;
}

interface EngineState {
  epoch: number;
  timer: ReturnType<typeof setInterval> | null;
  inFlight: boolean;
  ticks: Record<string, [number, number][]>;
  quotes: Record<string, Quote>;
  signals: Record<string, Signal>;
  fx: { rate: number; src: "sina" | "yahoo" | "default"; at: number };
  feed: { name: string; ok: boolean; latency: number; lastOkAt: number; error: string };
  auto: boolean;
  lastAuto: Record<string, number>;
  lock: Promise<unknown>;
  booted: boolean;
}

const g = globalThis as typeof globalThis & { __quantEngine?: EngineState };

function engine(): EngineState {
  if (!g.__quantEngine) {
    g.__quantEngine = {
      epoch: Date.now(),
      timer: null,
      inFlight: false,
      ticks: {},
      quotes: {},
      signals: {},
      fx: { rate: DEFAULT_FX, src: "default", at: 0 },
      feed: { name: "-", ok: false, latency: 0, lastOkAt: 0, error: "starting" },
      auto: false,
      lastAuto: {},
      lock: Promise.resolve(),
      booted: false,
    };
  }
  return g.__quantEngine;
}

const r2 = (v: number) => Math.round(v * 100) / 100;
const r4 = (v: number) => Math.round(v * 10000) / 10000;

function withLock<T>(fn: () => Promise<T>): Promise<T> {
  const e = engine();
  const run = e.lock.then(
    () => fn(),
    () => fn(),
  );
  e.lock = run.catch(() => undefined);
  return run;
}

/* ------------------------------ 账户 ------------------------------ */

export async function ensureAccount() {
  await db
    .insert(account)
    .values({ id: 1, cash: INITIAL_CAPITAL, initialCapital: INITIAL_CAPITAL })
    .onConflictDoNothing();
  const [a] = await db.select().from(account).where(eq(account.id, 1));
  engine().auto = a?.autoEnabled ?? false;
}

export async function setAuto(enabled: boolean) {
  await ensureAccount();
  await db.update(account).set({ autoEnabled: enabled }).where(eq(account.id, 1));
  engine().auto = enabled;
}

export async function resetAccount() {
  return withLock(async () => {
    await ensureAccount();
    await db.transaction(async (tx) => {
      await tx.delete(trades);
      await tx.delete(positions);
      await tx
        .update(account)
        .set({ cash: INITIAL_CAPITAL, initialCapital: INITIAL_CAPITAL, autoEnabled: false, startedAt: new Date() })
        .where(eq(account.id, 1));
    });
    engine().auto = false;
    engine().lastAuto = {};
  });
}

/* ------------------------------ 行情轮询 ------------------------------ */

function applyReal(sym: SymbolDef, raw: FeedResult["quotes"][string], now: number) {
  const e = engine();
  const prev = e.quotes[sym.code];
  if (prev && prev.feed === "sim") e.ticks[sym.code] = []; // 丢弃模拟阶段的采样
  e.quotes[sym.code] = {
    code: sym.code,
    price: raw.price,
    prevClose: raw.prevClose,
    open: raw.open,
    high: raw.high,
    low: raw.low,
    quoteTime: raw.quoteTime,
    feed: raw.feed,
    lastOk: now,
  };
}

function applyFallback(sym: SymbolDef, now: number) {
  const e = engine();
  const q = e.quotes[sym.code];
  if (q && q.feed !== "sim") {
    // 曾经拿到过真实行情:保持最后价,仅标记"延迟",绝不伪造数据
    if (now - q.lastOk > 10_000) q.feed = "stale";
    return;
  }
  // 从未拿到真实行情(例如无外网):使用显式标注的"模拟行情",自动策略会被禁用
  const base = q?.price ?? sym.base;
  const price = base * (1 + (Math.random() - 0.5) * 0.0012);
  e.quotes[sym.code] = {
    code: sym.code,
    price: Math.round(price * 10 ** sym.decimals) / 10 ** sym.decimals,
    prevClose: q?.prevClose ?? sym.base,
    open: q?.open ?? sym.base,
    high: Math.max(q?.high ?? price, price),
    low: Math.min(q?.low ?? price, price),
    quoteTime: now,
    feed: "sim",
    lastOk: now,
  };
}

async function poll() {
  const e = engine();
  if (e.inFlight) return;
  e.inFlight = true;
  const t0 = Date.now();
  try {
    let res: FeedResult | null = null;
    let name = "sina";
    let err = "";
    try {
      res = await fetchSina();
    } catch (ex) {
      err = `sina: ${(ex as Error).message}`;
    }
    const missing = () => SYMBOLS.filter((s) => !res?.quotes[s.code]);
    if (!res || missing().length > 0) {
      try {
        const t = await fetchTencent();
        if (!res) {
          res = t;
          name = "tencent";
        } else {
          for (const s of missing()) if (t.quotes[s.code]) res.quotes[s.code] = t.quotes[s.code];
          name = "sina+tencent";
        }
      } catch (ex) {
        err += ` tencent: ${(ex as Error).message}`;
      }
    }
    const now = Date.now();
    const gotAny = !!res && Object.keys(res.quotes).length > 0;
    e.feed = gotAny
      ? { name, ok: true, latency: now - t0, lastOkAt: now, error: "" }
      : { ...e.feed, name, ok: false, latency: now - t0, error: err || "no data" };

    // 汇率:新浪随行情返回;缺失时每分钟尝试一次 Yahoo
    if (res?.fx) e.fx = { rate: res.fx, src: "sina", at: now };
    else if (now - e.fx.at > 60_000) {
      e.fx.at = now;
      const y = await fetchYahooFx().catch(() => null);
      if (y) e.fx = { rate: y, src: "yahoo", at: now };
    }

    const sec = Math.floor(now / 1000) * 1000;
    for (const s of SYMBOLS) {
      const raw = res?.quotes[s.code];
      if (raw) applyReal(s, raw, now);
      else applyFallback(s, now);
      const q = e.quotes[s.code];
      const arr = (e.ticks[s.code] ??= []);
      if (arr.length === 0 || arr[arr.length - 1][0] < sec) {
        arr.push([sec, q.price]);
        if (arr.length > TICK_MAX) arr.splice(0, arr.length - TICK_MAX);
        e.signals[s.code] = analyze(arr.map((x) => x[1]));
      }
    }
    if (e.auto) await withLock(runStrategy);
  } catch {
    /* 轮询失败不应崩溃进程 */
  } finally {
    e.inFlight = false;
  }
}

export function ensureStarted() {
  const e = engine();
  if (e.timer) return;
  e.timer = setInterval(() => void poll(), POLL_MS);
  void poll();
  if (!e.booted) {
    e.booted = true;
    void ensureAccount().catch(() => undefined);
  }
}

/* ------------------------------ 成交 ------------------------------ */

export interface TradeResult {
  ok: boolean;
  error?: string;
  trade?: TradeRow;
}

export interface TradeRow {
  id: number;
  ts: number;
  symbol: string;
  side: string;
  qty: number;
  price: number;
  currency: string;
  fx: number;
  amountCny: number;
  feeCny: number;
  pnlCny: number | null;
  pnlPct: number | null;
  source: string;
  feed: string;
  note: string;
}

function fxFor(sym: SymbolDef): number {
  return sym.currency === "CNY" ? 1 : engine().fx.rate;
}

async function execute(code: string, side: "BUY" | "SELL", qty: number, source: "manual" | "auto"): Promise<TradeResult> {
  const sym = SYMBOLS.find((s) => s.code === code);
  if (!sym) return { ok: false, error: "未知标的" };
  if (!Number.isInteger(qty) || qty <= 0) return { ok: false, error: "数量必须是正整数" };
  if (qty % sym.lot !== 0) return { ok: false, error: `${sym.code} 最小交易单位为 ${sym.lot}` };
  const q = engine().quotes[code];
  if (!q || q.price <= 0) return { ok: false, error: "暂无行情,无法成交" };

  await ensureAccount();
  const price = q.price;
  const fx = fxFor(sym);
  const amount = r2(price * qty * fx);
  const fee = r2(amount * FEE_RATE);
  const now = Date.now();
  const notes: string[] = [];
  const st = marketState(sym.market, now);
  if (st !== "open") notes.push(`${STATE_LABEL[st]}·按最近价成交`);
  if (q.feed === "sim") notes.push("模拟行情");
  if (q.feed === "stale") notes.push("行情延迟");

  return db.transaction(async (tx) => {
    const [acc] = await tx.select().from(account).where(eq(account.id, 1));
    const [pos] = await tx.select().from(positions).where(eq(positions.symbol, code));
    const curQty = pos?.qty ?? 0;
    const curCost = pos?.costCny ?? 0;
    let pnl: number | null = null;
    let pnlPct: number | null = null;

    if (side === "BUY") {
      const need = r2(amount + fee);
      if (acc.cash + 1e-9 < need) return { ok: false, error: `资金不足:需要 ¥${need},可用 ¥${r2(acc.cash)}` };
      await tx.update(account).set({ cash: r4(acc.cash - need) }).where(eq(account.id, 1));
      await tx
        .insert(positions)
        .values({ symbol: code, qty: curQty + qty, costCny: r4(curCost + need) })
        .onConflictDoUpdate({ target: positions.symbol, set: { qty: curQty + qty, costCny: r4(curCost + need) } });
    } else {
      if (curQty < qty) return { ok: false, error: `持仓不足:持有 ${curQty},欲卖 ${qty}` };
      const proceeds = r2(amount - fee);
      const costPart = curCost * (qty / curQty);
      pnl = r2(proceeds - costPart);
      pnlPct = costPart > 0 ? pnl / costPart : 0;
      await tx.update(account).set({ cash: r4(acc.cash + proceeds) }).where(eq(account.id, 1));
      await tx
        .update(positions)
        .set({ qty: curQty - qty, costCny: curQty - qty === 0 ? 0 : r4(curCost - costPart) })
        .where(eq(positions.symbol, code));
    }

    const [row] = await tx
      .insert(trades)
      .values({
        ts: new Date(now),
        symbol: code,
        side,
        qty,
        price,
        currency: sym.currency,
        fx,
        amountCny: amount,
        feeCny: fee,
        pnlCny: pnl,
        pnlPct,
        source,
        feed: q.feed,
        note: notes.join(";"),
      })
      .returning();
    return { ok: true, trade: toRow(row) };
  });
}

export function placeOrder(code: string, side: "BUY" | "SELL", qty: number, source: "manual" | "auto" = "manual") {
  return withLock(() => execute(code, side, qty, source));
}

/** 自动策略:仅在"交易中 + 真实行情(非模拟/延迟)+ 指标已预热"时运行。 */
async function runStrategy() {
  const e = engine();
  await ensureAccount();
  const [acc] = await db.select().from(account).where(eq(account.id, 1));
  const poss = await db.select().from(positions);
  const now = Date.now();
  let cash = acc.cash;
  for (const s of SYMBOLS) {
    const q = e.quotes[s.code];
    const sig = e.signals[s.code];
    if (!q || !sig?.ready) continue;
    if (q.feed !== "sina" && q.feed !== "tencent") continue;
    if (marketState(s.market, now) !== "open") continue;
    if (now - (e.lastAuto[s.code] ?? 0) < AUTO_COOLDOWN_MS) continue;
    const fx = fxFor(s);
    const pos = poss.find((p) => p.symbol === s.code);
    if (pos && pos.qty > 0) {
      const mv = q.price * fx * pos.qty;
      const ret = pos.costCny > 0 ? (mv - pos.costCny) / pos.costCny : 0;
      if (ret >= TAKE_PROFIT || ret <= -STOP_LOSS || sig.action === "SELL") {
        const r = await execute(s.code, "SELL", pos.qty, "auto");
        if (r.ok) {
          e.lastAuto[s.code] = now;
          cash += r.trade?.amountCny ?? 0;
        }
      }
    } else if (sig.action === "BUY") {
      const unit = q.price * fx * (1 + FEE_RATE) * s.lot;
      const lots = Math.floor((cash * AUTO_CASH_FRACTION) / unit);
      if (lots >= 1) {
        const r = await execute(s.code, "BUY", lots * s.lot, "auto");
        if (r.ok) {
          e.lastAuto[s.code] = now;
          cash -= (r.trade?.amountCny ?? 0) + (r.trade?.feeCny ?? 0);
        }
      }
    }
  }
}

/* ------------------------------ 查询 ------------------------------ */

function toRow(r: typeof trades.$inferSelect): TradeRow {
  return {
    id: r.id,
    ts: r.ts.getTime(),
    symbol: r.symbol,
    side: r.side,
    qty: r.qty,
    price: r.price,
    currency: r.currency,
    fx: r.fx,
    amountCny: r.amountCny,
    feeCny: r.feeCny,
    pnlCny: r.pnlCny,
    pnlPct: r.pnlPct,
    source: r.source,
    feed: r.feed,
    note: r.note,
  };
}

export async function listTrades(offset: number, limit: number) {
  const rows = await db.select().from(trades).orderBy(desc(trades.id)).limit(limit).offset(offset);
  const [c] = await db.select({ n: sql<number>`count(*)`.mapWith(Number) }).from(trades);
  return { total: c?.n ?? 0, rows: rows.map(toRow) };
}

export async function buildState(since: number) {
  ensureStarted();
  const e = engine();
  const now = Date.now();
  await ensureAccount();
  const [acc] = await db.select().from(account).where(eq(account.id, 1));
  const poss = await db.select().from(positions);
  const [st] = await db
    .select({
      n: sql<number>`count(*)`.mapWith(Number),
      buys: sql<number>`count(*) filter (where ${trades.side} = 'BUY')`.mapWith(Number),
      sells: sql<number>`count(*) filter (where ${trades.side} = 'SELL')`.mapWith(Number),
      wins: sql<number>`count(*) filter (where ${trades.side} = 'SELL' and ${trades.pnlCny} > 0)`.mapWith(Number),
      realized: sql<number>`coalesce(sum(${trades.pnlCny}), 0)`.mapWith(Number),
      fees: sql<number>`coalesce(sum(${trades.feeCny}), 0)`.mapWith(Number),
    })
    .from(trades);
  const recent = (await db.select().from(trades).orderBy(desc(trades.id)).limit(40)).map(toRow);

  const quotes = SYMBOLS.map((s) => {
    const q = e.quotes[s.code];
    const state = marketState(s.market, now);
    return {
      code: s.code,
      name: s.name,
      short: s.short,
      market: s.market,
      currency: s.currency,
      lot: s.lot,
      decimals: s.decimals,
      price: q?.price ?? null,
      prevClose: q?.prevClose ?? null,
      open: q?.open ?? null,
      high: q?.high ?? null,
      low: q?.low ?? null,
      change: q ? q.price - q.prevClose : null,
      pct: q && q.prevClose > 0 ? (q.price / q.prevClose - 1) * 100 : null,
      quoteTime: q?.quoteTime ?? null,
      feed: q?.feed ?? "none",
      state,
      stateLabel: STATE_LABEL[state],
    };
  });

  let mv = 0;
  let cost = 0;
  const positionsOut = poss
    .filter((p) => p.qty > 0)
    .map((p) => {
      const s = SYMBOLS.find((x) => x.code === p.symbol)!;
      const q = e.quotes[p.symbol];
      const fx = fxFor(s);
      const value = q ? q.price * fx * p.qty : 0;
      mv += value;
      cost += p.costCny;
      return {
        symbol: p.symbol,
        qty: p.qty,
        costCny: p.costCny,
        avgPrice: p.costCny / (p.qty * fx),
        mvCny: value,
        pnlCny: value - p.costCny,
        pnlPct: p.costCny > 0 ? (value - p.costCny) / p.costCny : 0,
      };
    });
  const equity = acc.cash + mv;

  const ticks: Record<string, [number, number][]> = {};
  for (const s of SYMBOLS) ticks[s.code] = (e.ticks[s.code] ?? []).filter((t) => t[0] > since);

  return {
    ts: now,
    epoch: e.epoch,
    quotes,
    ticks,
    signals: e.signals,
    fx: e.fx,
    feed: e.feed,
    account: {
      cash: acc.cash,
      initial: acc.initialCapital,
      equity,
      marketValue: mv,
      unrealized: mv - cost,
      realized: st.realized,
      fees: st.fees,
      returnPct: ((equity / acc.initialCapital) - 1) * 100,
      auto: acc.autoEnabled,
      startedAt: acc.startedAt.getTime(),
    },
    positions: positionsOut,
    stats: {
      trades: st.n,
      buys: st.buys,
      sells: st.sells,
      wins: st.wins,
      losses: st.sells - st.wins,
      winRate: st.sells > 0 ? (st.wins / st.sells) * 100 : null,
    },
    trades: recent,
  };
}
