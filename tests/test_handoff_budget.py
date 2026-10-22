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
# 条目的终止边界：下一条日期条目，或下一个 markdown 标题。
BOUNDARY_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|#{1,6}\s)")

OVERFLOW_HINT = (
    f"超出的部分请滚进 {ARCHIVE_HINT}（按日期倒序，插在对应日期分组最前面）；"
    "实证过程、否决记录、复现数据写进 docs/REVIEW_FINDINGS_*.md 或对应专项方案，"
    "HANDOFF 条目只留结论、影响和链接。"
)


def _entries(lines):
    """把以 YYYY-MM-DD 开头的日期条目切出来，返回 [(起始行号, 文本)]。

    边界取「下一条日期条目」与「下一个 markdown 标题」里先到的那个，条目不会越过
    章节标题把无关内容吃进来。这条边界是 2026-10-21 的 review 实测出来的：当时边界
    一路吃到文件末尾，HANDOFF 里一句碰巧以日期开头的正文（R 系列那句「2026-08-02 的
    全量审查……」）把整个「## 接手步骤」节吞了进去，往那一节加 212 字节就会让守卫
    指着那句正文报「条目超长，请滚进档案」——既认错了对象，也给了一条对它无意义的
    指示。`tests/test_events.py` 的同类守卫本来就是按 markdown 标题切块的。
    """
    out = []
    for start, line in enumerate(lines):
        if not ENTRY_RE.match(line):
            continue
        end = start + 1
        while end < len(lines) and not BOUNDARY_RE.match(lines[end]):
            end += 1
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


class EntryBoundaryTest(unittest.TestCase):
    """条目边界本身的回归保护。

    原先只验了「超标会红」，漏了另一半：**不该红的时候不能红**。一个会在错误时机、
    以错误理由变红的守卫比没有更糟，它只会训练人去绕过它。
    """

    def test_entry_stops_at_next_heading(self):
        """无关章节变长不得把日期条目撑爆——这正是 2026-10-21 实测到的误报。"""
        lines = [
            "2026-08-02 一句碰巧以日期开头的正文。",
            "",
            "## 接手步骤",
            "",
            "填充" * 2000,
        ]
        entries = _entries(lines)
        self.assertEqual(len(entries), 1)
        line_no, body = entries[0]
        self.assertEqual(line_no, 1)
        self.assertNotIn("填充", body)
        self.assertLessEqual(len(body.encode("utf-8")), MAX_ENTRY_BYTES)

    def test_entry_stops_at_next_dated_entry(self):
        """同一节里相邻的两条条目仍然各算各的。"""
        lines = [
            "2026-10-20 第一条。",
            "正文一。",
            "2026-10-19 第二条。",
            "正文二。",
        ]
        entries = _entries(lines)
        self.assertEqual([line_no for line_no, _ in entries], [1, 3])
        self.assertNotIn("第二条", entries[0][1])
        self.assertNotIn("正文二", entries[0][1])

    def test_real_handoff_entries_do_not_swallow_sections(self):
        """真实 HANDOFF 上没有任何条目跨越章节标题。"""
        lines = HANDOFF.read_text(encoding="utf-8").split("\n")
        for line_no, body in _entries(lines):
            with self.subTest(line=line_no):
                self.assertFalse(
                    [x for x in body.split("\n")[1:] if x.startswith("#")],
                    f"第 {line_no} 行起的条目吃进了章节标题",
                )


if __name__ == "__main__":
    unittest.main()
