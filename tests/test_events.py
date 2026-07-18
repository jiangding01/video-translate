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
# 匹配 create_event(...) 的第一个字符串字面量参数（允许跨行、前置注释）。
_LITERAL_TYPE = re.compile(r'create_event\(\s*(?:#.*\n\s*)?["\']([^"\']+)["\']', re.M)


class EventProtocolTest(unittest.TestCase):
    def test_event_serialization_includes_protocol_version(self) -> None:
        event = create_event(EventType.STAGE_STARTED, stage="inspect")
        payload = json.loads(event.to_json())

        self.assertEqual(payload["v"], 1)
        self.assertEqual(EVENT_PROTOCOL_VERSION, 1)
        self.assertEqual(payload["type"], "stage_started")
        self.assertEqual(payload["data"], {"stage": "inspect"})

    def test_create_event_accepts_enum_and_str(self) -> None:
        self.assertEqual(create_event(EventType.CLI_COMPLETED).type, "cli_completed")
        self.assertEqual(create_event("cli_completed").type, "cli_completed")

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

    def test_every_literal_event_type_is_covered_by_enum(self) -> None:
        """扫描全部生产端 create_event 字面量，确保每个类型都能被 EventType 解析。

        本测试在 P3b/P3c 生产端迁移期间保证不会遗漏新增的事件类型。
        """
        literals: set[str] = set()
        for source in sorted(_SRC_DIR.glob("*.py")):
            if source.name == "events.py":
                continue
            for match in _LITERAL_TYPE.finditer(source.read_text()):
                literals.add(match.group(1))

        self.assertTrue(literals, "未扫描到任何 create_event 字面量事件类型")
        for value in sorted(literals):
            # 值不在枚举中时 EventType(value) 抛 ValueError，测试失败并指出缺失类型。
            self.assertEqual(EventType(value).value, value)


if __name__ == "__main__":
    unittest.main()
