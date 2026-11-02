# 生物科技ETF 量化终端 · BIOTECH QUANT

监测三只标的,**1 秒级刷新**,带模拟交易、成交日志、实时胜率与量化信号。纯黑底大横屏,整页固定不需要滚动。

| 代码 | 名称 | 市场 |
| --- | --- | --- |
| IBB | 生物科技指数ETF-iShares Nasdaq | 美股(USD) |
| XBI | 美国生科ETF-SPDR | 美股(USD) |
| 159502 | 标普生物科技ETF嘉实 | A 股(CNY,100 份一手) |

## 功能

* **秒级行情**:服务端每 1 秒拉取一次(新浪为主 → 腾讯备用 → 全失败才显示明确标注的"模拟行情")。
* **超大中央图谱**:Canvas 绘制;分辨率 秒 / 1分 / 5分 / 15分 / 日(K 线用真实历史:Yahoo → 东方财富回退);**鼠标左键按住拖动查看历史**、**滚轮缩放**(以鼠标位置为锚点)、双击复位、"⇥最新"回到右端;MA / 布林 / 成交标记 / 十字光标;"三只对比 %"模式。
* **模拟交易**:起始资金 ¥10,000;美股按实时汇率折算人民币;手续费万三;A 股 ETF 以 100 份为单位。成交日志(北京时间)、CSV 导出、实时胜率。
* **自动交易三档**:关闭 / 规则策略(原 1 秒指标评分,保持不变) / **AI 推理策略(新)**。
* **AI 推理决策(新,仅 Kaggle 后端)**:`Qwen3-14B-AWQ` 思考模式,由 vLLM 以 **tensor-parallel-size 2** 同时占用两张 T4;实时显示两张卡的利用率/显存与模型的推理过程。
* **浏览器模拟器 + 复制代码界面**:`/` 为浏览器模拟器(地址栏、网址、右上角"复制代码"下拉含 Cell 0 / Cell 1 / GitHub 全部代码、"进入复制代码界面");`/code` 逐文件展示,自动与你的 GitHub 仓库对比并标出「新增 / 有修改 / 已同步」,每个文件有"在 GitHub 编辑"(先自动复制再打开)和"复制此文件"。

## AI 决策的第一性原理

LLM 不能凭空预测价格。本系统让**数学做裁判、LLM 做分析师**:

1. `kaggle/quant_math.py` 用真实历史 K 线计算:EWMA/ATR 波动率、Lo-MacKinlay 方差比(趋势/均值回归/随机游走)、岭回归预测 + **70/30 时间切分(含 embargo)的样本外检验**(IC、命中率及 Wilson 下界、扣费净期望、t 值)。
2. `kaggle/ai_engine.py`:159502 对 XBI 的**隔夜联动回归**(β、t、R²)、SPY/QQQ/VIX/美债/美元/XLV 宏观、Yahoo 新闻标题 → 组织成提示词 → 大模型推理并输出 JSON(方向/置信度/周期/止损止盈/因素/风险)。
3. **数学闸门**(代码执行,LLM 不能越过):样本外必须有统计优势、预期净收益 ≥ 1.5×往返成本、模型上涨概率 ≥ 52%、置信度 ≥ 0.6、避开开收盘、24h 亏损 ≤ 3% 本金、不加仓、5 分钟冷却、1/4 凯利 + 单笔风险预算 1% 的仓位。
4. 持仓由代码执行止损/止盈/到期/收盘前平仓;所有 AI 决策到期后自动复盘,统计命中率与净收益(`ai_decisions.jsonl` 也会落盘)。

## 架构(第一性原则:一份前端、两套后端)

```
public/terminal/index.html   框架无关的单文件前端(Canvas),Next 与 Kaggle 共用
src/lib/quant/*              Next.js 版引擎:行情源、指标、撮合、胜率、规则策略、历史K线
src/db/schema.ts             PostgreSQL(Drizzle):account / positions / trades
src/app/api/*                state / trade / trades / auto / reset / health / code / history / ai
kaggle/server.py             Python 版引擎(SQLite),接口与 Next 版一致 + AI 接口
kaggle/quant_math.py         量化数学内核(numpy)
kaggle/ai_engine.py          AI 引擎:数据、统计检验、提示词、vLLM 流式调用、数学闸门、执行与复盘
kaggle/llm_cell.py           Kaggle Cell 0:双 T4 部署 vLLM(TP=2)并实测双卡同时工作
kaggle/launch_cell.py        Kaggle Cell 1 模板(写文件 + 启动服务 + cloudflared 公网隧道)
kaggle/status_cell.py        Kaggle Cell 2(状态 / 双卡利用率 / AI 决策 / 停止)
```

## 本地运行(Next.js + PostgreSQL)

```bash
npm install
cp .env.example .env        # 配置 DATABASE_URL
npx drizzle-kit push        # 建表
npm run build && npm start  # 打开 http://localhost:3000
```

## Kaggle 运行

1. 新建 Notebook → Settings → **Accelerator = GPU T4 x2**,**Internet = ON**。
2. 打开本站 `/code` → 标签②:先复制 **Cell 0** 运行(首次 8~20 分钟),看到 `✅ 两张 T4 同时工作`;再复制 **Cell 1** 运行,输出的 `https://xxxx.trycloudflare.com` 就是网址。
3. 页面右侧"AI 推理决策"面板:点 **AI** 启用自动交易;"立即分析"手动触发;"推理详情"查看完整推理、闸门检查、数据源状态。
4. Cell 2 可查看状态 / 双卡利用率 / 停止服务。

## 诚实的边界

- 这是**模拟盘**,不构成投资建议。行情来自公开免费接口,可能有延迟;休市时价格不变(日志备注会标"休市·按最近价成交")。
- 免费源的"1 秒刷新"是**抓取频率**,实际报价变动频率取决于交易所与数据商(美股通常数秒一次)。
- 指标基于 1 秒采样序列,采满 120 秒后启用(预热进度显示在界面)。
- Kaggle 会话关闭后服务随之停止;trycloudflare 网址每次启动都会变化。
* **没有任何模型能保证胜率。** 本系统的提升来自"选择性":没有样本外统计优势就不交易;AI 决策命中率以复盘数据为准,请观察足够样本后再下结论。
* 日内 30 分钟尺度的价格几乎不可预测(往返成本 ≈ 波动),真实可验证的优势主要来自**隔夜联动**(159502 跟随前一美股交易日 XBI)与少数极端状态。
