import {
  boolean,
  doublePrecision,
  integer,
  pgTable,
  serial,
  text,
  timestamp,
} from "drizzle-orm/pg-core";

/** 模拟账户(单账户,id 固定为 1)。所有金额统一折算为人民币元。 */
export const account = pgTable("account", {
  id: integer("id").primaryKey(),
  cash: doublePrecision("cash").notNull(),
  initialCapital: doublePrecision("initial_capital").notNull(),
  autoEnabled: boolean("auto_enabled").notNull().default(false),
  startedAt: timestamp("started_at", { withTimezone: true }).notNull().defaultNow(),
});

/** 当前持仓。costCny = 含手续费的人民币总成本。 */
export const positions = pgTable("positions", {
  symbol: text("symbol").primaryKey(),
  qty: integer("qty").notNull().default(0),
  costCny: doublePrecision("cost_cny").notNull().default(0),
});

/** 成交日志:每一笔买入/卖出都精确落库(时间、价格、数量、金额、手续费、盈亏、数据源)。 */
export const trades = pgTable("trades", {
  id: serial("id").primaryKey(),
  ts: timestamp("ts", { withTimezone: true }).notNull().defaultNow(),
  symbol: text("symbol").notNull(),
  side: text("side").notNull(), // BUY | SELL
  qty: integer("qty").notNull(),
  price: doublePrecision("price").notNull(), // 标的原币种价格
  currency: text("currency").notNull(),
  fx: doublePrecision("fx").notNull(), // 折算人民币汇率(人民币标的为 1)
  amountCny: doublePrecision("amount_cny").notNull(),
  feeCny: doublePrecision("fee_cny").notNull(),
  pnlCny: doublePrecision("pnl_cny"), // 仅卖出有值(已实现盈亏,已扣费)
  pnlPct: doublePrecision("pnl_pct"),
  source: text("source").notNull().default("manual"), // manual | auto
  feed: text("feed").notNull().default("sina"), // 行情来源 sina | tencent | sim
  note: text("note").notNull().default(""),
});
