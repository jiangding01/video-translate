import ast
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
_EVENTS_MD = Path(__file__).resolve().parent.parent / "docs" / "EVENTS.md"
# 匹配 create_event(...) 第一个参数为裸字符串字面量（含 f-string）的调用，
# 允许跨行、前置注释。命中即说明该调用未引用 EventType 枚举成员。
_BARE_STRING_CALL = re.compile(r'create_event\(\s*(?:#.*\n\s*)?f?["\']([^"\']*)', re.M)

_MD_HEADING = re.compile(r"^### (.+)$", re.M)
_MD_TABLE_ROW = re.compile(r"^\|\s*`([a-zA-Z0-9_.]+)`\s*\|\s*([^|]+?)\s*\|", re.M)
_MD_TYPE_IN_HEADING = re.compile(r"`([a-z_]+)`")


def _parse_events_md_field_tables(text: str) -> dict[str, list[tuple[str, str]]]:
    """把 docs/EVENTS.md 中每个 `### \\`event_type\\`` 小节下的字段表解析为
    ``{event_type: [(field_name, field_type), ...]}``。

    一个标题可能同时列出多个共享同一张表的事件类型（如
    ``### \\`stage_started\\` / \\`stage_waiting\\` / ...``），此时同一份字段列表
    会分别注册到标题里出现的每个类型名下。
    """
    headings = list(_MD_HEADING.finditer(text))
    events: dict[str, list[tuple[str, str]]] = {}
    for index, heading in enumerate(headings):
        type_names = _MD_TYPE_IN_HEADING.findall(heading.group(1))
        if not type_names:
            continue
        block_start = heading.end()
        block_end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[block_start:block_end]
        fields = [
            (name, field_type)
            for name, field_type in _MD_TABLE_ROW.findall(block)
            if name not in {"字段", "---"}
        ]
        for type_name in type_names:
            events.setdefault(type_name, fields)
    return events


def _required_field_names(fields: list[tuple[str, str]]) -> set[str]:
    """字段类型不以 ``?`` 结尾即视为「键必须存在」（值是否可为 null 由类型自身表达，
    如 ``job_id`` 记为 ``string?`` 但键始终存在，属于信封约定，不在此函数覆盖范围）。
    """
    return {name for name, field_type in fields if not field_type.rstrip().endswith("?")}


# batch_message 是内部辅助事件，其 docs/EVENTS.md 契约就是裸 ``message`` 字段
# （用于复用 create_event 的脱敏逻辑），不属于失败类事件的 error 信封，豁免扫描。
_BARE_MESSAGE_EXEMPT_EVENT_TYPES = {"BATCH_MESSAGE"}


def _find_bare_error_kwargs(path: Path) -> list[str]:
    """用 AST 扫描 ``create_event(...)`` 调用，找出直接传入裸 ``category=``/``message=``
    关键字参数（而非通过 ``**error_event_fields(...)`` 展开）的调用点。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offending: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "create_event":
            continue
        first_arg = node.args[0] if node.args else None
        event_type_name = getattr(first_arg, "attr", None)
        if event_type_name in _BARE_MESSAGE_EXEMPT_EVENT_TYPES:
            continue
        for kw in node.keywords:
            if kw.arg in {"category", "message"}:
                offending.append(f"{path.name}:{node.lineno} -> {kw.arg}")
    return offending


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

    def test_source_selected_event_type_is_registered(self) -> None:
        self.assertEqual(EventType.SOURCE_SELECTED, "source_selected")

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
        for source in sorted(_SRC_DIR.rglob("*.py")):
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


class EventPayloadContractTest(unittest.TestCase):
    """载荷结构契约测试（P7c）。

    不满足于「事件名存在」，而是把 docs/EVENTS.md 的字段表解析为
    ``{type: 必填字段集}``，再用贴近生产端真实调用路径（``job_event_fields`` /
    ``error_event_fields`` 与实际调用时相同的关键字顺序、字面量）构造的
    「golden 事件样例」互相校验：
    - golden 事件必须携带文档要求的全部必填字段；
    - golden 事件不应携带文档字段表之外的未知字段；
    - 失败类事件必须是嵌套 ``error: {category, message}``，不得有顶层裸
      ``category``/``message``。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.fields_by_type = _parse_events_md_field_tables(_EVENTS_MD.read_text(encoding="utf-8"))

    def _golden_events(self):
        return {
            EventType.WORKFLOW_STARTED: create_event(
                EventType.WORKFLOW_STARTED,
                **job_event_fields(None, None),
                continuation=False,
            ),
            EventType.WORKFLOW_PLANNED: create_event(
                EventType.WORKFLOW_PLANNED,
                steps=["transcribe", "translate", "burn"],
            ),
            EventType.WORKFLOW_WAITING: create_event(
                EventType.WORKFLOW_WAITING,
                **job_event_fields("job-1", "/tmp/job.json"),
                reason="awaiting manual translation input",
                artifacts=["/tmp/translation-batch-1.json"],
            ),
            EventType.WORKFLOW_COMPLETED: create_event(
                EventType.WORKFLOW_COMPLETED,
                **job_event_fields("job-1", "/tmp/job.json"),
                artifacts=["/tmp/output.mp4"],
            ),
            EventType.WORKFLOW_FAILED: create_event(
                EventType.WORKFLOW_FAILED,
                **job_event_fields(None, None),
                **error_event_fields("workflow", "boom"),
            ),
            EventType.STAGE_STARTED: create_event(
                EventType.STAGE_STARTED,
                **job_event_fields("job-1", "/tmp/job.json"),
                stage="transcribe",
                status="running",
            ),
            EventType.STAGE_WAITING: create_event(
                EventType.STAGE_WAITING,
                **job_event_fields("job-1", "/tmp/job.json"),
                stage="translate",
                status="awaiting_input",
            ),
            EventType.STAGE_COMPLETED: create_event(
                EventType.STAGE_COMPLETED,
                **job_event_fields("job-1", "/tmp/job.json"),
                stage="translate",
                status="completed",
            ),
            EventType.STAGE_FAILED: create_event(
                EventType.STAGE_FAILED,
                **job_event_fields("job-1", "/tmp/job.json"),
                stage="translate",
                status="failed",
                **error_event_fields("workflow", "boom"),
            ),
            EventType.SOURCE_SELECTED: create_event(
                EventType.SOURCE_SELECTED,
                **job_event_fields("job-1", "/tmp/job.json"),
                strategy="auto",
                candidate_kind="manual",
                selected_kind="subtitles",
                reason="manual_subtitles_passed",
                report_path="/tmp/source-selection.json",
            ),
            EventType.TRANSLATION_BATCH_STARTED: create_event(
                EventType.TRANSLATION_BATCH_STARTED,
                **job_event_fields("job-1", "/tmp/job.json"),
                batch=1,
                batch_total=3,
            ),
            EventType.TRANSLATION_BATCH_RETRYING: create_event(
                EventType.TRANSLATION_BATCH_RETRYING,
                **job_event_fields("job-1", "/tmp/job.json"),
                batch=1,
                batch_total=3,
                attempt=2,
                delay_seconds=1.5,
                reason="provider temporarily unavailable",
            ),
            EventType.TRANSLATION_BATCH_COMPLETED: create_event(
                EventType.TRANSLATION_BATCH_COMPLETED,
                **job_event_fields("job-1", "/tmp/job.json"),
                batch=1,
                batch_total=3,
            ),
            EventType.CLI_FAILED: create_event(
                EventType.CLI_FAILED,
                command="run",
                **error_event_fields("invalid_input", "bad input"),
            ),
            EventType.BATCH_ITEM_FAILED: create_event(
                EventType.BATCH_ITEM_FAILED,
                batch_file="/tmp/batch.json",
                input="/tmp/input.mp4",
                job_file=None,
                index=1,
                total=1,
                **error_event_fields("workflow", "boom"),
                recovery_plan=None,
            ),
        }

    def test_golden_events_satisfy_documented_required_fields(self) -> None:
        for event_type, event in self._golden_events().items():
            fields = self.fields_by_type.get(str(event_type))
            self.assertIsNotNone(fields, f"{event_type} 在 EVENTS.md 中没有字段表")
            required = _required_field_names(fields)
            missing = required - event.data.keys()
            self.assertEqual(
                missing,
                set(),
                f"{event_type} 缺少 EVENTS.md 要求的必填字段: {missing}",
            )

    def test_golden_events_have_no_undocumented_fields(self) -> None:
        for event_type, event in self._golden_events().items():
            fields = self.fields_by_type.get(str(event_type))
            self.assertIsNotNone(fields, f"{event_type} 在 EVENTS.md 中没有字段表")
            documented = {name for name, _ in fields}
            extra = event.data.keys() - documented
            self.assertEqual(
                extra,
                set(),
                f"{event_type} 携带了 EVENTS.md 未记录的字段: {extra}",
            )

    def test_failure_class_golden_events_use_nested_error_only(self) -> None:
        failure_types = {
            EventType.WORKFLOW_FAILED,
            EventType.STAGE_FAILED,
            EventType.CLI_FAILED,
            EventType.BATCH_ITEM_FAILED,
        }
        golden = self._golden_events()
        for event_type in failure_types:
            event = golden[event_type]
            self.assertIn("error", event.data, f"{event_type} 缺少嵌套 error 字段")
            self.assertEqual(set(event.data["error"].keys()), {"category", "message"})
            self.assertNotIn("category", event.data, f"{event_type} 不应有顶层裸 category 字段")
            self.assertNotIn("message", event.data, f"{event_type} 不应有顶层裸 message 字段")

    def test_no_bare_category_or_message_kwargs_in_create_event_calls(self) -> None:
        """AST 扫描全部生产模块：create_event(...) 调用不得直接传入裸
        category=/message= 关键字参数（必须通过 error_event_fields(...) 展开），
        防止 P7c 修复的失配（cli.py 顶层 category/message）再次出现。
        """
        offending: list[str] = []
        for source in sorted(_SRC_DIR.rglob("*.py")):
            if source.name == "events.py":
                continue
            offending.extend(_find_bare_error_kwargs(source))

        self.assertEqual(
            offending,
            [],
            f"发现 create_event 调用直接传入裸 category=/message= 参数: {offending}",
        )


if __name__ == "__main__":
    unittest.main()
