# 面板 UI 地图

改 UI 之前先看这张表，**不要整目录读**。模板已从单文件 7132 行拆成
`tools/ui/` 下的 36 个分片，按视觉区块切；每个分片单独读都是几百行，
定位靠「要改的东西属于哪个视图」，不再靠全文 grep。

## 拼接方式

`tools/ui/index.html` 是一张目录（55 行）：`<!--@include 相对路径-->` 独占一行，
整行替换为该文件全部内容。拼装结果与拆分前的单文件**逐字节相同**。

- Python 侧：`live/uiparts.py::load_template()`（面板每次请求 `/` 时按 mtime 缓存拼装）
- Node 侧：`tools/ui_assemble.js::assemble()`（冒烟测试用）
- 两边结果由 `tests/test_ui_parts.py::test_node_and_python_agree` 比对，别只改一边

**分片共享同一个 `<style>` / `<script>` 作用域** —— 拆的是文件，不是作用域。
所以 CSS/JS 的 include 顺序有意义（后面覆盖前面、重复 `const` 直接语法错），
也**不要**加分片的 `import` / `export` / IIFE 包裹。

## 目录

| 文件 | 行 | 职责 |
|---|---:|---|
| `index.html` | 55 | 目录：`<head>`、`<body>` 骨架、include 顺序 |
| `css/base.css` | 167 | `:root` token、reset、布局、通用组件、页脚、卡片动效 |
| `css/desk.css` | 389 | 做单台：状态带、决策环、持仓面板、决策流页、设置舱外框 |
| `css/flow.css` | 123 | 决策流独立页、矮窗口紧凑排版（`.tight`） |
| `css/setup.css` | 105 | 设置舱内部 |
| `css/charts.css` | 167 | 滚动条、SVG 曲线、hover 浮窗、表格通用规则 |
| `css/live.css` | 211 | 账户条、做单台一屏收口、近期质量 |
| `css/modals.css` | 190 | 付费确认、交易溯源、应用内更新、图表最大化、人工信号弹窗 |
| `css/score.css` | 55 | 交易评分卡 |
| `css/autotrade.css` | 296 | AI 做单参数、卡片排版、工具条、常驻状态带 |
| `css/overlay.css` | 132 | 全局 toast、全屏提醒幕布 |
| `css/overrides.css` | 29 | 视觉层收敛补丁（命中区/断点/分章） |
| `html/modals.html` | 142 | 五个弹窗 + 通用确认框 `#modalMask` |
| `html/view-desk.html` | 232 | 做单台：`#atBar` 状态带、账户条、`#live-desk` |
| `html/view-flow.html` | 36 | 决策流页 `#live-flow` |
| `html/view-review.html` | 167 | 复盘台 `#live-review` |
| `html/view-setup.html` | 219 | 设置舱 `#live-setup` |
| `html/footer.html` | 9 | 页脚：程序名 / 版本 / 检查更新 |
| `html/curtain.html` | 28 | 全屏提醒幕布 `#atCurtain`（在 `</script>` 之后） |
| `js/bridge.js` | 243 | pywebview 桥、格式化工具、`drawArea`/`drawBars` 图表原语 |
| `js/review.js` | 331 | `loadLive`/`renderLive` 主轮询、账户切换、账户条秒针与数据新鲜度（`paintAcctClock`） |
| `js/autotrade.js` | 162 | `loadAutoTrade`、`renderStatusBar`、反馈面板 |
| `js/desk-ring.js` | 275 | `renderRing`（决策环 + 8 格位）、`setLed` |
| `js/desk-pos.js` | 215 | `renderPos`（浮盈尺、保本三态；趋势单的线叫「复核门槛」不叫止盈；轴上限按峰值阶梯放大） |
| `js/desk-gate.js` | 133 | `renderGateLeds`（6 盏闸门灯 + 设置舱参数表） |
| `js/desk-auto.js` | 400 | `renderAutoTrade`：做单台汇总 + 决策流表格（`#rowsAuto`）+ 冷静期标签 |
| `js/desk-prefs.js` | 392 | 做单参数读取/回填（`atMode`/`atSymbol`/`atLots`/`applyPrefs`）、toast |
| `js/lessons.js` | 194 | 教训库统计、经验库包导入导出 |
| `js/model-switch.js` | 177 | 供应商 → 模型两级下拉 |
| `js/setup-wizard.js` | 101 | 首启配置向导 |
| `js/app-exit.js` | 301 | 退出流程、应用内更新下载/校验 |
| `js/curtain.js` | 112 | 全屏幕布状态 |
| `js/chart-max.js` | 201 | 图表最大化、单笔交易溯源弹窗、`stopAutoTrade` |
| `js/session-review.js` | 402 | `renderSession`：本场复盘、行为标记、备注草稿 |
| `js/scoring.js` | 86 | 交易评分、保本表 |
| `js/trade-quality.js` | 373 | `renderQuality`、AI 建议、`confirmApiCall` 弹窗栈 |
| `js/sig-modal.js` | 609 | 人工确认信号弹窗、提示音/静音/置顶/标题闪烁、三档轮询节拍（1s/3s/20s） |

## 「我要改 X」去哪

| 要改什么 | 先看 |
|---|---|
| 决策环（大字、8 格位、p_up 条、理由原话） | `js/desk-ring.js` + `css/desk.css` |
| 持仓面板（浮盈刻度尺、保本三态） | `js/desk-pos.js` + `css/desk.css` |
| 6 盏闸门灯 / 设置舱参数表 | `js/desk-gate.js` |
| 顶部常驻状态带 `#atBar` | `js/desk-auto.js`（`renderAutoTrade`）+ `css/autotrade.css` |
| 辅助模式的冷静期倒计时（`冷静期 · 还剩 m:ss`） | `js/desk-auto.js` 的 `atCooldown()` / `atMmSs()`；数据来自 `summary().assist_deny_until` + `server_ts`，后端不另算 |
| 决策流表格（列、跳过行开关） | `js/desk-auto.js`（`#rowsAuto`）+ `css/flow.css` |
| 做单参数（品种/间隔/手数/纸面真实） | `js/desk-prefs.js` + `html/view-setup.html`（手数档位另有 `live/prefs.py` 的 `LOTS`，两边由 `tests/test_lot_scale.py` 钉住） |
| 账户条 / 余额净值 | `html/view-desk.html` + `css/live.css` + `js/review.js` |
| 复盘台（本场复盘、行为标记） | `js/session-review.js` + `html/view-review.html` |
| 交易评分 / 近期质量 | `js/scoring.js`、`js/trade-quality.js` |
| 经验库、教训库统计 | `js/lessons.js` |
| 模型切换下拉 | `js/model-switch.js` |
| 首启向导 | `js/setup-wizard.js` |
| 更新弹窗的结构（日志能滚、按钮常在） | `css/modals.css` + `js/app-exit.js`（`tests/test_updater.py` 锁着） |
| 全屏提醒幕布 | `html/curtain.html` + `css/overlay.css` + `js/curtain.js` |
| 颜色/字号/圆角 token | `css/base.css` 的 `:root`（改完跑 `tools/ui_lint.py`） |

## 三条纪律

1. **别把代码塞回 `index.html`**。它只放 include 指令和骨架，
   `tests/test_ui_parts.py` 会在它超过 100 行或出现 `function `/`margin:` 时报错。
2. **加文件必须加 include**。孤儿分片有测试拦（`test_no_orphan_parts`），
   否则改了不生效，很容易被误判成缓存问题。
3. **改完跑这四条**：
   ```
   .venv/Scripts/python.exe -m unittest discover -s tests
   .venv/Scripts/python.exe tools/ui_lint.py
   node tools/smoke_dashboard.js          # 需先起面板抓 live_probe.json
   ```
   `load_template()` 按 mtime 缓存，**改完分片刷新浏览器即生效**，不用重启面板。
