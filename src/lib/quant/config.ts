// 量化系统全局配置:标的、资金、费率、缓冲区大小。
// 注意:kaggle/server.py 中有一份等价配置,修改时两边需保持一致。

export type MarketId = "US" | "CN";

export interface SymbolDef {
  code: string;
  name: string;
  short: string;
  market: MarketId;
  currency: "USD" | "CNY";
  lot: number; // 最小交易单位(美股 1 股,A 股 ETF 100 份)
  decimals: number;
  sina: string;
  tencent: string;
  base: number; // 仅在所有行情源均不可用时,用于"模拟行情"的起始价
}

export const SYMBOLS: SymbolDef[] = [
  {
    code: "IBB",
    name: "生物科技指数ETF-iShares Nasdaq",
    short: "iShares Nasdaq Biotech",
    market: "US",
    currency: "USD",
    lot: 1,
    decimals: 2,
    sina: "gb_ibb",
    tencent: "usIBB",
    base: 217.6,
  },
  {
    code: "XBI",
    name: "美国生科ETF-SPDR",
    short: "SPDR S&P Biotech",
    market: "US",
    currency: "USD",
    lot: 1,
    decimals: 2,
    sina: "gb_xbi",
    tencent: "usXBI",
    base: 156,
  },
  {
    code: "159502",
    name: "标普生物科技ETF嘉实",
    short: "嘉实标普生物科技",
    market: "CN",
    currency: "CNY",
    lot: 100,
    decimals: 3,
    sina: "sz159502",
    tencent: "sz159502",
    base: 1.62,
  },
];

export const INITIAL_CAPITAL = 10000; // 元
export const FEE_RATE = 0.0003; // 单边万三(买卖各收一次)
export const TICK_MAX = 7200; // 每个标的保留 2 小时的 1 秒采样
export const POLL_MS = 1000; // 行情轮询周期
export const DEFAULT_FX = 7.2; // 汇率全部失败时的兜底值(会在界面标注)

// 策略参数(1 秒采样序列上)
export const MA_FAST = 30;
export const MA_SLOW = 120;
export const RSI_N = 60;
export const BOLL_N = 120;
export const BOLL_K = 2;
export const TAKE_PROFIT = 0.012;
export const STOP_LOSS = 0.008;
export const AUTO_COOLDOWN_MS = 30_000;
export const AUTO_CASH_FRACTION = 0.5;
