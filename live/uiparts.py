"""面板模板的拼装加载器。**源码是 `tools/ui/` 下的分片，不是单个 HTML。**

为什么拆：
    拆之前 dashboard_template.html 是 7132 行 / 29 万字符，其中 87% 是内联
    CSS + JS，全部塞在一个 `<script>` 里（717 个顶层变量、146 个函数共享
    作用域）。改一处 UI 要先在几千行里定位，整读一次就吃掉大半上下文预算；
    而改逻辑能只读一个函数。拆分后改决策环只看 js/desk-ring.js（274 行）+
    css/desk.css（389 行），一眼能定位。

拼装规则（只有一条，别加花样）：
    index.html 里 `<!--@include 相对路径-->` **独占一行**，整行替换为该文件
    的全部内容。不重排缩进、不补空行 —— 拼装结果与拆分前的单文件逐字节
    相同（tools/ui_split.py 负责校验这一点），所以任何渲染差异都只能来自
    你改的那几行。

    分片之间共享同一个 `<style>` / `<script>` 作用域：拆的是**文件**，不是
    作用域。因此
      * CSS 顺序有意义（后面的覆盖前面的）；
      * JS 顺序有意义（顶层 const 重复声明会直接 SyntaxError）；
      * 不要给分片加 `import` / `export` / IIFE 包裹。

缓存：
    每次请求 `/` 都会 stat 一遍所有分片，键（mtime、大小）没变就复用上次
    拼好的字节。开发时保存分片 → 浏览器刷新即生效，不用重启面板
    （这也是拆分前就有的行为，别改成启动时只读一次）。
"""
from __future__ import annotations

import re
from pathlib import Path

from live import appdirs

# 独占一行的 include 指令。行尾换行由分片自身提供，这里连行尾一起吃掉。
_INCLUDE = re.compile(r"^<!--@include\s+([^>\s]+)\s*-->\s*(\r?\n)?$")

_cache_key: tuple | None = None
_cache_val: bytes | None = None


class TemplateMissing(FileNotFoundError):
    """分片缺失或 index.html 不存在。面板据此回 500 "template missing"。"""


def ui_dir() -> Path:
    return appdirs.ui_dir()


def _read(path: Path) -> str:
    # newline="" 保留 CRLF：分片是原文件切出来的，换行风格必须原样带走。
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def assemble(index_text: str, base: Path) -> str:
    """纯函数：把 index.html 的 include 指令展开。便于单测和 node 侧对齐。

    找不到分片时抛 TemplateMissing，并指出是哪个 include —— 静默返回半截
    页面（少一段 CSS 还行，少一段 <div> 就是整页错位）是最难查的故障。
    """
    out: list[str] = []
    for lineno, line in enumerate(index_text.splitlines(keepends=True), 1):
        m = _INCLUDE.match(line)
        if not m:
            out.append(line)
            continue
        rel = m.group(1)
        part = base / rel
        if not part.is_file():
            raise TemplateMissing(
                f"tools/ui/index.html 第 {lineno} 行的 include 找不到文件：{rel}")
        out.append(_read(part))
    return "".join(out)


def _parts(index: Path) -> list[Path]:
    return [index, *sorted(index.parent.rglob("*.css")),
            *sorted(index.parent.rglob("*.js")),
            *sorted(index.parent.rglob("*.html"))]


def load_template() -> bytes:
    """返回拼装好的整页 HTML（bytes）。键没变时走缓存。"""
    global _cache_key, _cache_val

    index = ui_dir() / "index.html"
    if not index.is_file():
        raise TemplateMissing(f"面板模板目录缺少 index.html：{index}")

    files = _parts(index)
    key = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if key == _cache_key and _cache_val is not None:
        return _cache_val

    html = assemble(_read(index), index.parent).encode("utf-8")
    _cache_key, _cache_val = key, html
    return html
