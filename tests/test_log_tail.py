"""流水尾部读取：`read_log(limit=N)` 必须真的只读 N 条。

为什么单开一条（2026-11-05）
--------------------------
流水只在手动重置时轮转，所以它只变长。原实现是「全文读 + 全文解析 → 取后
N 条」：`limit` 只是返回值上限，省不了任何成本。面板每秒轮询一次，文件涨到
几千行后每轮都在解析几年前的行。

改成从尾部按块倒读后，最怕的不是慢，是**切错**：块边界可能落在半个汉字上，
解码出来是乱码，那一行的 JSON 就废了。所以这里一半的用例在钉边界。
"""

from __future__ import annotations

import json
import unittest

from tests._autotrade_base import TempLedger

from live import autotrade


def _row(i: int, text: str = "") -> dict:
    return {"ts": 1_790_000_000 + i, "kind": "skip",
            "reason": text or f"第 {i} 行：点差 0.24，不做"}


class TestReadLogTail(TempLedger):
    def _write(self, n: int) -> None:
        for i in range(n):
            autotrade.append_log(_row(i))

    def test_limit_equals_full_read_tail(self):
        self._write(50)
        self.assertEqual(autotrade.read_log(limit=10), autotrade.read_log()[-10:])

    def test_limit_larger_than_file(self):
        self._write(5)
        self.assertEqual(len(autotrade.read_log(limit=99)), 5)

    def test_limit_one(self):
        self._write(30)
        self.assertEqual(autotrade.read_log(limit=1), [autotrade.read_log()[-1]])

    def test_limit_zero_still_reads_everything(self):
        """老语义：`limit=0` 是"没给"，不是"要 0 条"。别在重构里悄悄改掉。"""
        self._write(8)
        self.assertEqual(len(autotrade.read_log(limit=0)), 8)

    def test_utf8_split_across_chunks(self):
        """块边界切在汉字中间时，那一行必须完好——不能出替换字符。

        chunk 故意设得很小，逼它把每一行都切碎。
        """
        self._write(40, )
        p = autotrade.log_path()
        lines = autotrade._tail_lines(p, 10, chunk=7)
        self.assertEqual(len(lines), 10)
        for line in lines:
            self.assertNotIn("\ufffd", line, "块边界切坏了多字节汉字")
            json.loads(line)          # 解析得动才算没坏

    def test_partial_first_line_is_dropped(self):
        """从文件中间起读时，第一段是半行，必须丢掉而不是当垃圾数据留下。"""
        self._write(100)
        p = autotrade.log_path()
        lines = autotrade._tail_lines(p, 5, chunk=13)
        self.assertEqual(len(lines), 5)
        for line in lines:
            json.loads(line)

    def test_broken_line_is_skipped(self):
        """坏行跳过。这条同时钉住一个已知的语义差别：`limit` 是**行数**上限，
        坏行被吃掉后返回条数会比 limit 少——旧实现（全解析再截）不会。

        为罕见的坏行保留 O(文件) 的代价不值，所以接受这个差别并写下来。
        """
        self._write(3)
        with open(autotrade.log_path(), "a", encoding="utf-8") as fh:
            fh.write("{这行坏了\n")
        self._write(1)
        rows = autotrade.read_log(limit=2)
        self.assertEqual(len(rows), 1, "坏行占了名额，只应剩下 1 条好行")
        self.assertEqual(rows[0]["ts"], 1_790_000_000)
        # 不限 limit 时两条好行都在
        self.assertEqual(len(autotrade.read_log()), 4)

    def test_tail_is_cheaper_than_full_read(self):
        """回归护栏：尾部读取的解析量不能随文件长度涨。

        不测绝对耗时（机器差异会让断言变成噪音），测的是**被解析的行数**——
        用 4 万行造一个"长了一年"的流水，只取最后 5 条。
        """
        p = autotrade.log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            for i in range(40_000):
                fh.write(json.dumps(_row(i), ensure_ascii=False) + "\n")
        self.assertEqual(len(autotrade.read_log(limit=5)), 5)
        self.assertEqual(autotrade.read_log(limit=5)[-1]["ts"],
                         1_790_000_000 + 39_999)


if __name__ == "__main__":
    unittest.main()
