# 架构

> 最后更新：2026-10-27
>
> 旧版（2026-10-19，描述 `prompts.py → .mq5` 那条路）已归档到
> [`archive/architecture-2026-10-19.md`](archive/architecture-2026-10-19.md)。
> 现在生成的是 **Python 策略**，不是 MQL5。

本文只讲**现在这套东西为什么这么搭**。踩过的具体坑见 [`dev-notes.md`](dev-notes.md)。

---

## 一、两条主线，边界很清楚

```
只读：MT5 成交  →  journal 账本  →  stats / sessions / quality / scoring  →  面板
                                   ↘  advisor（LLM，要确认）
做单：MT5 行情  →  autotrade 决策  →  execution 下单  →  账本（同上）
```

**复盘那一半永远不碰下单。** `live/journal.py` 用 `history_deals_get` / `positions_get`，
`live/advisor.py` 只读统计结果。下单能力集中在 `live/autotrade.py` + `execution/live.py`，
面板上必须点「开始做单」才会启动。

这条边界不是洁癖：只读模块被面板的轮询线程反复调用（1.5 秒一次），
任何一个能下单的函数混进去都可能被意外触达。

## 二、AI 交易员的决策链路

一轮 `tick()`（默认 60 秒一次，有持仓时每 5 秒查止损）：

```
1. 取行情：34 根 M1（多拉是为了算 MA28，模型只看最近 14 根）+ 报价 + tick 摘要
2. 有持仓？
   ├─ 是 → _exit_gate 三道闸门（顺序写死，不能换）
   │        止损 sl_usd → 持仓超时 max_hold_sec → 止盈门槛 tp_min_usd
   │        返回 dict = 程序平掉，不问模型；返回 None = 交给模型决定
   └─ 否 → 冷却检查（平仓后 30 秒） → 调 ask_entry
3. 模型返回 {act, p_up, agree, reason}
4. 闸门 A：act == false → skip（模型主动不做，这是允许的）
5. 闸门 B：|p_up - 0.5| < min_edge(0.06) → skip（不够付点差）
6. direction = 1 if p_up > 0.5 else -1 → send_open
7. 落盘：open/skip/hold 行 + 特征快照 feat
```

**为什么出场闸门在前、模型在后**：模型有 60 秒的决策节奏，但止损不能等 60 秒。
实测过一笔浮亏 -2.06 拖到 -3.77 才平（超调 26%），这就是"等下一轮再判断"的代价。

**为什么 `_exit_gate` 的顺序不能换**：止盈排到止损前面，止损就永远不会触发——
价格先摸到 +3.25 的次数远多于先摸到 -3.00。

## 三、提示词分层

| 层 | 位置 | 内容 | 为什么放这 |
|---|---|---|---|
| 静态规则 | `ENTRY_SYSTEM` / `EXIT_SYSTEM` | 打法、信息边界、输出格式、判断原则 | 前缀固定，能命中 prompt cache |
| 动态数据 | user 消息 | K 线、结构指标、tick、教训、报价、时间 | 按「最不变 → 最易变」排序，时间戳放最后一行 |
| 经验 | user 消息里的一节 | 教训库按主题聚类，按次数排序 | 它是数据不是规则，位置要能被模型当成"我的历史"读 |

**教训分域注入**：教训带 `scope`（entry/exit/both），进场提示词只收 entry 和 both，
出场同理。实测修之前，76% 的教训是出场味的却同时注入进场提示词——那等于在教它"别急着平仓"
的同时让它"敢开仓"，两条互相抵消。

## 四、信息边界：程序算好结论，不给原始数据

模型能看到的东西是**刻意限制**的：

| 给它什么 | 形式 | 不给什么 |
|---|---|---|
| M1 行情 | 最近 **14 根**原始 K 线（`visible_bars()` 硬截断） | 第 15 根，以及更早的 |
| M5 背景 | **一行**结构摘要（ER / 位置 / 方向） | 14 根 M5 的原始 K 线 |
| MA28 | **一个数字** + 距均线多远（用 ATR 归一化） | 那 28 根收盘价 |

**为什么这么抠**：这个策略赚的是 1~3 分钟的价差。多给一根 K 线它就会去找更长的故事，
而故事越长越容易和真实的时间尺度错配。14 根是"刚好够看出结构"的量。

MA28 需要 28 根，所以取数要拉 34 根——但多出来的部分只变成均线数字，
`build_entry_prompt` / `_structure` / `_atr` 内部各自走 `visible_bars()` 截断。
测试里钉死了这条：prompt 里必须仍然只有 14 行 K 线、不出现 `T-14`。

## 五、路径：两个根，别混

`live/appdirs.py`：

```
bundle_root()   只读资源  →  源码态 = 仓库根；冻结后 = _MEIPASS(_internal)
                            模板、strategies/*.py
app_root()      可写数据  →  源码态 = 仓库根；冻结后 = exe 同级
                            data/、config/
```

**必须分开。** 如果数据根也走 `_MEIPASS`，绿色版的账本会写进 `_internal/`，
覆盖升级或重装就一起没了——而 `data/live/` 下的账本是**唯一副本**
（MT5 历史有保留期、换服务器就丢），丢了没有任何地方能恢复。

`env_file()` 的回落顺序：`config/.env` → exe 同级 `.env` → 新建走 `config/.env`。
这样源码直跑继续用仓库根的 `.env`（现有习惯和测试都不受影响），
安装版首次配置落在 `config/`，和只读资源彻底分开。

## 六、进程模型

### 源码态 / `--web`

```
main() → serve_forever()          单线程，浏览器打开页面
```

### 打包版 / `--native`

```
主线程      webview.start()   ← 必须占主线程（WinForms 消息循环）
            └─ 窗口全关后返回 → server.shutdown() → 干净退出
http 线程   serve_forever(poll_interval=0.25)   daemon
live 线程   每 300 秒同步账本                    daemon
```

**顺序是反的**：窗口占主线程，HTTP 在子线程。因为 `webview.start()` 内部是
WinForms 的消息循环，不能在子线程跑。

### 退出：只有一份实现

```
用户点 X（GUI 线程）
  → closing 立刻 return False 拦下，起线程 evaluate_js("aqaNative.onCloseRequested()")
  → 页面调 GET /api/app/quit-preview（只读，allow_launch=False 所以终端没开也秒回）
       没仓 → 直接 POST /api/app/quit
       有仓 → 弹 quitMask → 用户选「先平仓 / 强制退出」→ POST /api/app/quit
  → 服务端 _shutdown（停线程 + 平仓 + 断 MT5）→ 写 JSON → 0.35 秒后 destroy 窗口
  → closing 再触发，此时 _exit_allowed 已置位 → 放行
```

**`closing` 里绝不能阻塞**——它在 GUI 线程跑，阻塞就等于窗口卡死。所以它只负责
"拦下 + 通知页面"，不做任何等待。用户点「取消」时 Python 侧没有任何等待中的对象要清理，
连点十次 X 也只是重来，没有死锁可能。

浏览器模式仍然走 `os._exit(0)`（原来是唯一路径，现在只服务 `--web`）。
窗口模式**不用**它——那会让窗口消失得像崩溃。

## 七、账本落盘

```
data/live/
  index.json                     账户索引
  accounts/<login>/
    deals.jsonl                  原始成交（带 login 字段，按 ticket 去重）
    daily.json                   逐日盈亏
    account_daily.json           每日余额快照
    quality_daily.json           每日滚动质量
    position_snapshots.jsonl     持仓期间 SL/TP 变动
    api_calls.jsonl              付费 API 调用记录
    advice.json                  AI 复盘缓存
    autotrade_state.json         AI 交易员状态（仓位、战绩、配置）
    autotrade_log.jsonl          每条决策 + 特征快照
    lessons.jsonl                教训库（独立于流水）
```

**教训库为什么独立成文件**：以前教训挂在流水里，一次「重置战绩」把所有教训一起归档了
（实测 188 条教训全不可见）。现在 `reset_history(clear_lessons=False)` 只清战绩，
`reset_lessons()` 单独清经验。

**特征快照存什么**：`er / drift / pos / span / streak / atr / regime / prog_dir` 加
`ma28*`。不存 14 根 K 线原文——每笔都存会让日志膨胀 3~4 倍，而"特征值 + 结果"
已经够做全部单变量/双变量分析。

## 八、单实例

同一个账户**只能跑一个面板**。两份进程会互相覆盖 `autotrade_state.json`、重复下单。

两道防线：
1. `Server.allow_reuse_address = False`（标准库 `HTTPServer` 默认是 1，Windows 上
   允许两个进程绑同一端口且不报错，表现为请求随机命中）；
2. 启动先探活 `/api/app/info`——是自己人就只开一个新窗口看板然后退出，不再起服务。

看板窗口 `js_api=None` 并注入 `window.__AQA_ATTACH__`，前端的「退出程序」会拦住并
提示回主窗口，否则那一 POST 会打到对方进程把正在跑的那份关掉。

## 九、测试策略

543 个单测，全部**不依赖 MT5 终端**：`tests/_stubs.py` 在没装 `MetaTrader5` 时装最小桩，
账本路径通过 `live/paths.py` 的 `set_root(tmp)` 重定向到临时目录。

前端另有一套冒烟：`tools/smoke_dashboard.js`，从 `dashboard_template.html` 抠出内联
`<script>`，在 Node 的假 DOM 里执行，用一份真实的 `/api/live` 快照（`live_probe.json`，
本地生成、不入库）跑各渲染分支。它钉住的是「JS 改了以后页面还能画出来」。

**改前端必须跑它。** 这套页面是单文件内联脚本，语法错一个字符整页白屏，而白屏在
Python 测试里看不出来。
