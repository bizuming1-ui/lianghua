# 生物科技ETF 量化终端 · BIOTECH QUANT

监测三只标的,**1 秒级刷新**,带模拟交易、成交日志、实时胜率与量化信号。纯黑底大横屏,整页固定不需要滚动。

| 代码 | 名称 | 市场 |
| --- | --- | --- |
| IBB | 生物科技指数ETF-iShares Nasdaq | 美股(USD) |
| XBI | 美国生科ETF-SPDR | 美股(USD) |
| 159502 | 标普生物科技ETF嘉实 | A 股(CNY,100 份一手) |

## 功能

- **秒级行情**:服务端每 1 秒拉取一次(新浪为主 → 腾讯备用 → 全失败才显示明确标注的"模拟行情"),前端每 1 秒同步增量 tick。
- **超大中央图谱**:Canvas 绘制价格 + MA30/MA120 + 布林带 + 成交标记(买/卖);窗口 1/5/15/30 分钟;"三只对比 %"模式;十字光标。
- **模拟交易**:起始资金 ¥10,000;美股按实时汇率折算人民币;手续费万三;A 股 ETF 以 100 份为单位。
- **成交日志**:每笔买/卖精确记录 时间(北京时间)、价格、数量、成交额、手续费、盈亏、来源(手动/自动)、行情源。支持分页查看和导出 CSV。
- **实时胜率**:胜率 = 已平仓且盈利笔数 / 卖出笔数(含手续费的净盈亏,加权平均成本法)。
- **量化自动策略(可选)**:评分(趋势 + RSI + 布林)≥2 买入 50% 现金;≤-1 或 +1.2% 止盈 / -0.8% 止损平仓。**仅在交易时段且行情为真实数据时运行**。
- **浏览器模拟器 + 复制代码界面**:`/` 为浏览器模拟器(地址栏、网址、右上角"复制代码"、"进入复制代码界面"),`/code` 可逐文件复制 GitHub 代码和 Kaggle 代码。

## 架构(第一性原则:一份前端、两套后端)

```
public/terminal/index.html   框架无关的单文件前端(Canvas),Next 与 Kaggle 共用
src/lib/quant/*              Next.js 版引擎:行情源、指标、撮合、胜率、自动策略
src/db/schema.ts             PostgreSQL(Drizzle):account / positions / trades
src/app/api/*                state / trade / trades / auto / reset / health / code
kaggle/server.py             Python 标准库版引擎(SQLite),接口与 Next 版一致,零依赖
kaggle/launch_cell.py        Kaggle Cell 1 模板(启动服务 + cloudflared 公网隧道)
kaggle/status_cell.py        Kaggle Cell 2(状态 / 停止)
```

## 本地运行(Next.js + PostgreSQL)

```bash
npm install
cp .env.example .env        # 配置 DATABASE_URL
npx drizzle-kit push        # 建表
npm run build && npm start  # 打开 http://localhost:3000
```

## Kaggle 运行

1. 新建 Notebook → Settings → **Internet 设为 ON**。
2. 打开本站 `/code` → 标签②,复制 **Cell 1**,粘贴运行。输出里的 `https://xxxx.trycloudflare.com` 就是网址。
3. Cell 2 可查看状态、重新打印网址或停止服务。

## 诚实的边界

- 这是**模拟盘**,不构成投资建议。行情来自公开免费接口,可能有延迟;休市时价格不变(日志备注会标"休市·按最近价成交")。
- 免费源的"1 秒刷新"是**抓取频率**,实际报价变动频率取决于交易所与数据商(美股通常数秒一次)。
- 指标基于 1 秒采样序列,采满 120 秒后启用(预热进度显示在界面)。
- Kaggle 会话关闭后服务随之停止;trycloudflare 网址每次启动都会变化。
