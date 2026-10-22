import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from xvt.events import redact_text
from xvt.job_store import create_job, load_job, save_job
from xvt.models import Artifact, Cue, StageName, StageStatus, Transcript, Word
from xvt.pipeline import interrupt_running_stage
from xvt.qa import check_subtitles, display_width, display_width_limit
from xvt.recovery import file_checksum, valid_artifact_path
from xvt.segment import SegmentationConfig, segment_transcript


def _register_acknowledged_report(job, artifacts: Path, issues: list[dict]) -> dict:
    """登记一份 `needs_review` 源字幕 QA 报告，并返回绑定到它的确认记录字段。

    真实确认通道（`acknowledge_subtitle_review`）总是把 `report_kind` /
    `report_checksum` 一起落盘，手搓 fixture 必须照做，否则确认对任何报告都无效，
    测试会因为「记录格式不全」而不是它想验的原因变绿。
    """
    report_path = artifacts / "qa.json"
    report_path.write_text(
        json.dumps({"status": "needs_review", "cue_count": 1, "issues": issues}),
        encoding="utf-8",
    )
    checksum = file_checksum(report_path)
    job.artifacts.append(Artifact("subtitle_qa", str(report_path), checksum))
    return {"report_kind": "subtitle_qa", "report_checksum": checksum}


class PipelineTest(unittest.TestCase):
    def test_segmenter_respects_language_display_width(self) -> None:
        tokens = (
            "This workflow keeps every generated bilingual subtitle safely inside the video frame."
        ).split()
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=tuple(
                Word(("" if index == 0 else " ") + token, index * 300, index * 300 + 250, 0.9)
                for index, token in enumerate(tokens)
            ),
        )

        cues = segment_transcript(transcript)

        self.assertGreater(len(cues), 1)
        self.assertTrue(
            all(display_width(cue.source_text) <= display_width_limit("en") for cue in cues)
        )

        balanced = segment_transcript(
            Transcript(
                language="en",
                engine="mlx",
                words=tuple(
                    Word(
                        ("" if index == 0 else " ") + token,
                        index * 300,
                        index * 300 + 250,
                        0.9,
                    )
                    for index, token in enumerate(
                        "And now Codex handles this next one for me.".split()
                    )
                ),
            )
        )
        self.assertEqual(
            [cue.source_text for cue in balanced],
            ["And now Codex handles this next one for me."],
        )

        long_sentence = (
            "It remembers where our metadata lives, how the upload package is organized, "
            "and how we add captions, save, and verify each upload."
        )
        balanced_long = segment_transcript(
            Transcript(
                language="en",
                engine="mlx",
                words=tuple(
                    Word(
                        ("" if index == 0 else " ") + token,
                        index * 300,
                        index * 300 + 250,
                        0.9,
                    )
                    for index, token in enumerate(long_sentence.split())
                ),
            )
        )
        self.assertTrue(all(len(cue.source_text.split()) > 1 for cue in balanced_long))

    def test_segmenter_prefers_semantic_boundaries_when_splitting_by_width(self) -> None:
        text = (
            "We should preserve the meaning of each subtitle, but still keep every line readable."
        )
        transcript = Transcript(
            language="en",
            engine="mlx",
            words=tuple(
                Word(
                    ("" if index == 0 else " ") + token,
                    index * 300,
                    index * 300 + 250,
                    0.9,
                )
                for index, token in enumerate(text.split())
            ),
        )

        cues = segment_transcript(transcript, SegmentationConfig(max_characters=50))

        self.assertEqual(
            [cue.source_text for cue in cues],
            [
                "We should preserve the meaning of each subtitle,",
                "but still keep every line readable.",
            ],
        )
        self.assertEqual([(cue.start_ms, cue.end_ms) for cue in cues], [(0, 2400), (2400, 4300)])

    def test_segmenter_prefers_cjk_punctuation_when_splitting_by_width(self) -> None:
        text = "我们需要保留每个字幕的含义，但是也要保证每行都清晰。"
        transcript = Transcript(
            language="zh-CN",
            engine="mlx",
            words=tuple(
                Word(token, index * 300, index * 300 + 250, 0.9) for index, token in enumerate(text)
            ),
        )

        cues = segment_transcript(transcript, SegmentationConfig(max_characters=28))

        self.assertEqual(
            [cue.source_text for cue in cues],
            ["我们需要保留每个字幕的含义，", "但是也要保证每行都清晰。"],
        )
        self.assertEqual([(cue.start_ms, cue.end_ms) for cue in cues], [(0, 4200), (4200, 7900)])

    def test_bilingual_qa_requires_review_for_visual_overflow(self) -> None:
        cues = [
            Cue(
                1,
                0,
                5000,
                "Short source",
                target_text="这是一条明显超过单行显示宽度限制的中文字幕" * 2,
                source_language="en",
                target_language="zh-CN",
            )
        ]

        report = check_subtitles(cues, require_translation=True)

        self.assertEqual(report.status, "needs_review")
        self.assertIn("bilingual_overflow_risk", {issue.code for issue in report.issues})

    def _make_review_blocked_subtitle_workflow(self, root: Path) -> Path:
        """构造一个因源字幕折叠后超宽而被 needs_review 阻断的端到端字幕 Job。"""
        from xvt.workflow import WorkflowOptions, run_workflow

        source = root / "source.srt"
        wide_line = "word " * 16  # 折叠后约 80 显示单位，超过 72 触发 review
        source.write_text(
            f"1\n00:00:00,000 --> 00:00:20,000\n{wide_line.strip()}\n",
            encoding="utf-8",
        )
        translated = root / "translated.srt"
        translated.write_text(
            "1\n00:00:00,000 --> 00:00:20,000\n你好。\n",
            encoding="utf-8",
        )
        result = run_workflow(
            WorkflowOptions(
                input=str(source),
                output=root / "output",
                language="en",
                target_language="zh-CN",
                translation_subtitles=translated,
            )
        )
        self.assertEqual(result.status, "awaiting_input")
        job_file = next((root / "output" / "jobs").glob("*/job.json"))
        return job_file

    def test_continue_without_acknowledgment_stays_blocked(self) -> None:
        from xvt.workflow import continue_workflow

        with tempfile.TemporaryDirectory() as directory:
            job_file = self._make_review_blocked_subtitle_workflow(Path(directory))
            result = continue_workflow(job_file)

        self.assertEqual(result.status, "awaiting_input")

    def test_acknowledged_continue_completes_both_qa_gates(self) -> None:
        from xvt.pipeline import acknowledge_subtitle_review
        from xvt.workflow import continue_workflow

        events = []
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._make_review_blocked_subtitle_workflow(Path(directory))
            acknowledge_subtitle_review(job_file, event_sink=events.append)
            with patch("xvt.workflow.resume_remote_transcription") as remote:
                result = continue_workflow(job_file, event_sink=events.append)

            self.assertEqual(result.status, "completed")
            remote.assert_not_called()
            updated = load_job(job_file)
            kinds = {artifact.kind for artifact in updated.artifacts}
            self.assertIn("source_srt", kinds)
            self.assertIn("bilingual_srt", kinds)
            self.assertEqual(updated.stages[StageName.TRANSLATE].status, StageStatus.SUCCEEDED)

        acknowledged = [event for event in events if event.type == "subtitle_review_acknowledged"]
        # 一次显式确认 + 两道 QA 门（源字幕 QA 与译文 QA）各一次复用放行。
        self.assertEqual(acknowledged[0].data["source"], "user_flag")
        self.assertGreaterEqual(len(acknowledged), 3)
        self.assertTrue(
            all(event.data["source"] == "stored_acknowledgment" for event in acknowledged[1:])
        )

    def test_acknowledged_continue_finalizes_stale_translate_failure(self) -> None:
        """字幕 QA needs_review 确认后，continue 必须清掉 translate 上的过期阻断错误。"""
        from xvt.pipeline import acknowledge_subtitle_review
        from xvt.pipeline.common import SUBTITLE_REVIEW_REQUIRED_MESSAGE
        from xvt.workflow import WorkflowOptions, continue_workflow, run_workflow

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:20,000\nShort source\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                f"1\n00:00:00,000 --> 00:00:20,000\n{'超长字幕' * 20}\n",
                encoding="utf-8",
            )
            waiting = run_workflow(
                WorkflowOptions(
                    input=str(source),
                    output=root / "output",
                    language="en",
                    target_language="zh-CN",
                    translation_subtitles=translated,
                )
            )
            self.assertEqual(waiting.status, "awaiting_input")
            job_file = next((root / "output" / "jobs").glob("*/job.json"))
            acknowledge_subtitle_review(job_file)

            # 语义复核腿在译文 QA 阻断时会把 translate 记为 failed；确认后这条错误已过期。
            job = load_job(job_file)
            job.stages[StageName.TRANSLATE].status = StageStatus.FAILED
            job.stages[StageName.TRANSLATE].error = SUBTITLE_REVIEW_REQUIRED_MESSAGE
            job_file = save_job(job)

            result = continue_workflow(job_file)

            self.assertEqual(result.status, "completed")
            updated = load_job(job_file)
            self.assertEqual(updated.stages[StageName.TRANSLATE].status, StageStatus.SUCCEEDED)
            self.assertIsNone(updated.stages[StageName.TRANSLATE].error)
            self.assertIsNotNone(valid_artifact_path(updated, "bilingual_srt"))

    def test_acknowledge_rejects_error_level_reports(self) -> None:
        from xvt.errors import UserInputError
        from xvt.pipeline import acknowledge_subtitle_review

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.srt", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            qa_path = artifacts / "qa.json"
            qa_path.write_text(
                json.dumps(
                    {
                        "status": "needs_review",
                        "cue_count": 1,
                        "issues": [
                            {"code": "line_too_wide", "severity": "review", "cue_id": 1},
                            {"code": "cue_overlap", "severity": "error", "cue_id": 2},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            job.artifacts.append(Artifact("subtitle_qa", str(qa_path), file_checksum(qa_path)))
            job_file = save_job(job)

            with self.assertRaisesRegex(UserInputError, "cannot be acknowledged"):
                acknowledge_subtitle_review(job_file)

    def test_acknowledgment_invalidated_when_subtitles_change(self) -> None:
        from xvt.pipeline.review import acknowledged_review_issues

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.srt", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            report_fields = _register_acknowledged_report(
                job, artifacts, [{"code": "line_too_wide", "severity": "review", "cue_id": 1}]
            )
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text("[]", encoding="utf-8")
            job.artifacts.append(Artifact("source_cues", str(cues_path), file_checksum(cues_path)))
            job.config["subtitle_review_acknowledgment"] = {
                **report_fields,
                "source_cues_checksum": file_checksum(cues_path),
                "acknowledged_issues": [["line_too_wide", 1]],
            }
            self.assertEqual(acknowledged_review_issues(job), {("display_width", 1)})

            # 字幕内容变化（checksum 改变）后确认失效。
            cues_path.write_text("[{}]", encoding="utf-8")
            job.artifacts[-1] = Artifact("source_cues", str(cues_path), file_checksum(cues_path))
            self.assertIsNone(acknowledged_review_issues(job))

    def test_acknowledgment_does_not_sign_off_a_different_translation(self) -> None:
        """源字幕不变、问题码与 cue id 都相同，但译文换了一段：旧确认不得代签。

        修复前只比对 `source_cues_checksum`：用户为 v1 译文确认过「cue 1 行宽超限」
        之后，一条内容完全不同、恰好同样在 cue 1 超宽的 v2 译文会被旧确认自动放行，
        而用户从没看过它。
        """

        from xvt.pipeline import acknowledge_subtitle_review
        from xvt.pipeline.common import write_json
        from xvt.pipeline.review import acknowledged_review_issues, review_gate_satisfied

        def register_translated_qa(job, artifacts: Path, target_text: str) -> dict:
            """按生产写法登记一份当前译文的译文 QA 报告，返回报告内容。"""
            cue = Cue(
                1,
                0,
                5000,
                "Short source",
                target_text,
                source_language="en",
                target_language="zh-CN",
            )
            qa_path = artifacts / "qa-translated.json"
            write_json(qa_path, asdict(check_subtitles([cue], require_translation=True)))
            job.artifacts = [
                item for item in job.artifacts if item.kind != "translated_subtitle_qa"
            ]
            job.artifacts.append(
                Artifact("translated_subtitle_qa", str(qa_path), file_checksum(qa_path))
            )
            # 与生产消费方一致：门判定读的是落盘后再解析出来的报告（issues 为 list）。
            return json.loads(qa_path.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.srt", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            source_cue = Cue(1, 0, 5000, "Short source", source_language="en")
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text(
                json.dumps([asdict(source_cue)], ensure_ascii=False), encoding="utf-8"
            )
            job.artifacts.append(Artifact("source_cues", str(cues_path), file_checksum(cues_path)))

            first = register_translated_qa(job, artifacts, "超长字幕" * 20)
            self.assertEqual(first["status"], "needs_review")
            job_file = save_job(job)
            acknowledge_subtitle_review(job_file)

            job = load_job(job_file)
            self.assertEqual(acknowledged_review_issues(job), {("display_width", 1)})
            self.assertTrue(review_gate_satisfied(job, first))

            # v2 译文：内容完全不同，但同样在 cue 1 触发同一个 review 问题码。
            second = register_translated_qa(job, artifacts, "完全不同的译文内容" * 20)
            review_codes = [
                (issue["code"], issue["cue_id"])
                for issue in second["issues"]
                if issue["severity"] == "review"
            ]
            self.assertEqual(
                review_codes,
                [
                    (issue["code"], issue["cue_id"])
                    for issue in first["issues"]
                    if issue["severity"] == "review"
                ],
            )
            self.assertNotEqual(first, second)
            self.assertIsNone(acknowledged_review_issues(job))
            self.assertFalse(review_gate_satisfied(job, second))

    def test_acknowledgment_rejects_unregistered_report(self) -> None:
        """伪造记录写着合法 report_kind 但报告未登记，`report_checksum` 为 null。

        `artifact_checksum_for` 此时返回 None，不显式排除就会「两边都是 None」相等。
        """

        from xvt.pipeline.review import acknowledged_review_issues

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.srt", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text("[]", encoding="utf-8")
            job.artifacts.append(Artifact("source_cues", str(cues_path), file_checksum(cues_path)))
            job.config["subtitle_review_acknowledgment"] = {
                "report_kind": "translated_subtitle_qa",
                "report_checksum": None,
                "source_cues_checksum": file_checksum(cues_path),
                "acknowledged_issues": [["line_too_wide", 1]],
            }
            self.assertIsNone(acknowledged_review_issues(job))

    def test_acknowledgment_rejects_boolean_cue_ids(self) -> None:
        # bool 是 int 的子类：`true == 1`，放行会让伪造的确认记录匹配 cue 1 的真实
        # review 问题。与 review_gate_satisfied 的 bool 排除保持对称。
        from xvt.pipeline.review import acknowledged_review_issues

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.srt", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            report_fields = _register_acknowledged_report(
                job, artifacts, [{"code": "line_too_wide", "severity": "review", "cue_id": 1}]
            )
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text("[]", encoding="utf-8")
            job.artifacts.append(Artifact("source_cues", str(cues_path), file_checksum(cues_path)))
            job.config["subtitle_review_acknowledgment"] = {
                **report_fields,
                "source_cues_checksum": file_checksum(cues_path),
                "acknowledged_issues": [["line_too_wide", True]],
            }
            self.assertIsNone(acknowledged_review_issues(job))

    def test_new_review_issue_blocks_despite_acknowledgment(self) -> None:
        from xvt.pipeline.review import complete_qa_stage
        from xvt.qa import SubtitleIssue, SubtitleQaReport

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = create_job("/tmp/input.srt", root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            report_fields = _register_acknowledged_report(
                job, artifacts, [{"code": "line_too_wide", "severity": "review", "cue_id": 1}]
            )
            cues_path = artifacts / "source-cues.json"
            cues_path.write_text("[]", encoding="utf-8")
            job.artifacts.append(Artifact("source_cues", str(cues_path), file_checksum(cues_path)))
            job.config["subtitle_review_acknowledgment"] = {
                **report_fields,
                "source_cues_checksum": file_checksum(cues_path),
                "acknowledged_issues": [["line_too_wide", 1]],
            }
            job_file = save_job(job)
            report = SubtitleQaReport(
                status="needs_review",
                cue_count=2,
                issues=(
                    SubtitleIssue("line_too_wide", "review", 1, "acknowledged"),
                    SubtitleIssue("line_too_wide", "review", 2, "new problem"),
                ),
            )
            self.assertFalse(complete_qa_stage(job, report, None))
            self.assertEqual(
                load_job(job_file).stages[StageName.SUBTITLE_QA].status,
                StageStatus.AWAITING_INPUT,
            )


class StageErrorRedactionTest(unittest.TestCase):
    """AGENTS 不可破坏约束 5：任何会持久化的错误文本都必须先脱敏。

    `set_stage` 把失败原因写进 `job.json`，`status --json` 再原样输出。这条链路
    不经过 `create_event`，所以必须在 `set_stage` 边界集中脱敏，而不是指望
    18 个调用点各自自觉调用 `redact_text`。
    """

    # key 是场景名，value 是 (错误文本, 必须消失的明文片段)。
    # 各条分别对应 process.py 的原始 stderr、download.py 的 403 detail、
    # translation_provider.py 的 4xx 消息这三类真实来源。
    _CASES = {
        "signed_url": (
            "yt-dlp failed: https://cdn.example.com/v.mp4?itag=22&sig=SIGLEAKVALUE",
            "SIGLEAKVALUE",
        ),
        "cookie_header": (
            "request headers: Cookie: SID=COOKIELEAKVALUE; theme=dark",
            "COOKIELEAKVALUE",
        ),
        "proxy_userinfo": (
            "proxy socks5://operator:PROXYLEAKVALUE@127.0.0.1:1080 refused the connection",
            "PROXYLEAKVALUE",
        ),
        "bearer_token": (
            "upstream echoed Authorization: Bearer sk-BEARERLEAKVALUE",
            "BEARERLEAKVALUE",
        ),
        "provider_base_url": (
            "translation provider returned HTTP 401 "
            "(model=m, base_url=https://operator:URLLEAKVALUE@api.example.com/v1): denied",
            "URLLEAKVALUE",
        ),
    }

    def test_stage_error_is_redacted_before_it_reaches_job_json(self) -> None:
        from xvt.pipeline.common import set_stage

        for name, (message, secret) in self._CASES.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                job = create_job("/tmp/input.mp4", Path(directory))
                job_file = save_job(job)
                events: list[object] = []

                set_stage(
                    job,
                    StageName.TRANSLATE,
                    StageStatus.FAILED,
                    events.append,
                    error=message,
                    error_category="dependency_or_process",
                )

                raw = Path(job_file).read_text(encoding="utf-8")
                self.assertNotIn(secret, raw, f"{name}: job.json 落盘后仍含明文凭据")
                self.assertIn("redacted", raw, f"{name}: job.json 没有脱敏标记")

                persisted = load_job(job_file).stages[StageName.TRANSLATE].error
                self.assertIsNotNone(persisted)
                self.assertNotIn(secret, persisted or "")
                # 事件与 job.json 必须给出同一份文本：事件分支会对已脱敏文本再脱敏，
                # 两者不一致说明 redact_text 不幂等。
                self.assertEqual(events[-1].data["error"]["message"], persisted)  # type: ignore[attr-defined]

    def test_messages_compared_by_value_survive_redaction_unchanged(self) -> None:
        """`set_stage` 在边界脱敏后，按**文本相等**识别 stage.error 的地方才仍然成立。

        `pipeline/review.py` 的 `record.error != SUBTITLE_REVIEW_REQUIRED_MESSAGE`
        就是这样一处：确认后收口过期阻断错误全靠这一句。脱敏若改写了这条常量，
        收口会静默失效而没有任何测试直接指向脱敏。往这类常量里加 URL 或
        `key=` 形态的片段之前，先让这条测试红。
        """
        from xvt.pipeline.common import SUBTITLE_REVIEW_REQUIRED_MESSAGE

        self.assertEqual(
            redact_text(SUBTITLE_REVIEW_REQUIRED_MESSAGE),
            SUBTITLE_REVIEW_REQUIRED_MESSAGE,
        )

    def test_stage_error_cleared_on_success_stays_none(self) -> None:
        from xvt.pipeline.common import set_stage

        with tempfile.TemporaryDirectory() as directory:
            job = create_job("/tmp/input.mp4", Path(directory))
            job_file = save_job(job)

            set_stage(job, StageName.TRANSLATE, StageStatus.FAILED, None, error="boom")
            set_stage(job, StageName.TRANSLATE, StageStatus.SUCCEEDED, None)

            self.assertIsNone(load_job(job_file).stages[StageName.TRANSLATE].error)


class InterruptRunningStageTest(unittest.TestCase):
    """Ctrl-C 收尾的第一条语句：任何一种读不出 Job 的方式都只该安静地返回 False。"""

    def test_missing_job_file_does_not_escape_the_interrupt_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "gone" / "job.json"

            # load_job 对缺失路径抛 UserInputError（XvtError 子类）。它逃逸出去会顶掉
            # 整个中断收尾：不发 cli_failed、不打恢复引导，退出码从 130/143 变成 1。
            self.assertFalse(interrupt_running_stage(missing, "translate"))

    def test_unreadable_job_file_also_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / "job.json"
            broken.write_text("not json", encoding="utf-8")

            self.assertFalse(interrupt_running_stage(broken, "translate"))

    def test_unknown_stage_name_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = save_job(create_job("/tmp/input.mp4", root))

            self.assertFalse(interrupt_running_stage(job_file, "not-a-stage"))


if __name__ == "__main__":
    unittest.main()
