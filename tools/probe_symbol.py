"""品种体检：连上当前终端的账户，列出所有含 XAU 的品种并逐个试报价。

为什么需要
----------
换账户（标准 -> 美分）后最常见的现象是「面板一直显示拿不到报价」。
根因往往不是行情断了，而是**品种名变了**：标准账户叫 XAUUSDm，
美分账户叫 XAUUSDc，代码里配的却是前者，`symbol_info_tick('XAUUSDm')`
对不存在的名字只返回 None，什么都不报。

这个脚本把「哪些品种真的存在、规格是多少、报价能不能读到」一次列出来，
避免靠猜。

用法：
    .venv/Scripts/python.exe -m tools.probe_symbol
    .venv/Scripts/python.exe -m tools.probe_symbol XAUUSDc     # 指定重点看谁
"""

from __future__ import annotations

import sys


def _fmt(v, nd=3):
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def main(argv: list[str]) -> int:
    try:
        import MetaTrader5 as mt5
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 导入 MetaTrader5 失败：{exc}")
        return 2

    targets = [a for a in argv if not a.startswith("-")]

    if not mt5.initialize(timeout=30_000):
        print(f"[x] initialize 失败：{mt5.last_error()}")
        print("    终端是否在跑、账户是否已登录？")
        return 3

    try:
        acct = mt5.account_info()
        term = mt5.terminal_info()
        print("=" * 62)
        if acct:
            print(f"账户   : {acct.login}  {acct.server}")
            print(f"币种   : {acct.currency}   余额 {acct.balance:.2f}  "
                  f"净值 {acct.equity:.2f}")
            print(f"可交易 : {acct.trade_allowed}   杠杆 1:{acct.leverage}")
        else:
            print(f"账户   : 读不到（{mt5.last_error()}）")
        if term:
            print(f"终端   : {term.name}   build {term.build}   "
                  f"连接 {term.connected}   算法交易 {term.trade_allowed}")
        print("=" * 62)

        # 1) 终端里所有黄金品种。换账户后名字会变（XAUUSDm → XAUUSDc），
        # 这一行就是判断「代码里配的那个名字到底存不存在」的直接证据。
        syms = mt5.symbols_get("XAU*")
        if syms is None:
            print(f"[!] symbols_get 失败：{mt5.last_error()}")
            syms = []
        found = [s.name for s in syms]
        print(f"终端里含 XAU 的品种 {len(found)} 个：")
        print("  " + ", ".join(sorted(found)))
        print()

        # 2) 重点品种逐个验
        check = targets or ["XAUUSDm", "XAUUSDc", "XAUUSD"]
        print("逐个体检：")
        hdr = (f"  {'品种':<10}{'可见':<6}{'point':<9}{'digits':<7}"
               f"{'contract':<10}{'spread':<8}{'filling':<8}{'报价'}")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for name in check:
            info = mt5.symbol_info(name)
            if info is None:
                print(f"  {name:<10}{'不存在':<6}  <- symbol_info 返回 None")
                continue
            if not info.visible:
                mt5.symbol_select(name, True)
                info = mt5.symbol_info(name) or info
            tick = mt5.symbol_info_tick(name)
            quote = (f"{tick.bid:.3f}/{tick.ask:.3f}" if tick else "None")
            print(f"  {info.name:<10}{str(info.visible):<6}"
                  f"{_fmt(info.point, 6):<9}{info.digits:<7}"
                  f"{_fmt(info.trade_contract_size, 2):<10}"
                  f"{info.spread:<8}{info.filling_mode:<8}{quote}")
        print()

        # 3) 美分账户的量纲提示
        if acct and acct.currency.upper() in ("USC", "XUSC", "CNT"):
            print(f"[i] 结算币种是美分（{acct.currency}）：余额 {acct.balance:.2f} "
                  f"= {acct.balance / 100:.2f} 美元。")
            print("    程序里 sl_usd / tp_min_usd 这类金额**按账户货币读**：")
            print("    美元户写 3.00 是 3 美元，美分户写 3.00 就是 3 分。")
            print("    不用自己换算——美分户的手数也小了 100 倍，价距是一样的。")
            print("    界面上所有金额都会带 USD / USC 后缀，看到没后缀就是没标全。")
    finally:
        # 单独跑体检才 shutdown；面板进程里绝不要这么做。
        mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
