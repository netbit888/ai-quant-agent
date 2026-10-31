"""UI 表现层守门脚本：token 之外不许出现裸值。

用法：
    .venv/Scripts/python.exe tools/ui_lint.py            # 检查
    .venv/Scripts/python.exe tools/ui_lint.py --strict   # 连装饰性裸 hex 也报错

为什么要有这个：
    dashboard_template.html 曾经有 115 处裸 font-size、59 处裸 border-radius，
    而 --fs-* / --r-* token 只被引用了 5 / 6 次。token 写了不等于在用——
    没有自动化检查，收敛完的下一次提交就会漂回去。

检查范围（只在 <style> 段内，且跳过 :root 块本身——那里是 token 的定义处）：
    1. font-size: <裸 px>        → 必须用 --fs-*
    2. border-radius: <裸 px/%>  → 必须用 --r-*（0 允许）
    3. 裸 hex 颜色                → 默认只统计；--strict 下报错
    4. HTML/JS 段内联 style 里的裸 font-size
    5. 重复的 DOM id（见下）

为什么第 5 条值得单独一条规则：
    做单台那 6 盏闸门灯的容器叫 #gateBox，策略评估页的 Gate 判定卡也叫
    #gateBox。getElementById 只返回**文档里第一个**，而策略评估页在启动时
    无条件加载——于是四颗 PASS 被写进了做单台的闸门灯里，6 盏灯连同
    #ledSl 一起被销毁，setLed 找不到元素就静默返回，刷新才复原。
    同一屏两个东西共用一个 id，症状是"做单台里凭空多出一块 Gate 2"，
    而且没人会去怀疑 id。

退出码：0 通过 / 1 有违规。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "tools" / "dashboard_template.html"

# 装饰性用途（图表描边、一次性高光）允许保留裸 hex；--strict 会收紧。
HEX_BUDGET = 45


def css_and_body(src: str) -> tuple[str, str, str]:
    """返回 (root 块, 其余 CSS, HTML+JS 段)。"""
    i0 = src.index("<style>") + len("<style>")
    i1 = src.index("</style>")
    head, css, tail = src[:i0], src[i0:i1], src[i1:]
    rs = css.index(":root {")
    re_ = css.index("\n  }", rs)
    return css[:re_], css[re_:], tail


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    src = TPL.read_text(encoding="utf-8")
    _root, css, tail = css_and_body(src)

    errors: list[str] = []
    notes: list[str] = []

    for m in re.finditer(r"font-size:\s*([\d.]+px)", css):
        line = css[: m.start()].count("\n") + 1
        errors.append(f"CSS 第 {line} 行：裸 font-size {m.group(1)}，改用 --fs-*")
    for m in re.finditer(r"border-radius:\s*([\d.]+px|50%)", css):
        line = css[: m.start()].count("\n") + 1
        errors.append(f"CSS 第 {line} 行：裸 border-radius {m.group(1)}，改用 --r-*")

    hexes = re.findall(r"#[0-9a-fA-F]{3,8}", css)
    uniq = sorted(set(hexes))
    notes.append(f"裸 hex：{len(hexes)} 处 / {len(uniq)} 种（预算 {HEX_BUDGET}）")
    if len(uniq) > HEX_BUDGET:
        msg = f"裸 hex 超预算：{len(uniq)} > {HEX_BUDGET} —— {uniq[:12]}"
        (errors if strict else notes).append(msg)

    for m in re.finditer(r"style=\"[^\"]*font-size:\s*([\d.]+px)", tail):
        line = tail[: m.start()].count("\n") + 1
        errors.append(f"HTML 第 {line} 行：内联裸 font-size {m.group(1)}")

    # 重复 id：getElementById 只认第一个，后写入的那份会被静默吞掉。
    # 跳过模板字符串拼出来的（id="${...}"），那不是静态 id。
    seen: dict[str, int] = {}
    for m in re.finditer(r'(?<![\w-])id="([^"$]+)"', tail):
        seen[m.group(1)] = seen.get(m.group(1), 0) + 1
    for name, n in sorted(seen.items()):
        if n > 1:
            errors.append(f"重复 DOM id：#{name} 出现 {n} 次（getElementById 只返回第一个）")

    for n in notes:
        print("  ·", n)
    if errors:
        print(f"\n✗ {len(errors)} 处违规：")
        for e in errors[:40]:
            print("   -", e)
        return 1
    print("\n✓ token 层干净：没有裸 font-size / border-radius")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
