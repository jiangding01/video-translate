import json
import re
import unittest
from pathlib import Path

from xvt.events import (
    EVENT_PROTOCOL_VERSION,
    EventType,
    create_event,
    error_event_fields,
    job_event_fields,
)

_SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "xvt"
# 匹配 create_event(...) 第一个参数为裸字符串字面量（含 f-string）的调用，
# 允许跨行、前置注释。命中即说明该调用未引用 EventType 枚举成员。
_BARE_STRING_CALL = re.compile(r'create_event\(\s*(?:#.*\n\s*)?f?["\']([^"\']*)', re.M)


class EventProtocolTest(unittest.TestCase):
    def test_event_serialization_includes_protocol_version(self) -> None:
        event = create_event(EventType.STAGE_STARTED, stage="inspect")
        payload = json.loads(event.to_json())

        self.assertEqual(payload["v"], 1)
        self.assertEqual(EVENT_PROTOCOL_VERSION, 1)
        self.assertEqual(payload["type"], "stage_started")
        self.assertEqual(payload["data"], {"stage": "inspect"})

    def test_create_event_accepts_enum(self) -> None:
        self.assertEqual(create_event(EventType.CLI_COMPLETED).type, "cli_completed")

    def test_job_event_fields_always_contains_both_keys(self) -> None:
        self.assertEqual(
            job_event_fields(None, None),
            {"job_id": None, "job_file": None},
        )
        self.assertEqual(
            job_event_fields("abc", Path("/tmp/job.json")),
            {"job_id": "abc", "job_file": "/tmp/job.json"},
        )

    def test_error_event_fields_shape(self) -> None:
        self.assertEqual(
            error_event_fields("interrupted", "interrupted by user"),
            {"error": {"category": "interrupted", "message": "interrupted by user"}},
        )

    def test_no_bare_string_create_event_calls_in_production_modules(self) -> None:
        """词汇表同步测试：生产端不得再有 ``create_event("...")``/``create_event(f"...")`` 裸字符串
        调用。

        P3c 完成生产端全量迁移后，全部调用都必须引用 ``EventType`` 枚举成员。本测试取代
        P3a/P3b 阶段「字面量必须能被 EventType 解析」的覆盖率测试：迁移完成后裸字符串
        调用应为零个，直接断言不存在比断言可解析更强，能防止未来再次引入裸字符串。
        """
        offending: list[str] = []
        for source in sorted(_SRC_DIR.glob("*.py")):
            if source.name == "events.py":
                continue
            text = source.read_text()
            for match in _BARE_STRING_CALL.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offending.append(f"{source.name}:{line} -> {match.group(1)!r}")

        self.assertEqual(
            offending,
            [],
            f"发现裸字符串 create_event 调用，请改用 EventType 枚举成员: {offending}",
        )


if __name__ == "__main__":
    unittest.main()
