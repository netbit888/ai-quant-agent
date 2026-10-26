"""从 CHANGELOG.md 里抽出指定版本的段落，作为 GitHub Release notes。

为什么要单独一个脚本：release.bat 是 ASCII-only 的批处理（UTF-8 中文会
让 cmd 读错位跑飞），做多行 Markdown 切分不是它该干的活。发版流程里
「每个 release 页只挂自己那段历史」就靠这个前置步骤。

用法::

    python tools/release_notes.py CHANGELOG.md 1.1.0 dist/release_notes_1.1.0.md

匹配 ``## v<ver>`` 或 ``## <ver>`` 开头的段落，到下一个 ``## `` 或文件尾为止。
找不到该版本段落就报错退出——发版 notes 缺失比发全量历史更糟，宁可失败。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def extract(text: str, version: str) -> str:
    """取出 version 对应的段落。找不到返回空串。"""
    head = re.compile(rf"^##\s+v?{re.escape(version)}(?:\s|$)", re.MULTILINE)
    m = head.search(text)
    if not m:
        return ""
    nxt = re.compile(r"^##\s+", re.MULTILINE)
    nxt_m = nxt.search(text, m.end())
    end = nxt_m.start() if nxt_m else len(text)
    return text[m.start():end].strip() + "\n"


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
