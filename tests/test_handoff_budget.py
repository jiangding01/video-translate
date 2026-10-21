"""HANDOFF.md 的体量守卫。

`AGENTS.md` 把 HANDOFF 指定为「进行中的工作与下一步」的入口，接手方会**整份**读进
上下文，所以它必须停在「一次读完不心疼」的体量。

2026-10-21 之前它长到 741 行 / 234KB，其中 74% 是 4 天内追加的日期条目流水账
（逐提交实测：7-15 为 12.7KB，10-11 为 60.9KB，10-17 为 112.7KB，10-20 为 239.9KB）。
那一轮之前也不是没有约定——handoff skill 自 7 月起就写了 200 行上限，文件当时已 307 行，
三个月从未达标。纯文字约定在这个仓库被证明无效，而且 `codex/` 分支的协作者只读仓库里的
`AGENTS.md`，根本不加载那个 skill。所以这条约束必须由测试强制，和
`test_readme_cli_reference.py` / `test_events.py` 守 README 与事件契约是同一个路子。

单条上限不是让人少写，是逼迫分流：实证过程、否决记录、复现数据属于
`docs/REVIEW_FINDINGS_*.md` 或对应专项方案，HANDOFF 条目只留结论、影响和链接。
"""

import re
import unittest
from pathlib import Path

HANDOFF = Path(__file__).resolve().parent.parent / "HANDOFF.md"
ARCHIVE_HINT = "docs/HANDOFF_ARCHIVE.md"

MAX_LINES = 200
MAX_BYTES = 32 * 1024
MAX_ENTRY_BYTES = 2 * 1024

ENTRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")

OVERFLOW_HINT = (
    f"超出的部分请滚进 {ARCHIVE_HINT}（按日期倒序，插在对应日期分组最前面）；"
    "实证过程、否决记录、复现数据写进 docs/REVIEW_FINDINGS_*.md 或对应专项方案，"
    "HANDOFF 条目只留结论、影响和链接。"
)


def _entries(lines):
    """把以 YYYY-MM-DD 开头的日期条目切出来，返回 [(起始行号, 文本)]。"""
    starts = [i for i, line in enumerate(lines) if ENTRY_RE.match(line)]
    out = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        out.append((start + 1, "\n".join(lines[start:end]).rstrip("\n")))
    return out


class HandoffBudgetTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            HANDOFF.is_file(),
            f"{HANDOFF.name} 必须存在：AGENTS.md 把它指定为接手入口，缺失即交接链路断了",
        )
        self.text = HANDOFF.read_text(encoding="utf-8")
        self.lines = self.text.split("\n")

    def test_total_lines_within_budget(self):
        actual = len(self.lines)
        self.assertLessEqual(
            actual,
            MAX_LINES,
            f"HANDOFF.md 共 {actual} 行，超过 {MAX_LINES} 行上限。{OVERFLOW_HINT}",
        )

    def test_total_bytes_within_budget(self):
        actual = len(self.text.encode("utf-8"))
        self.assertLessEqual(
            actual,
            MAX_BYTES,
            f"HANDOFF.md 共 {actual / 1024:.1f}KB，超过 {MAX_BYTES // 1024}KB 上限"
            f"（行数达标不代表体量达标：2026-10 期间平均行长从 41B 涨到 324B）。{OVERFLOW_HINT}",
        )

    def test_each_dated_entry_within_budget(self):
        for line_no, entry in _entries(self.lines):
            size = len(entry.encode("utf-8"))
            with self.subTest(line=line_no):
                self.assertLessEqual(
                    size,
                    MAX_ENTRY_BYTES,
                    f"HANDOFF.md 第 {line_no} 行起的日期条目 {size / 1024:.1f}KB，"
                    f"超过单条 {MAX_ENTRY_BYTES // 1024}KB 上限。{OVERFLOW_HINT}",
                )


if __name__ == "__main__":
    unittest.main()
