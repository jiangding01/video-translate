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
    redact_text,
    redact_url,
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
                available_platform_subtitles=["en"],
                report_path="/tmp/source-selection.json",
            ),
            EventType.TRANSLATION_REVIEW_INPUT_READY: create_event(
                EventType.TRANSLATION_REVIEW_INPUT_READY,
                **job_event_fields("job-1", "/tmp/job.json"),
                path="/tmp/translation-review.json",
                scope="translation",
                item_count=2,
            ),
            EventType.TRANSLATION_REVIEW_REVISED: create_event(
                EventType.TRANSLATION_REVIEW_REVISED,
                **job_event_fields("job-1", "/tmp/job.json"),
                scope="translation",
                item_count=2,
                checksum="abc123",
            ),
            EventType.SOURCE_REVIEW_INPUT_READY: create_event(
                EventType.SOURCE_REVIEW_INPUT_READY,
                **job_event_fields("job-1", "/tmp/job.json"),
                path="/tmp/source-review.json",
                scope="source",
                item_count=2,
            ),
            EventType.SOURCE_REVIEW_REVISED: create_event(
                EventType.SOURCE_REVIEW_REVISED,
                **job_event_fields("job-1", "/tmp/job.json"),
                scope="source",
                item_count=2,
                checksum="def456",
                qa_status="warning",
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
            EventType.DOWNLOAD_SPACE_INSUFFICIENT: create_event(
                EventType.DOWNLOAD_SPACE_INSUFFICIENT,
                required_bytes=4 * 2**30,
                available_bytes=512 * 2**20,
                format_id="137+140",
                part_count=2,
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

    def test_signed_url_query_keys_are_redacted_across_providers(self) -> None:
        """F8：签名参数名单从「精确匹配」扩成「精确键 + 子串/分段规则」。

        旧名单只收了 AWS 三件套，GCS v4 的 `X-Goog-Signature` / `X-Goog-Credential`
        与阿里云 `auth_key` 原样落进事件与 job.json。
        """
        sensitive_keys = (
            "X-Goog-Signature",
            "X-Goog-Credential",
            "x-goog-signature",
            "auth_key",
            "X-Amz-Signature",
            "X-Amz-Credential",
            "X-Amz-Security-Token",
            "q-signature",
            "Signature",
            "sig",
            "key",
            "access_token",
            "refresh_token",
            "id_token",
            "csrf_token",
            "api_key",
            "apikey",
            "OSSAccessKeyId",
            "SecretAccessKey",
            "x-secret",
            "pwd",
            "password",
            "auth",
            "Authorization",
        )
        for query_key in sensitive_keys:
            with self.subTest(key=query_key):
                url = f"https://cdn.example.com/v.mp4?{query_key}=LEAKVALUE&itag=22"
                redacted = redact_url(url)

                self.assertNotIn("LEAKVALUE", redacted)
                self.assertIn("itag=22", redacted)
                self.assertIn("redacted", redacted)
                # 事件边界与自由文本边界必须共用同一判定。
                self.assertNotIn("LEAKVALUE", redact_text(f"download failed: {url}"))
                self.assertNotIn(
                    "LEAKVALUE",
                    create_event(EventType.COMMAND_FAILED, url=url).to_json(),
                )

    def test_benign_query_keys_are_not_redacted(self) -> None:
        """子串规则的误伤边界：`key` / `sig` / `auth` 只在分段整词相等时命中。"""
        benign_keys = (
            "monkey",
            "keyword",
            "keywords",
            "author",
            "authors",
            "design",
            "designation",
            "signal",
            "assignment",
            "id",
            "itag",
            "page",
            "lang",
            "expires",
            "range",
            "format",
            "q",
            "t",
        )
        for query_key in benign_keys:
            with self.subTest(key=query_key):
                url = f"https://cdn.example.com/v.mp4?{query_key}=plainvalue"
                redacted = redact_url(url)

                self.assertIn("plainvalue", redacted, f"{query_key} 被误伤脱敏")
                self.assertNotIn("redacted", redacted)

    def test_redact_text_is_idempotent(self) -> None:
        """`set_stage` 在边界脱敏后，事件分支会对同一段文本再脱敏一次。"""
        samples = (
            "https://user:pw@host/v1?X-Goog-Signature=abc&token=t",
            "Cookie: SID=xyz; HSID=abc",
            "Authorization: Bearer sk-live-123",
            "proxy socks5://user:pw@127.0.0.1:1080 failed",
            "ffmpeg error at https://cdn.example.com/x.mp4?sig=deadbeef&Expires=1",
            "plain text with no secrets",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                once = redact_text(sample)
                self.assertEqual(once, redact_text(once))

    def test_non_http_userinfo_with_at_in_password_is_fully_redacted(self) -> None:
        """非 http scheme 的 userinfo 必须吃到 authority 里的**最后**一个 `@`。

        旧 `_USERINFO_PATTERN` 的字符类排除 `@`，只吃到第一个 `@`，密码里带 `@` 的
        代理 URL 会漏出后半段（`socks5://user:p@ss@host` → `socks5://<redacted>@ss@host`）。
        http/https 走 `redact_url` 的 `netloc.rsplit('@', 1)`（最后一个 `@`），因此一直正确；
        两条分支必须给出同口径结果，且不能靠 scheme 白名单（下一个 scheme 又会漏）。
        """
        # name: (原文, 期望脱敏结果, 必须消失的密码片段)。期望值写全串而不是只查
        # 子串：host / port / 尾随诊断文本必须原样保留，脱敏不等于抹掉排障信息。
        cases = {
            "socks5": (
                "socks5://user:p@ss@proxy.example.com:1080 failed",
                "socks5://<redacted>@proxy.example.com:1080 failed",
                ("p@ss", "ss@proxy"),
            ),
            "socks5h_multi_at": (
                "socks5h://user:a@b@c@proxy:1080",
                "socks5h://<redacted>@proxy:1080",
                ("a@b", "b@c", "c@proxy"),
            ),
            "socks4": (
                "socks4://user:pw@rd@127.0.0.1:1080",
                "socks4://<redacted>@127.0.0.1:1080",
                ("pw@rd", "rd@127"),
            ),
            "ftp": (
                "ftp://anon:se@cret@files.example.com/x",
                "ftp://<redacted>@files.example.com/x",
                ("se@cret", "cret@files"),
            ),
            "unknown_scheme": (
                "weird+proto-1.0://u:a@b@host",
                "weird+proto-1.0://<redacted>@host",
                ("a@b", "b@host"),
            ),
            "no_username": (
                "socks5://:pa@ss@proxy.example.com",
                "socks5://<redacted>@proxy.example.com",
                ("pa@ss", "ss@proxy"),
            ),
            "token_only": (
                "socks5://tok@en@proxy.example.com",
                "socks5://<redacted>@proxy.example.com",
                ("tok@en", "en@proxy"),
            ),
            "http_keeps_working": (
                "http://user:p@ss@proxy.example.com:8080 failed",
                "http://<redacted>@proxy.example.com:8080 failed",
                ("p@ss", "ss@proxy"),
            ),
            "https_keeps_working": (
                "https://user:p@ss@proxy.example.com:8443/v1",
                "https://<redacted>@proxy.example.com:8443/v1",
                ("p@ss", "ss@proxy"),
            ),
        }
        for name, (sample, expected, leaks) in cases.items():
            with self.subTest(case=name):
                redacted = redact_text(sample)

                self.assertEqual(expected, redacted)
                for leak in leaks:
                    self.assertNotIn(leak, redacted, f"{name} 漏出 {leak!r}: {redacted!r}")
                # 事件边界与自由文本边界共用同一判定。
                self.assertNotIn(
                    leaks[0],
                    create_event(
                        EventType.COMMAND_FAILED,
                        **error_event_fields("dependency_or_process", sample),
                    ).to_json(),
                )
                # 幂等：`set_stage` 脱敏后事件分支会再脱敏一次，两条出口必须一致。
                self.assertEqual(redacted, redact_text(redacted))

    def test_userinfo_redaction_does_not_touch_plain_at_signs(self) -> None:
        """过度匹配边界：没有 `scheme://` 的 `@`，以及 authority 之外的 `@`，都不得被改写。

        `@` 在日志里太常见（邮箱、ssh 串、装饰器、Python 仓库定位符），userinfo 规则
        放宽到「吃到最后一个 `@`」之后必须由 authority 终止符（`/` `?` `#` 空白）兜住。
        """
        untouched = (
            "contact foo@bar.com for access",
            "git@github.com:org/repo.git",
            "scp user@host:/tmp/a.srt .",
            "@dataclass(frozen=True)",
            "pip install xvt@git+https://example.com/x.git",
            "see https://example.com/docs and mail foo@bar.com",
            # authority 终止符：`?` 与 `#` 之后的 `@` 已不属于 userinfo。
            "socks5://proxy.example.com:1080?next=foo@bar.com",
            "socks5://proxy.example.com:1080#note@2",
            "https://cdn.example.com/v.mp4#anchor@2",
            "ffmpeg: Invalid data found at 0x40@frame 12",
            "https://cdn.example.com/paths/a@2x.png",
            "plain text with no secrets",
        )
        for sample in untouched:
            with self.subTest(sample=sample):
                self.assertEqual(sample, redact_text(sample))

    def test_schemeless_signed_query_values_are_redacted(self) -> None:
        """不带 scheme 的 `路径?query` 里的签名参数必须脱敏。

        `_URL_PATTERN` 要求 `https?://`、`_USERINFO_PATTERN` 要求 `scheme://`，
        yt-dlp / urllib3 的失败消息里那种「只有路径 + query」的签名 URL 两条都够不着
        （2026-10-18 实测原样返回，`SECRETSIG123` 明文落进事件与 `job.json`）。

        口径刻意收窄到「键名被 `_is_sensitive_query_key` 判为敏感」的那些
        `key=value` 的**值**——不是对任意 `?k=v` 形状整体脱敏。期望值写全串而不是
        只查子串：路径、非敏感参数与尾随诊断文本必须原样保留。
        """
        # name: (原文, 期望脱敏结果, 必须消失的凭据片段)
        cases = {
            "bare_path_query": (
                "unable to download video data: /videoplayback?sig=SECRETSIG123&id=abc",
                "unable to download video data: /videoplayback?sig=<redacted>&id=abc",
                "SECRETSIG123",
            ),
            "urllib3_connection_pool": (
                "HTTPSConnectionPool(host='r1.googlevideo.com', port=443): "
                "/videoplayback?sig=SECRET&key=yt6",
                "HTTPSConnectionPool(host='r1.googlevideo.com', port=443): "
                "/videoplayback?sig=<redacted>&key=<redacted>",
                "SECRET",
            ),
            "protocol_relative": (
                "//r1.googlevideo.com/videoplayback?sig=SECRETSIG123",
                "//r1.googlevideo.com/videoplayback?sig=<redacted>",
                "SECRETSIG123",
            ),
            "parenthesised_query": (
                "ffmpeg failed (input /videoplayback?signature=LEAKSIG&itag=22)",
                "ffmpeg failed (input /videoplayback?signature=<redacted>&itag=22)",
                "LEAKSIG",
            ),
            "quoted_query": (
                "yt-dlp: '/videoplayback?X-Goog-Signature=LEAKSIG&expire=1' returned 403",
                "yt-dlp: '/videoplayback?X-Goog-Signature=<redacted>&expire=1' returned 403",
                "LEAKSIG",
            ),
            "schemeless_authority_with_token": (
                "api.example.com/v1/chat?access_token=sk-LEAK123 returned 401",
                "api.example.com/v1/chat?access_token=<redacted> returned 401",
                "sk-LEAK123",
            ),
            # 顺带收口的既有遗漏：非 http scheme 的 query 此前完全不脱敏
            # （`redact_url` 只认 http/https，`_USERINFO_PATTERN` 只管 `@` 之前）。
            "non_http_scheme_query": (
                "proxy socks5://host/p?token=LEAKTOK failed",
                "proxy socks5://host/p?token=<redacted> failed",
                "LEAKTOK",
            ),
            "non_http_scheme_userinfo_and_query": (
                "socks5://u:pw@host/p?token=LEAKTOK",
                "socks5://<redacted>@host/p?token=<redacted>",
                "LEAKTOK",
            ),
            # 键名在判定前按 `parse_qsl` 的口径解码一次，否则 `%73ig` 绕过三层匹配。
            "percent_encoded_key": (
                "/v.mp4?%73ig=LEAKSIG&itag=22",
                "/v.mp4?%73ig=<redacted>&itag=22",
                "LEAKSIG",
            ),
            "non_sensitive_keys_survive": (
                "/videoplayback?itag=22&expire=1764000000&lang=en",
                "/videoplayback?itag=22&expire=1764000000&lang=en",
                None,
            ),
        }
        for name, (sample, expected, leak) in cases.items():
            with self.subTest(case=name):
                redacted = redact_text(sample)

                self.assertEqual(expected, redacted)
                if leak is not None:
                    self.assertNotIn(leak, redacted, f"{name} 漏出 {leak!r}: {redacted!r}")
                    # 事件边界必须与自由文本边界共用同一判定。
                    self.assertNotIn(
                        leak,
                        create_event(
                            EventType.COMMAND_FAILED,
                            **error_event_fields("dependency_or_process", sample),
                        ).to_json(),
                    )
                # 幂等：`set_stage` 脱敏后事件分支会再脱敏一次，两条出口必须一致。
                self.assertEqual(redacted, redact_text(redacted))

    def test_schemeless_query_redaction_does_not_touch_benign_text(self) -> None:
        """窄口径的唯一风险是误伤，因此反向守卫用真实形状逐字钉住。

        `?k=v` / `k=v` 这个形状在 ffmpeg 滤镜串、yt-dlp 格式选择器、QA 消息与普通
        日志散文里到处都是。断言落在 `原文 == redact_text(原文)` 上（不是「不含某
        子串」这种弱断言），任何一条被改写都说明口径超出了「键名命中策展名单」。
        """
        untouched = (
            # ffmpeg 滤镜串（burn.py / render.py / doctor.py 的真实取值）
            "-vf ass=filename=/path/x.ass",
            "ass=filename=measure.ass:alpha=1,extractplanes=a,bbox=min_val=0",
            "scale=640:360",
            "subtitles=f.srt:force_style='Fontsize=24'",
            "[0:v]scale=640:360[v];[v]ass=filename=f.ass[out]",
            "-map 0:v:0 -map 0:a? -c:v h264_videotoolbox -b:v 5M",
            "drawtext=text='a=b':fontcolor=white:x=(w-text_w)/2",
            # yt-dlp 格式选择器与参数（download.py 的真实取值）
            "bv*+ba/b",
            "-f 'bestvideo[height<=1080]+bestaudio'",
            "--convert-subs srt",
            "-f bestaudio/best --format-sort-force -S lang,res",
            "--cookies-from-browser chrome",
            # 普通散文与日志
            "cue_count=52",
            "status=failed",
            "exit=2",
            "width=161 limit=492",
            "bilingual_overflow_risk: cue 3 width=161 limit=492",
            "really? x=1",
            "retry 1/3 && continue",
            "ffmpeg exited with code 1 (signal=none, timeout=False)",
            "frame= 12 fps=25 q=28.0 size=128kB time=00:00:00.48 bitrate=2185.5kbits/s",
            # 键名只是**看起来**像凭据：子串/分段规则本就不该命中
            "/v.mp4?monkey=plain&keyword=plain&author=plain&design=plain&signal=plain",
            "/v.mp4?assignment=plain&designation=plain&keywords=plain",
            # 已脱敏文本不得被二次改写（幂等的另一半）
            "/videoplayback?sig=<redacted>&id=abc",
            "download failed: https://cdn.example.com/v.mp4?sig=%3Credacted%3E&itag=22",
        )
        for sample in untouched:
            with self.subTest(sample=sample):
                self.assertEqual(sample, redact_text(sample))

    def test_schemeless_and_http_query_redaction_agree_on_the_same_key(self) -> None:
        """两条路径（`redact_url` 规范化 vs 自由文本规则）判定必须一致。

        同一个键在带 scheme 与不带 scheme 两种形态下都必须被脱敏（或都不脱敏），
        否则「有没有 scheme」就成了凭据能否泄漏的开关。标记串的拼写两条不同
        （`%3Credacted%3E` vs `<redacted>`）是刻意的：带 scheme 那条经
        `urlencode` 规范化，本次修复不动它。
        """
        sensitive = ("sig", "signature", "X-Goog-Signature", "access_token", "auth_key", "key")
        benign = ("itag", "monkey", "author", "design", "signal", "expires")
        for query_key in sensitive:
            with self.subTest(key=query_key, verdict="sensitive"):
                tail = f"/v.mp4?{query_key}=LEAKVALUE&itag=22"
                with_scheme = redact_text(f"failed: https://cdn.example.com{tail}")
                without_scheme = redact_text(f"failed: cdn.example.com{tail}")

                self.assertNotIn("LEAKVALUE", with_scheme)
                self.assertNotIn("LEAKVALUE", without_scheme)
                self.assertIn("itag=22", with_scheme)
                self.assertIn("itag=22", without_scheme)
        for query_key in benign:
            with self.subTest(key=query_key, verdict="benign"):
                tail = f"/v.mp4?{query_key}=plainvalue&itag=22"
                with_scheme = redact_text(f"failed: https://cdn.example.com{tail}")
                without_scheme = redact_text(f"failed: cdn.example.com{tail}")

                self.assertIn("plainvalue", with_scheme, f"{query_key} 带 scheme 时被误伤")
                self.assertIn("plainvalue", without_scheme, f"{query_key} 不带 scheme 时被误伤")

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
