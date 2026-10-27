"""从 CHANGELOG.md 里抽出指定版本的段落，作为 GitHub Release notes。

为什么要单独一个脚本：release.bat 是 ASCII-only 的批处理（UTF-8 中文会
让 cmd 读错位跑飞），做多行 Markdown 切分不是它该干的活。发版流程里
「每个 release 页只挂自己那段历史」就靠这个前置步骤。

用法::

    python tools/release_notes.py CHANGELOG.md 1.1.0 dist/release_notes_1.1.0.md

匹配 ``## v<ver>`` 或 ``## <ver>`` 开头的段落，到下一个 ``## `` 或文件尾为止。
找不到该版本段落就报错退出——发版 notes 缺失比发全量历史更糟，宁可失败。

段落里若有 ``<!-- notes -->`` … ``<!-- /notes -->`` 包围块，**只取块内**。
这是为了把"CHANGELOG 写详细"和"弹窗只能显示短日志"分开：v1.5.3 那次就是
整篇 notes 摊进弹窗，把「下载并安装」顶到视口外，更新通道被自己的日志堵死。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 弹窗能舒服显示的正文长度上限。超了不拦发版（拦了更糟），但一定要吼出来。
MAX_NOTES_CHARS = 1200
NOTES_BLOCK = re.compile(r"<!--\s*notes\s*-->(.*?)<!--\s*/notes\s*-->", re.DOTALL)


def extract(text: str, version: str) -> str:
    """取出 version 对应的段落。找不到返回空串。

    段落内带 notes 标记块时只返回块内内容（页面上标题已有版本号，不用重复）。
    """
    head = re.compile(rf"^##\s+v?{re.escape(version)}(?:\s|$)", re.MULTILINE)
    m = head.search(text)
    if not m:
        return ""
    nxt = re.compile(r"^##\s+", re.MULTILINE)
    nxt_m = nxt.search(text, m.end())
    end = nxt_m.start() if nxt_m else len(text)
    section = text[m.start():end]
    block = NOTES_BLOCK.search(section)
    if block:
        return block.group(1).strip() + "\n"
    return section.strip() + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: release_notes.py <CHANGELOG.md> <version> <out.md>",
              file=sys.stderr)
        return 2
    src, version, out = Path(argv[1]), argv[2], Path(argv[3])
    section = extract(src.read_text(encoding="utf-8"), version)
    if not section:
        print(f"[ERROR] no section for v{version} in {src}", file=sys.stderr)
        return 1
    out.write_text(section, encoding="utf-8")
    print(f"[INFO] notes for v{version} -> {out} ({len(section)} chars)")
    if len(section) > MAX_NOTES_CHARS:
        print(f"[WARN] notes is {len(section)} chars (> {MAX_NOTES_CHARS}). "
              f"更新弹窗会很难看——请用 <!-- notes --> 标记块把正文收短。",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
