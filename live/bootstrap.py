"""启动期配置：从 state 还原上次配置、启动前自检。

为什么单独一层
--------------
这两个函数都不是纯计算：`cfg_from_state` 要读 state 文件，`validate_cfg`
要查账户结算币种和手数白名单。它们以前留在 `autotrade` 里，于是
`live/cfg.py` 想保持「只 import dataclasses」就只能把「配置」这个概念
裂成两处：schema 在 cfg，校验与还原在 autotrade。

拆出来之后依赖是单向的：本模块 -> {storage, cfg, market, prefs}。
反向没有——`cfg.py` 不许 import 本模块（那会立刻成环，因为 storage 为了
`_log_signal` 的类型注解要 import Cfg）。
"""

from live.cfg import TRADABLE_SYMBOLS, Cfg, money_label
from live.market import _acct_currency
from live.storage import load_state


def cfg_from_state(login: int | str | None = None) -> Cfg | None:
    """还原上次运行的配置。没有记录返回 None。"""
    raw = load_state(login).get("cfg")
    if not isinstance(raw, dict):
        return None
    clean = {k: v for k, v in raw.items() if k in Cfg.__dataclass_fields__}
    # 2026-10-29 冷却改成按方向分档，旧存档里只有 cooldown_sec。存档里
    # **没有 cooldown_same_sec 且 cooldown_sec 恰好是老的默认值 30** → 当成
    # 「没配过」，丢掉它让新默认（反向 60 / 同向 180）生效。用户自己配过的
    # 其它值（比如 120）原样保留，不替人做决定。
    if "cooldown_same_sec" not in clean and int(clean.get("cooldown_sec") or 0) == 30:
        clean.pop("cooldown_sec", None)
    try:
        return Cfg(**clean)
    except (TypeError, ValueError):
        return None


def validate_cfg(cfg: Cfg) -> list[str]:
    """配置自检。返回问题列表，空 = 通过。

    这里放的是**逻辑上不可能成立**的配置：与其让它跑起来后悄悄违反规则，
    不如启动时就说清楚。
    """
    issues: list[str] = []
    # 品种必须在白名单里：SYMBOLS 里没测过 point/contract/spread 的品种，
    # 点差成本会退回 0（把「没测过」显示成「零成本」），风险直接失控。
    if cfg.symbol not in TRADABLE_SYMBOLS:
        issues.append(
            f"品种 {cfg.symbol} 不在可交易白名单（{'/'.join(TRADABLE_SYMBOLS)}）："
            "没有实测 point/contract/spread 的品种不能上真单"
        )
    # 3 秒是物理下限：一次模型调用实测 ~2.6 秒，再短线程就在堆请求，
    # 上一轮没回来下一轮已经开始，决策全挤在一起。
    if cfg.interval_sec < 3:
        issues.append(
            "决策间隔不能小于 3 秒（模型单次调用约 2.6 秒，再快只会堆积请求）"
        )
    # 对齐偏移不能是负数：负偏移等于"在 K 线收盘之前判"，正是这个开关要消掉的
    # 东西。上界不设——settle 接近一个周期时 _wait_deadline 会夹到半个周期，
    # 夹住的结果仍是"每根 K 线判一次"，只是相位不同，不至于失效。
    if float(getattr(cfg, "align_settle_sec", 0.0) or 0.0) < 0:
        issues.append(f"收盘对齐偏移不能为负（{cfg.align_settle_sec:g} 秒）")
    if cfg.lots <= 0:
        issues.append("手数必须大于 0")
    else:
        # 手数必须落在界面那几档里。券商那边是 round(lots, 2)：0.013 会被
        # 静默改成 0.01（界面说 0.013、实际 0.01），0.001 直接报 10014。
        # 白名单和 prefs / 模板同源，API 直接 POST 也绕不过去。
        from live import prefs

        if not prefs.valid_lots(cfg.lots):
            issues.append(
                f"开仓手数只接受 {'/'.join(f'{x:g}' for x in prefs.LOTS)} 手，"
                f"收到 {cfg.lots}")
    # 同向冷却比反向还短 = 这台机器专门鼓励同向重进，与它存在的理由相反。
    # 相等是允许的（等于回到单一冷却，行为可控）。
    if cfg.cooldown_same_sec < cfg.cooldown_sec:
        issues.append(
            f"同向冷却 {cfg.cooldown_same_sec} 秒不能短于反向冷却 {cfg.cooldown_sec} 秒"
        )
    # 冷却不再有「必须 <60 秒」的上限——「平仓后 1 分钟内必须重新开仓」的硬需求
    # 已删除（它和复盘体系的报复单标记直接矛盾，且是输出量要求不是质量要求）。
    # 冷却只是防连环单的下限，用户想配 2 分钟冷却应该允许。
    # 止损/止盈这对参数是配套的，单独调一个只会换一种亏法：
    #   止盈门槛 ≤ 止损 → 盈亏比还在 1 附近甚至更差，白加止损；
    #   止损太接近点差 → 直接被点差打掉，等于每开一单就主动交一次过路费。
    #
    # 金额单位是**账户结算货币**：美元户 0.5 = 0.5 美元，美分户 0.5 = 0.5 分。
    # 这条判据在两种账户下天然等价 —— 价距 = 金额 / (合约 × 手数 × 每美元折
    # 多少账户货币)，美分户的金额小 100 倍、分母也大 100 倍，比值不变。
    # 所以阈值不需要按币种改，只需要把单位标出来，别让人对着两个 3.00 猜。
    if cfg.sl_usd > 0 and cfg.sl_usd < 0.5:
        unit = money_label(_acct_currency())
        issues.append(
            f"止损 {cfg.sl_usd:.2f} {unit} 太小：单笔点差就 0.24 的价距，"
            f"止损不到 0.5 {unit} 等于一开仓就被点差打掉"
        )
    if cfg.tp_min_usd > 0 and cfg.sl_usd > 0 and cfg.tp_min_usd <= cfg.sl_usd:
        issues.append(
            f"止盈门槛 {cfg.tp_min_usd:.2f} 不高于止损 {cfg.sl_usd:.2f}，"
            "盈亏比仍 ≤ 1，这套参数没解决必亏问题"
        )
    # 趋势单的止盈门槛同一套盈亏比约束。0 = 关掉宽止盈（退回 tp_min_usd），
    # 是合法配置；但只要开了，就不能比止损还窄——趋势打法赚的本来就是
    # 「拿够一段」和震荡单不同，门槛比止损窄等于把趋势单削成震荡单。
    if cfg.tp_min_trend < 0:
        issues.append(f"趋势止盈门槛不能为负（{cfg.tp_min_trend:.2f}）")
    if cfg.tp_min_trend > 0 and cfg.sl_usd > 0 and cfg.tp_min_trend <= cfg.sl_usd:
        issues.append(
            f"趋势止盈门槛 {cfg.tp_min_trend:.2f} 不高于止损 {cfg.sl_usd:.2f}，"
            "趋势单被削成盈亏比 ≤ 1 的震荡单，分 regime 就白分了"
        )
    if cfg.max_hold_trend < 0:
        issues.append(f"趋势持仓上限不能为负（{cfg.max_hold_trend} 秒）")
    # 趋势上限比震荡上限还短 = 配置矛盾：趋势单拿的波段比震荡单还短，
    # tp_min_trend 再宽也永远等不到，等于悄悄退回震荡参数。
    if cfg.max_hold_trend > 0 and cfg.max_hold_sec > 0 \
            and cfg.max_hold_trend < cfg.max_hold_sec:
        issues.append(
            f"趋势持仓上限 {cfg.max_hold_trend}s 比震荡上限 {cfg.max_hold_sec}s 还短："
            "宽止盈永远等不到，分 regime 形同虚设"
        )
    # 峰值停滞唤醒（2026-10-29）：这条规则只"放行"、不改任何阈值，所以配歪了
    # 最坏结果是「多问几次模型」或「完全不唤醒」，不会改变亏损结构。
    # 校验只拦语义上不可能成立的组合，不去拦"激进"。
    if getattr(cfg, "stale_sec", 0) < 0:
        issues.append(f"停滞唤醒窗口不能为负（{cfg.stale_sec} 秒）")
    if not 0.0 <= float(getattr(cfg, "stale_frac", 0.0)) <= 1.0:
        issues.append(f"停滞唤醒的浮盈比例必须落在 0~1（{cfg.stale_frac}）")
    if getattr(cfg, "stale_reask_sec", 0) < 0:
        issues.append(f"停滞唤醒的重问间隔不能为负（{cfg.stale_reask_sec} 秒）")
    # 到过门槛后的复核节拍：0 是合法的「关掉」，只有负数报错。
    if getattr(cfg, "tp_reask_sec", 0) < 0:
        issues.append(f"到过门槛后的复核间隔不能为负（{cfg.tp_reask_sec} 秒）")
    # 止损快查的间隔只有下限有意义：< 2 秒时 MT5 tick 读取的抖动开始比价格
    # 变化本身大，白白空转。**上界不在这里拦**——配得比决策间隔大也不报错，
    # _wait_next 会把它夹到 interval_sec（等于没开）。理由：这是个纯粹的
    # 安全网参数，配歪了最坏结果是"回到旧行为"，不该让人启动不了。
    # 0 是合法的「关掉」。
    if getattr(cfg, "guard_sec", 0.0) and cfg.guard_sec > 0 and cfg.guard_sec < 2:
        issues.append(
            f"止损快查间隔 {cfg.guard_sec:.1f} 秒太短（tick 读取抖动比价差还大），最小 2 秒"
        )
    # 近门槛档位同样只有下限有意义，且不可能比远档还粗——粗的那档就该叫远档。
    if getattr(cfg, "guard_near_sec", 0.0) and cfg.guard_near_sec > 0 and cfg.guard_near_sec < 2:
        issues.append(
            f"近门槛快查间隔 {cfg.guard_near_sec:.1f} 秒太短（同 guard_sec 口径），最小 2 秒"
        )
    if getattr(cfg, "guard_sec", 0.0) and getattr(cfg, "guard_near_sec", 0.0) \
            and cfg.guard_near_sec > cfg.guard_sec:
        issues.append(
            f"近门槛快查间隔 {cfg.guard_near_sec:.1f} 秒不能比远档 {cfg.guard_sec:.1f} 秒还粗"
        )
    # 辅助模式的参数。**不校验 assist 与 dry_run 的互斥**：
    # 「辅助 + 纸面」是合法且有用的组合（弹窗照弹、同意只记账），
    # 用来先把流程和手感跑顺，再上真钱。
    if cfg.assist:
        if cfg.assist_entry_wait_sec < 5:
            issues.append(
                f"入场确认时限 {cfg.assist_entry_wait_sec} 秒太短：弹窗刚出现就过期，"
                "等于替你把所有单子都否了"
            )
        if cfg.assist_exit_wait_sec < 5:
            issues.append(
                f"出场确认时限 {cfg.assist_exit_wait_sec} 秒太短：同上，"
                "到点会自动按模型决定平仓，太短会变成「实际上没人看过」"
            )
        if cfg.assist_max_slip_usd <= 0:
            issues.append(
                f"价格漂移阈值 {cfg.assist_max_slip_usd:.2f} 必须大于 0："
                "设 0 等于任何价格变动都作废，一单也开不出去"
            )
        if cfg.assist_deny_cooldown < 0:
            issues.append("否决冷却不能是负数")
    return issues
