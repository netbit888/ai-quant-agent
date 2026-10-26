# AI Quant Agent

> Windows 桌面工具：读 MT5 账户的真实成交做复盘，并用 LLM 按数据给改进建议；
> 另有一个 AI 交易员模块，按 M1 结构自主决定开平仓。

**当前状态：543 个单测通过。** 两条主线都能跑：

- **实盘复盘**（只读）：多账户自动归档、按场次复盘、行为标记、交易评分、滚动质量，全部本地数据；
- **AI 自动做单**（会下单）：M1 超短线，面板上一键启停，**默认真实下单**，可切纸面。

有 Windows 安装包（`AIQuant-Setup-*.exe`）和绿色版（解压即用）两种形态。

---

## 安装与首次配置

### 装哪一个

| 形态 | 适合 | 说明 |
|---|---|---|
| **安装包** | 日常使用 | 双击 `AIQuant-Setup-1.0.0.exe`，**可选安装路径**（默认 `%LOCALAPPDATA%\Programs\AIQuant`），装完桌面和开始菜单各有快捷方式，卸载用安装目录的 `unins000.exe` |
| **绿色版** | 换机器、放 U 盘 | 解压到任意**可写**目录，双击 `AIQuant.exe` |

**不要装到 `C:\Program Files`。** 程序要往自己旁边写账本（`data/`）和配置（`config/`），
只读目录会在启动时直接报「目录不可写」——这是故意的，比等到点「开始做单」才 500 好查。

**卸载会保留 `data\` 和 `config\`。** 账本是唯一副本（MT5 历史有保留期、换服务器就丢），
卸载程序只清 WebView2 缓存。要彻底干净请手动删安装目录。

### 运行前提

1. 本机装好 **MetaTrader 5 终端**并登录目标账户。程序不内置 MT5，它通过 IPC 连**已经运行**的终端。
2. 窗口依赖 **Microsoft Edge WebView2 运行时**（Win10/11 绝大多数已自带）。
   真缺了会弹提示：「是」跳官方下载页，「否」退回浏览器模式（功能完全一样，只多一个浏览器窗口）。

### 首次启动

双击后会打开一个**独立应用窗口**——不是浏览器，没有地址栏和标签页。

1. 首启自动弹「配置模型 API」：选供应商（智谱 GLM 有免费档，国内直连不用代理）→ 填 key → 点「测试并保存」。
   它会**真调一次模型验证**，失败会回滚文件，不会留坏配置。
2. 配置存在 `config/.env`。以后想换，点面板模型条右边的「配置向导」。
3. 不想用 AI、只看账本，可以点「稍后设置」，面板照常能用。

### 怎么退出（重要）

**用窗口右上角「退出程序」按钮，或直接点窗口的 X。**

点 X 时程序会先拦下来问：没持仓直接退；有持仓给两个选择「先平仓再退出」/「强制退出」。

为什么要问：这套策略**不挂止盈止损**，仓位躺在券商那边没人看着就等于裸仓。
强制退出不会平仓。**别用任务管理器结束进程**——那等于拔电源，来不及平仓也不留日志。

---

## AI 自动做单

面板上的「AI 交易员」卡片是这套东西的核心，它不是回测，是**真的在账户上下单**。

| 项 | 默认 | 说明 |
|---|---|---|
| 模式 | **真实下单** | 面板上是二选一单选，纸面模式只记账不发单 |
| 决策间隔 | 60 秒 | 有持仓时每 5 秒查一次止损（`guard_sec`），不等满 60 秒 |
| 品种 | XAUUSDm | 0.01 手起 |
| 出场 | 程序闸门优先 | 止损 3.0 USD → 持仓超时 3600 秒 → 止盈门槛 3.25 USD，三条按序判断，过线直接市价平，不问模型 |

**它赚的是 1~3 分钟的价差**，不是趋势也不是波段。每条决策都是现算的：

1. 取 34 根 M1（模型只看最近 14 根，多出来的用于算 MA28）；
2. 程序算结构指标：ER（净移动÷总行程）、收盘位置、连阳连阴、振幅的 ATR 倍数；
3. 给两个背景量：M5 一行结构摘要、MA28 一个数字（**都是程序算好的结论，不给原始 K 线**）；
4. 模型返回 `{"act": bool, "p_up": 0~1, "agree": bool, "reason": "..."}`；
5. 闸门：`act=false` 直接跳过（这是允许的、也是常见的）；`|p_up-0.5| < 0.06` 跳过（信号不够强，不够付点差）；
6. 下单。

**教训库（经验库）**：每笔平仓后等 7 根 K 线做复盘，模型自己给「主题 + 教训」，按重复次数分域注入下一轮提示词。教训库是**可安装的包**（`data/live/lesson_packs/`），面板上可切换、导入、导出，像选 EA 一样——换账户不丢经验，重置战绩更不会清空它。
（`entry` 只进进场提示词，`exit` 只进出场提示词）。教训存在 `lessons.jsonl`，**不随「重置战绩」清空**。

**为什么退出要问**：程序不挂 SL/TP，裸仓没人管。所以退出前必须决定是平掉还是留着。

---

## 快速开始（源码态）

> 下面这些是开发/命令行入口。日常使用不需要，双击 exe 就行。

```bash
# 数据（回测才需要，实盘复盘不需要）
python aqa.py data pull --symbol XAUUSDm --tf H4     # 需要 MT5 终端开着

# 实盘复盘（只读）
python aqa.py live                 # 终端输出统计 + 规则风险标记
python aqa.py live --ai            # 额外生成 AI 复盘（会先要求确认）
python aqa.py live --ai --force    # 忽略缓存重新分析（同样要确认）
python aqa.py live --ai --yes      # 跳过确认，给脚本用
python aqa.py live --watch --interval 300   # 常驻循环同步
python aqa.py live --json          # 机器可读输出
python aqa.py live --account <login>        # 看归档的历史账户（离线，不连 MT5）
python aqa.py accounts list        # 列出所有已归档账户
python aqa.py doctor               # 环境自检：MT5 进程与连接、账本、面板

# 面板
python -m tools.dashboard          # 源码态默认用浏览器打开
python -m tools.dashboard --native # 强制开原生窗口（调试窗口用）
python -m tools.dashboard --web    # 强制用浏览器（这是调试入口，永远可用）

# 单元测试
python -m unittest discover -s tests -v
```

回测/评估那套（`backtest` / `evaluate` / `iterate` / `portfolio` / `validate` / `sweep`）
需要先用 `aqa.py data pull` 拉数据，仓库里不带 CSV。

## 监控面板

四视图，纯 SVG 图表（不依赖 CDN）：

| 视图 | 内容 |
|---|---|
| **实盘复盘** | **AI 交易员控制台**（做单品种、纸面/真实、开始、关闭并清仓、清空经验、教训库、全屏提醒）、**本场复盘**（按 30 分钟断场，含自动行为标记与下一场规则）、真实账户余额/净值、今日与累计盈亏、权益曲线、逐日盈亏、每日余额快照、AI 复盘建议、规则风险标记、分方向/平仓原因/品种/星期统计、持仓与最近成交 |
| **实时迭代** | KPI 卡、每轮净盈利/回撤曲线、轮次表、LLM 诊断、代码查看 |
| **策略评估** | 逐年柱状图、成本压力表、Gate 2 逐项判定 |
| **策略库** | strategies/ 下所有策略、参数、最近评估状态 |

## 实盘复盘

把 MT5 账户的真实成交读出来做统计，并让 LLM 按数据给改进建议。

```bash
python aqa.py live                 # 终端输出统计 + 规则风险标记
python aqa.py live --ai            # 额外生成 AI 复盘（会先要求确认）
python aqa.py live --ai --force    # 忽略缓存重新分析（同样要确认）
python aqa.py live --ai --yes      # 跳过确认，给脚本用
python aqa.py live --watch --interval 300   # 常驻循环同步
python aqa.py live --json          # 机器可读输出
python aqa.py live --account <login>        # 看归档的历史账户（离线，不连 MT5）
python aqa.py accounts list        # 列出所有已归档账户
python aqa.py doctor               # 环境自检：MT5 进程与连接、账本、面板
```

### 本场复盘（盘后第一眼）

手动打完一场，打开面板最上面那块就是它。切场口径：**上一笔平仓 → 下一笔开仓间隔超过 30 分钟**断一场。

| 内容 | 说明 |
|---|---|
| 场次标签 | 最近 6 场，点一下切换 |
| 五个数 | 笔数 / 持仓中位 / **最大单笔风险占余额** / 止损宽度 / 手动平仓占比 |
| 逐笔 | 盈亏、R、持仓、单笔风险、止损点数、离场方式 |
| 自动标记 | 报复单（亏损后 5 分钟内再入场）、手痒（持仓 <1 分钟）、宽止损（>本场中位数 2 倍）、超仓 |
| 一句话备注 | 每场一句话 + 3 个快捷标签，10 秒，可跳过 |
| AI 分析 | 本场 vs 上一场，2 条「证据 → 结论」（付费，需确认） |
| 下一场规则 | 只给 1 条，且必须从固定指标表里选，下一场自动检查遵守 / 违反 |

**为什么不打逐笔标签**：打完很累、隔天又会忘。报复 / 手痒 / 宽止损 / 超仓这些全部由时间戳和价格算出来，不问你；
意图真正有信息量的粒度是「这一场我在干什么」，一句话就够。

**小样本护栏**：本场 < 5 笔时只显示过程指标，不显示胜率和 PF——n=8 的胜率置信区间是 ±35 个百分点。

**场次不用于统计胜率**：单笔场次很常见（做一笔就停），按场次算胜率只会得到 0% 或 100%。
场次是「阅读单元」和「AI 分析单元」，统计仍然走全账户。

### 删除归档账户

账户下拉框右侧的「删除账户」只在**非当前**账户上出现，点击后要确认。行为是**软删除**：
整个目录移到 `data/live/_trash/<login>-<时间戳>/`，从 `index.json` 里移除。把它移回 `data/live/accounts/` 再同步一次就恢复了。

真实账户账本是唯一副本（MT5 有历史保留期、换服务器就丢），所以不提供真删除。

### MT5 会不会自己开

**取决于面板从哪启动。** 双击 `AIQuant.exe`（安装版）或 `start-dashboard.bat`（源码态）启动时，
面板在资源管理器的进程树里，发现终端没开会用 `subprocess.Popen` 把它拉起来（等最多 30 秒），
成功后自动连上。连不上就每 20 秒重试一次，不用重启面板。

从脚本/CI/agent 的进程树里启动时**拉不起来**：MetaTrader 的 GUI 会以
`0xC0000139`（ENTRYPOINT_NOT_FOUND）立刻退出，`terminal64.exe`、`MetaEditor64.exe`、
`metatester64.exe` 三个都一样。这是进程树相关的限制，不是 MT5 装坏了。

- HTTP 请求路径**永远不会**触发启动（会阻塞，页面会假死），只有后台记录线程会。
- 想关掉自动启动：`python -m tools.dashboard --no-launch`
- 排查用：`python aqa.py doctor`，会打印安装路径、进程状态、连接结果、账本、面板端口。

### 多账户：在 MT5 终端里切换，这里自动归档

MT5 的 Python 接口要切换登录**必须提供账号密码**，把密码存明文是本工具不该承担的风险。
所以做法反过来：**你在 MT5 终端里切账户，本工具每次同步时自动识别（`account_info().login`）
并把数据写进该账户自己的目录。**

```
data/live/
  index.json                 账户索引（login → 服务器/币种/首次与最后出现）
  profile.json               品种画像（全局，不分账户）
  accounts/
    <login>/                 每个账户一套完整账本，互不污染
      deals.jsonl  account.json  account_daily.json  daily.json
      quality_daily.json  advice.json  api_calls.jsonl
      position_state.json  position_snapshots.jsonl
      autotrade_state.json  autotrade_log.jsonl  lessons.jsonl  rules.json
```

每条成交记录都带 `login` 字段。这不是冗余——**早期版本没有它，切账户时新账户的成交
直接追加到旧账户账本上，净额/胜率/回撤/入金全错，而且事后无法自动拆分**（只能靠
`tools/migrate_accounts.py` 按「终端当前能读到的 ticket」反推归属）。

面板顶部有账户下拉框：带「● 当前」的是终端现在登录的（实时同步），
带「· 归档」的只能离线看历史数据——不连 MT5、不刷新、不给生成新的 AI 复盘。

```bash
python -m tools.migrate_accounts --other <login>   # 把旧版混合账本拆开（幂等）
python aqa.py accounts add <login> --server <服务器>  # 补登记一个只有归档数据的老账户
```

### 付费 API 一律先确认

会花 token 的调用（`live --ai`、`iterate`、面板里的「重新分析」）都必须先确认：

| 入口 | 确认方式 |
|---|---|
| 面板 | 弹窗，显示「会产生费用」，点「确认调用」才发请求 |
| CLI | `[y/N]` 提示，默认 N；`--yes` 显式跳过 |
| HTTP | `GET /api/live` **永不调用 LLM**，只返回缓存；生成走 `POST /api/advice`，body 必须 `{"confirm": true}`，否则 400 |

设计原则：**GET 不能产生费用**。之前页面一加载、交易集合一变就静默调 API，
这是错的。现在没有缓存时接口返回 `need_confirm: true`，面板渲染一个按钮等用户点。

每次调用都记一行到 `data/live/accounts/<login>/api_calls.jsonl`
（时间、模型、token 用量、成功与否），面板顶部显示「已调用 N 次 · X tokens」（跨账户汇总）。

**数据落在本地，可以累积**（以下都在 `data/live/accounts/<login>/` 下，每账户一份）：

| 文件 | 内容 |
|---|---|
| `deals.jsonl` | 原始成交，带 `login`，按 ticket 去重追加 |
| `daily.json` | 逐日盈亏汇总 |
| `account_daily.json` | 每日账户余额快照 |
| `quality_daily.json` | 每日各滚动窗口的质量指标（用于画趋势） |
| `position_snapshots.jsonl` | 持仓期间 SL/TP 变动记录（只在变化时写） |
| `api_calls.jsonl` | 每次付费 API 调用的时间、模型、token 用量 |
| `advice.json` | 最近一次 AI 复盘（按交易集合缓存） |
| `autotrade_log.jsonl` | AI 交易员每条决策（open/close/skip/hold/review），带当时的特征快照 |
| `lesson_packs/` | **经验库包**（可安装）：像选 EA 一样在面板上切换/导入/导出/新建。当前激活的教训存 `<包名>.jsonl`，切换即刻生效；未激活包时落到 `_scratch_.jsonl` 草稿。首次启动会自动把账户目录里的旧 `lessons.jsonl` 提取成包 |

### 交易评分

`live/scoring.py`，纯规则计算，同一份数据永远得同一个分数（LLM 的结论只作解释，不进分数）。

**单笔 0-100**：结果 40 / 纪律 30 / 风险 20 / 时长 10。权重写在代码里可审计，
用于复盘找坏交易，面板「最近平仓记录」每行带评分徽章。

**账户 A~F**：六个维度各自定级，**整体取最差的一项**（木桶原理），不做加权平均——
「期望 0.3R」和「回撤 50%」不可交换，平均成 72 分是伪科学。

| 维度 | 依据 | 达标线 |
|---|---|---|
| 期望 | 单笔期望 R | ≥ +0.10 R |
| **盈亏平衡边际** | 实际胜率 − 盈亏比要求的胜率 | ≥ 0 pp |
| 稳定性 | 各窗口期望为负的比例 | ≤ 20% |
| 风险 | 峰值回撤%，穿仓即 F | ≤ 20% |
| 纪律 | 手动平仓占比 | ≤ 35% |
| 样本 | 已平仓笔数 | ≥ 50 笔 |

**为什么不用「对回测偏离」**：拿 H4 结构突破策略的期望当 M1 剥头皮的基准是错的，
两个策略不可比。而且市面上任何「M1 剥头皮 PF 1.5~2.0」的说法都没有可验证的成交记录。

真正不可争辩的参照只有**盈亏平衡线**，它由你自己的盈亏比决定：

```
需要的胜率 = 1 / (1 + 盈亏比)      # 事后口径，点差已含在已实现盈亏里
```

盈亏比 0.75 → 需要 57.1% 胜率。这个基准不需要外部数据，任何策略、任何品种都算得出来，
而且换手法也改不了。

**交易画像** `data/live/profile.json` 记录品种/周期/点差/ATR，可重算（需要先 `aqa.py data pull`）：

```bash
python -m tools.profile              # 从拉下来的 M1 数据重算
python -m tools.profile --tf H4
```

**计划盈亏比**：持仓快照会记录开仓时的 SL/TP，因此能算出「我打算赚多少」，
和实际实现的盈亏比对照——差得越多说明越没守住计划。

### 近期交易质量

`live/quality.py` 在 `stats.py`（全账户总览）之外单独看**最近一段**，并和它前面
那个等长窗口对比，判断在变好还是变差。

两个轴，不能混：

| 轴 | 窗口 | 回答什么 |
|---|---|---|
| 时间 | 近 7 天 / 30 天 / 1 年 | 这段时间状态如何——策略在当前行情还灵不灵 |
| 笔数 | 近 20 / 100 笔 | 统计上能不能信——样本够不够下结论 |

每个窗口都给 **Wilson 95% 置信区间**。近 10 笔胜率 50% 的区间是 [24%, 76%]，
所以 n < 20 的窗口会打上「样本不足，不可据此决策」——没有这条护栏，
这个模块就是在生产错误决策。

窗口内回撤用**累计 R** 的峰谷差（R = 盈亏 / 全历史平均亏损），不受手数变化影响，
可跨窗口横向比较。另配一张 **近 20 笔滚动期望**曲线，能直接看出从哪天开始失效。

面板启动时会同时拉起一个后台线程，每 300 秒同步一次——**只要面板开着，即使不看页面也在记**。
不想启用后台线程用 `--no-live`。

> 前提：MT5 终端必须开着并已登录目标账户。复盘模块只读（`history_deals_get` / `positions_get`），
> 没有任何下单能力。**下单能力在 `live/autotrade.py`，只有面板上点了「开始做单」才会用。**

## 架构

```
AIQuant.exe            安装版入口（PyInstaller 打包）；源码态是 python -m tools.dashboard
aqa_launcher.py        打包入口：stdio shim（无控制台时 stdout 重定向到日志）+ 单实例 mutex + 崩溃弹窗
installer/aqa.iss      Inno Setup 安装脚本
aqa.spec               PyInstaller 配置（onedir）
aqa.py                 统一 CLI（data / strategy / backtest / evaluate / iterate / portfolio / validate / sweep / live / accounts / serve）
├── live/              实盘：journal 账本、stats 统计、sessions 场次、rules 规则、quality 质量、
│                      scoring 评分、profile 画像、positions 持仓、advisor AI 复盘（只读）、
│                      autotrade AI 交易员（做单）、appdirs 路径、cost 成本
├── tools/             dashboard 面板、desktop 原生窗口宿主、switch_llm、lesson_stats、build_*.bat
├── agent/             LLM 层：llm 封装、prompt 模板、代码沙箱、迭代主循环
├── backtest/          回测引擎：指标、模型、引擎、metrics、PA 结构识别、品种规格
├── strategies/        策略库（手写参照 + LLM 生成）
├── risk/              风控：单笔风险、日熔断、连亏停手、总回撤熔断
├── portfolio/         多品种组合：独立回测 + 权益曲线合并
├── execution/         MT5 下单接口（`live/autotrade.py` 走这里发单）
└── mt5/               数据拉取、EA 编译、MT5 报告解析
```

细节见 [docs/architecture.md](docs/architecture.md)。

## 关键限制（务必阅读）

1. **回测是「仅开盘价」模型**：信号基于上一根 bar 收盘，当前 bar 开盘成交。滑点、手续费、跳空未建模。
2. **成本是硬约束**：点差口径要分清——回测按 180 points 建模，实盘 XAUUSDm 是固定 240 points（0.01 手约 0.24 USD/笔）。
   AI 交易员做 M1，一笔的来回成本占单根振幅比例很可观，这是它必须把目标定在 3 USD 以上才划算的原因。
3. **样本量**：黄金 H4 十二年只有约 130 笔信号，单年回测统计意义弱。
4. **收益水平低**：Gate 2 通过但年化仅 7-8%，2025 年几乎不赚（PF 1.08），样本外测试段只有 57 笔。
5. **参数有数据窥探**：`max_atr_ratio=2.5` 在全区间选出，虽样本外仍正但增益有限。
6. **AI 交易员是短样本**：历史成交都在少数几个场次里，按分桶得出的结论只对那几段行情成立。

## 验证记录

- [docs/validation.md](docs/validation.md) —— 回测引擎哪些可信、哪些存疑
- [docs/iteration-log.md](docs/iteration-log.md) —— 每轮策略迭代的完整数据（停在 2026-10-19）
- [docs/dev-notes.md](docs/dev-notes.md) —— 为什么这么设计：踩过的坑与取舍
- [docs/archive/status-report-2026-10-19.md](docs/archive/status-report-2026-10-19.md) —— 2026-10-19 阶段报告（历史，含「试过但失败的路径」）
- [PLAN.md](PLAN.md) —— 四阶段路线图与 Gate 标准（历史，已附现状补记）

## 环境

Windows 10/11 x64 · Python 3.13 · MetaTrader5 · pywebview + WebView2 · 智谱 GLM（默认）/ DeepSeek（备选，OpenAI 兼容）

`pip` 配置：`.venv/pip.ini` 已设为官方源并禁用代理（本机 HTTP_PROXY 会导致 pip 失败）。

提交推送：双击 `push.bat`（逻辑在 `push.ps1`）。提示会是中文，提交说明也可以直接写中文——
脚本把它读成 UTF-16 再写成 UTF-8 交给 git，所以 GitHub 上不会乱码。

## 从源码构建

```bash
# 一次性：装依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install "pyinstaller>=6.11"

# 打包（onedir，无控制台）
tools\build_portable.bat          # → dist\AIQuant\AIQuant.exe

# 打安装包（需要先装 Inno Setup 6：https://jrsoftware.org/isdl.php）
tools\build_installer.bat         # → dist\installer\AIQuant-Setup-*.exe
```

调控制台版本：`set AQA_CONSOLE=1` 再打包，会保留黑窗口把日志看回来。

**改前端 CSS/HTML 必须重新打包才生效**——模板在打包时被复制进 `_internal/tools/`，
不像源码态那样刷新页面就行。

## 免责声明

技术研究用途，不构成投资建议。

**这个程序会在你的交易账户上真实下单。** AI 交易员默认模式是真实下单（面板上可切纸面），
它按 1~3 分钟的价差做超短线，**不保证盈利，可能亏掉全部本金**。它不挂止盈止损——
出场由程序闸门按浮亏主动市价平仓，但程序没在跑的时候仓位没有任何保护。

回测收益不代表未来表现。用之前先在模拟账户上跑够样本，再用你真愿意亏掉的钱。
