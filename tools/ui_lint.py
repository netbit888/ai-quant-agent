"""UI 表现层守门脚本：token 之外不许出现裸值。

用法：
    .venv/Scripts/python.exe tools/ui_lint.py            # 检查
    .venv/Scripts/python.exe tools/ui_lint.py --strict   # 连装饰性裸 hex 也报错

为什么要有这个：
    面板模板曾经有 115 处裸 font-size、59 处裸 border-radius，而 --fs-* /
    --r-* token 只被引用了 5 / 6 次。token 写了不等于在用——没有自动化检查，
    收敛完的下一次提交就会漂回去。

检查范围（模板已按视觉区块拆成 tools/ui/ 分片，见 live/uiparts.py）：
    1. css/*.css：font-size: <裸 px>      → 必须用 --fs-*（跳过 base.css 的 :root）
    2. css/*.css：border-radius: <裸 px>  → 必须用 --r-*（0 允许）
    3. css/*.css：裸 hex 颜色             → 默认只统计；--strict 下报错
    4. html/ + js/：内联 style 里的裸 font-size
    5. html/ + js/：重复的 DOM id（见下）

    报错带**文件名:行号**——这也是拆分的收益之一：以前只有全局行号，改完
    模板行号一漂就得重新数。

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
UI = ROOT / "tools" / "ui"

# 装饰性用途（图表描边、一次性高光）允许保留裸 hex；--strict 会收紧。
HEX_BUDGET = 45


def parts(sub: str, suffix: str) -> list[Path]:
    return sorted((UI / sub).glob(f"*{suffix}"))


def blank_root_block(text: str) -> str:
    """把 :root 块抹成等长空白，保持行号不变（那里是 token 的定义处）。"""
    if ":root {" not in text:
        return text
    i0 = text.index(":root {")
    i1 = text.find("\n  }", i0)
    if i1 < 0:
        return text
    seg = text[i0:i1 + 4]
    return text[:i0] + re.sub(r"[^\n]", " ", seg) + text[i0 + len(seg):]


def lineno(text: str, pos: int) -> int:
    return text[:pos].count("\n") + 1


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    if not UI.is_dir():
        print(f"✗ 找不到模板分片目录：{UI}")
        return 1

    errors: list[str] = []
    notes: list[str] = []

    css_files = parts("css", ".css")
    for path in css_files:
        css = blank_root_block(path.read_text(encoding="utf-8"))
        rel = f"css/{path.name}"
        for m in re.finditer(r"font-size:\s*([\d.]+px)", css):
            errors.append(f"{rel}:{lineno(css, m.start())} 裸 font-size "
                          f"{m.group(1)}，改用 --fs-*")
        for m in re.finditer(r"border-radius:\s*([\d.]+px|50%)", css):
            errors.append(f"{rel}:{lineno(css, m.start())} 裸 border-radius "
                          f"{m.group(1)}，改用 --r-*")

    hexes = [h for p in css_files
             for h in re.findall(r"#[0-9a-fA-F]{3,8}", p.read_text(encoding="utf-8"))]
    uniq = sorted(set(hexes))
    notes.append(f"裸 hex：{len(hexes)} 处 / {len(uniq)} 种（预算 {HEX_BUDGET}）")
    if len(uniq) > HEX_BUDGET:
        msg = f"裸 hex 超预算：{len(uniq)} > {HEX_BUDGET} —— {uniq[:12]}"
        (errors if strict else notes).append(msg)

    inline = parts("html", ".html") + parts("js", ".js")
    for path in inline:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"style=\"[^\"]*font-size:\s*([\d.]+px)", text):
            errors.append(f"{path.name}:{lineno(text, m.start())} 内联裸 font-size "
                          f"{m.group(1)}")

    # 重复 id：getElementById 只认第一个，后写入的那份会被静默吞掉。
    # 跳过模板字符串拼出来的（id="${...}"），那不是静态 id。
    seen: dict[str, list[str]] = {}
    for path in inline:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'(?<![\w-])id="([^"$]+)"', text):
            seen.setdefault(m.group(1), []).append(
                f"{path.name}:{lineno(text, m.start())}")
    for name, where in sorted(seen.items()):
        if len(where) > 1:
            errors.append(f"重复 DOM id：#{name} 出现 {len(where)} 次 —— "
                          f"{', '.join(where[:4])}（getElementById 只返回第一个）")

    for n in notes:
        print("  ·", n)
    if errors:
        print(f"\n✗ {len(errors)} 处违规：")
        for e in errors[:40]:
            print("   -", e)
        return 1
    print(f"\n✓ token 层干净：{len(css_files)} 个 CSS 分片没有裸 "
          f"font-size / border-radius")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
