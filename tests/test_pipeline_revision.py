import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from xvt.cli import main
from xvt.errors import UserInputError
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job
from xvt.models import Artifact, Cue, StageName, StageStatus, Word
from xvt.pipeline import (
    acknowledge_subtitle_review,
    apply_source_review_fixes,
    preflight_source_review_fixes,
    prepare_source_review,
    run_subtitle_transcription,
)
from xvt.pipeline.revision import source_review_stem
from xvt.recovery import file_checksum, valid_artifact_path
from xvt.workflow import continue_workflow, source_selection_matches
from xvt.workflow_config import WorkflowConfig, set_workflow_config


class SourceReviewPipelineTest(unittest.TestCase):
    def _job(self, root: Path) -> Path:
        source = root / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nGPT's 5.6 soul.\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\nKeep this line.\n",
            encoding="utf-8",
        )
        job = run_subtitle_transcription(source, root, language="en")
        job.config["source_strategy"] = "auto"
        source_path = valid_artifact_path(job, "source_cues")
        assert source_path is not None
        cues = json.loads(source_path.read_text(encoding="utf-8"))
        cues[0]["words"] = [
            asdict(Word("GPT's", 0, 400, 0.8)),
            asdict(Word("5.6", 400, 700, 0.7)),
        ]
        cues[0]["text_confidence"] = 0.8
        cues[0]["timing_confidence"] = 0.7
        cues[0]["provenance"] = "asr"
        source_path.write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
        for artifact in job.artifacts:
            if artifact.kind == "source_cues":
                artifact.checksum = file_checksum(source_path)
        selection = get_job_directory(job) / "artifacts" / "source-selection.json"
        selection.write_text(
            json.dumps(
                {
                    "strategy": "auto",
                    "candidate_kind": "none",
                    "selected_kind": "asr",
                    "reason": "test",
                    "source_cues_checksum": file_checksum(source_path),
                }
            ),
            encoding="utf-8",
        )
        job.artifacts = [item for item in job.artifacts if item.kind != "source_selection"]
        job.artifacts.append(Artifact("source_selection", str(selection), file_checksum(selection)))
        set_workflow_config(
            job,
            WorkflowConfig(
                target_language="zh-CN",
                translation_quality="polished",
                provider="agent",
            ),
        )
        translated = get_job_directory(job) / "artifacts" / "translated-cues.json"
        translated.write_text(
            json.dumps(
                [
                    asdict(
                        replace(
                            Cue(**{**item, "words": [Word(**word) for word in item["words"]]}),
                            target_text="旧译文",
                            target_language="zh-CN",
                        )
                    )
                    for item in cues
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        job.artifacts.append(
            Artifact("translated_cues", str(translated), file_checksum(translated))
        )
        translation_revision = (
            get_job_directory(job) / "artifacts" / "translation-review-revision.json"
        )
        translation_revision.write_text("{}", encoding="utf-8")
        job.artifacts.append(
            Artifact(
                "translation_review_revision",
                str(translation_revision),
                file_checksum(translation_revision),
            )
        )
        job.config["translation_review_acknowledgment"] = {"stale": True}
        job.config["subtitle_review_acknowledgment"] = {"stale": True}
        save_job(job)
        return get_job_file(job)

    def _response(self, job_file: Path, cue_ids: list[int]) -> Path:
        template = prepare_source_review(job_file, cue_ids)
        return template

    def test_prepare_and_apply_preserve_timeline_and_invalidate_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            before_job = load_job(job_file)
            before_source = valid_artifact_path(before_job, "source_cues")
            assert before_source is not None
            before_cues = json.loads(before_source.read_text(encoding="utf-8"))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "GPT-5.6 does in these categories."
            response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            paths = apply_source_review_fixes(job_file, response)

            job = load_job(job_file)
            source_path = valid_artifact_path(job, "source_cues")
            assert source_path is not None
            after_cues = json.loads(source_path.read_text(encoding="utf-8"))
            self.assertEqual(after_cues[0]["source_text"], "GPT-5.6 does in these categories.")
            for field in (
                "id",
                "start_ms",
                "end_ms",
                "words",
                "source_language",
                "target_language",
                "text_confidence",
                "timing_confidence",
                "provenance",
                "warnings",
            ):
                self.assertEqual(after_cues[0][field], before_cues[0][field])
            self.assertEqual(after_cues[1], before_cues[1])
            kinds = {artifact.kind for artifact in job.artifacts}
            self.assertIn("source_review_revision", kinds)
            self.assertIn("source_srt", kinds)
            self.assertIn("subtitle_qa", kinds)
            self.assertNotIn("translated_cues", kinds)
            self.assertNotIn("translation_review_revision", kinds)
            self.assertNotIn("translation_review_acknowledgment", job.config)
            self.assertNotIn("subtitle_review_acknowledgment", job.config)
            selection_path = valid_artifact_path(job, "source_selection")
            assert selection_path is not None
            selection = json.loads(selection_path.read_text(encoding="utf-8"))
            self.assertEqual(selection["selected_kind"], "human_reviewed")
            self.assertTrue(source_selection_matches(job))
            revision_path = valid_artifact_path(job, "source_review_revision")
            assert revision_path is not None
            revision = json.loads(revision_path.read_text(encoding="utf-8"))
            self.assertEqual(
                revision["reviewed_source_cues_checksum"],
                file_checksum(source_path),
            )
            self.assertEqual(job.stages[StageName.SUBTITLE_QA].status, StageStatus.SUCCEEDED)
            self.assertTrue(all(path.is_file() for path in paths))

    def test_human_selection_rejects_mismatched_revision_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "Changed."
            response.write_text(json.dumps(payload), encoding="utf-8")
            apply_source_review_fixes(job_file, response)
            job = load_job(job_file)
            revision_path = valid_artifact_path(job, "source_review_revision")
            assert revision_path is not None
            revision = json.loads(revision_path.read_text(encoding="utf-8"))
            revision["reviewed_source_cues_checksum"] = "wrong"
            revision_path.write_text(json.dumps(revision), encoding="utf-8")
            for artifact in job.artifacts:
                if artifact.kind == "source_review_revision":
                    artifact.checksum = file_checksum(revision_path)
            save_job(job)

            self.assertFalse(source_selection_matches(load_job(job_file)))

    def test_stale_response_and_unknown_ids_do_not_change_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["id"] = 99
            payload["items"][0]["text"] = "Changed"
            response.write_text(json.dumps(payload), encoding="utf-8")
            before = job_file.read_bytes()

            with self.assertRaisesRegex(UserInputError, "stale"):
                apply_source_review_fixes(job_file, response)

            self.assertEqual(job_file.read_bytes(), before)

    def test_prepare_does_not_overwrite_existing_user_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = prepare_source_review(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "Unsaved user edit"
            response.write_text(json.dumps(payload), encoding="utf-8")

            repeated = prepare_source_review(job_file, [1])

            self.assertEqual(repeated, response)
            persisted = json.loads(repeated.read_text(encoding="utf-8"))
            self.assertEqual(persisted["items"][0]["text"], "Unsaved user edit")

    def test_prepare_source_review_replaces_srt_symlink_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            response = prepare_source_review(job_file, [1])
            srt_path = response.with_suffix(".srt")
            outside = root / "outside.srt"
            outside.write_text("sentinel", encoding="utf-8")
            srt_path.unlink()
            srt_path.symlink_to(outside)

            prepare_source_review(job_file, [1])

            self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel")
            self.assertTrue(srt_path.is_file())
            self.assertFalse(srt_path.is_symlink())

    def test_source_review_srt_matches_json_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "json").mkdir()
            (root / "srt").mkdir()
            json_job = self._job(root / "json")
            json_response = prepare_source_review(json_job, [1])
            json_payload = json.loads(json_response.read_text(encoding="utf-8"))
            json_payload["items"][0]["text"] = "Corrected source text."
            json_response.write_text(json.dumps(json_payload), encoding="utf-8")
            apply_source_review_fixes(json_job, json_response)
            json_revision = valid_artifact_path(load_job(json_job), "source_review_revision")
            assert json_revision is not None

            srt_job = self._job(root / "srt")
            srt_template = prepare_source_review(srt_job, [1]).with_suffix(".srt")
            srt_text = srt_template.read_text(encoding="utf-8").replace(
                "GPT's 5.6 soul.",
                "Corrected source text.",
            )
            srt_template.write_text(srt_text, encoding="utf-8")
            apply_source_review_fixes(srt_job, srt_template)
            srt_revision = valid_artifact_path(load_job(srt_job), "source_review_revision")
            assert srt_revision is not None

            json_data = json.loads(json_revision.read_text(encoding="utf-8"))
            srt_data = json.loads(srt_revision.read_text(encoding="utf-8"))
            for data in (json_data, srt_data):
                data.pop("revision_fingerprint")
                data.pop("previous_source_cues_checksum")
                data.pop("reviewed_source_cues_checksum")
            self.assertEqual(json_data, srt_data)

    def test_source_review_srt_rejects_timing_add_remove_marker_and_stale_name(self) -> None:
        cases = {
            "timing": (
                "00:00:00,000 --> 00:00:01,000",
                "00:00:00,100 --> 00:00:01,000",
                "timing",
            ),
            "added": (
                "#1\nGPT's 5.6 soul.",
                "#1\nGPT's 5.6 soul.\n\n2\n00:00:01,000 --> 00:00:02,000\n#2\nAdded.",
                "stale",
            ),
            "removed": (
                "1\n00:00:00,000 --> 00:00:01,000\n#1\nGPT's 5.6 soul.\n",
                "",
                "non-empty",
            ),
            "marker": ("#1", "#2", "does not match"),
        }
        for name, (old, new, message) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                job_file = self._job(Path(directory))
                response = prepare_source_review(job_file, [1]).with_suffix(".srt")
                response.write_text(
                    response.read_text(encoding="utf-8").replace(old, new),
                    encoding="utf-8",
                )
                before = job_file.read_bytes()
                with self.assertRaisesRegex(UserInputError, message):
                    apply_source_review_fixes(job_file, response)
                self.assertEqual(job_file.read_bytes(), before)

        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = prepare_source_review(job_file, [1]).with_suffix(".srt")
            response.rename(response.with_name(f"{source_review_stem('0' * 32)}.srt"))
            stale = response.with_name(f"{source_review_stem('0' * 32)}.srt")
            with self.assertRaisesRegex(UserInputError, "stale"):
                apply_source_review_fixes(job_file, stale)

    def _snapshot(self, root: Path) -> dict[str, str]:
        """Job 目录的全文件 checksum 快照。

        排除 `.xvt.lock`：它是取 Job 锁留下的执行痕迹（跑任何命令都会重写，内容只是
        PID），不是产物写入。把它算进来会让「只读」断言在走 CLI 的用例上必然红，而
        那红的是锁不是预检。
        """

        return {
            str(p.relative_to(root)): file_checksum(p)
            for p in root.rglob("*")
            if p.is_file() and p.name != ".xvt.lock"
        }

    def _overlap_timeline(self, job_file: Path) -> None:
        """把两条 cue 的时间轴改成重叠，让修订后的源字幕 QA 必然判 failed。"""

        job = load_job(job_file)
        source_path = valid_artifact_path(job, "source_cues")
        assert source_path is not None
        cues = json.loads(source_path.read_text(encoding="utf-8"))
        cues[1]["start_ms"] = cues[0]["start_ms"]
        cues[1]["end_ms"] = cues[0]["end_ms"]
        source_path.write_text(json.dumps(cues), encoding="utf-8")
        for artifact in job.artifacts:
            if artifact.kind == "source_cues":
                artifact.checksum = file_checksum(source_path)
        save_job(job)

    def test_source_preflight_is_read_only(self) -> None:
        """最要紧的一条：预检一个字节都不许写。

        `_prepare_source_revision` 是从 `apply_source_review_fixes` 第一次 `write_json`
        之前切出来的，而「那一段全是只读」是读代码得出的判断——万一其中某个 helper
        内部悄悄 `save_job`，预检就不再只读，直接违反 AGENTS 约束 4。所以这里对整个
        Job 目录做 checksum 快照来钉死，而不是靠再读一遍代码确认。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            response = prepare_source_review(job_file, [1]).with_suffix(".srt")
            response.write_text(
                response.read_text(encoding="utf-8").replace("GPT's 5.6 soul.", "GPT-5.6 soul."),
                encoding="utf-8",
            )
            before = self._snapshot(root)
            result = preflight_source_review_fixes(job_file, response)
            self.assertTrue(result["valid"])
            self.assertEqual(result["task"], "source_review_preflight")
            self.assertEqual(result["changed_ids"], [1])
            self.assertEqual(before, self._snapshot(root))

    def test_source_preflight_predicts_the_qa_failure_before_anything_is_written(self) -> None:
        """预检先报 failed，正式 apply 再如实走到「写盘之后才抛错」。

        这正是本条要防的局面：源文修订的既有契约是 QA 失败也照旧写盘、失效全部下游，
        最后才抛 RuntimeError（`apply_source_review_fixes` 末尾）。预检的价值是让用户
        在动手前就知道会被带到哪里，而不是改掉那条契约——所以这里同时断言预检报
        `valid: false`，以及 apply 的行为一字未变。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            self._overlap_timeline(job_file)
            response = prepare_source_review(job_file, [2]).with_suffix(".srt")
            response.write_text(
                response.read_text(encoding="utf-8").replace(
                    "Keep this line.", "Corrected second cue."
                ),
                encoding="utf-8",
            )

            before = self._snapshot(root)
            result = preflight_source_review_fixes(job_file, response)
            self.assertFalse(result["valid"])
            self.assertEqual(result["qa_status"], "failed")
            self.assertTrue(result["blockers"])
            self.assertEqual(before, self._snapshot(root), "预检不得写盘，即便结论是失败")

            # 既有契约未变：照旧写盘、失效下游，最后才抛错。
            with self.assertRaisesRegex(RuntimeError, "subtitle QA failed"):
                apply_source_review_fixes(job_file, response)
            self.assertNotEqual(before, self._snapshot(root), "apply 确实写了盘")

    def test_cli_check_now_accepts_source_review_and_still_refuses_continue(self) -> None:
        """`apply --check` 放宽到源文修订，但两条既有拒绝保持不动。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._job(root)
            response = prepare_source_review(job_file, [1]).with_suffix(".srt")
            response.write_text(
                response.read_text(encoding="utf-8").replace("GPT's 5.6 soul.", "GPT-5.6 soul."),
                encoding="utf-8",
            )

            before = self._snapshot(root)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = main(["apply", str(job_file), str(response), "--check"])
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["task"], "source_review_preflight")
            self.assertTrue(payload["valid"])
            self.assertEqual(before, self._snapshot(root), "CLI 预检同样不得写盘")

            # --check 与 --continue 不能组合；源文修订本身也拒绝 --continue。
            self.assertEqual(
                main(["apply", str(job_file), str(response), "--check", "--continue"]), 2
            )
            self.assertEqual(main(["apply", str(job_file), str(response), "--continue"]), 2)
            self.assertEqual(before, self._snapshot(root), "被拒绝的组合不得留下痕迹")

    def test_source_review_srt_uses_marker_to_disambiguate_shared_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            job = load_job(job_file)
            source_path = valid_artifact_path(job, "source_cues")
            assert source_path is not None
            cues = json.loads(source_path.read_text(encoding="utf-8"))
            cues[1]["start_ms"] = cues[0]["start_ms"]
            cues[1]["end_ms"] = cues[0]["end_ms"]
            source_path.write_text(json.dumps(cues), encoding="utf-8")
            for artifact in job.artifacts:
                if artifact.kind == "source_cues":
                    artifact.checksum = file_checksum(source_path)
            save_job(job)
            response = prepare_source_review(job_file, [2]).with_suffix(".srt")
            response.write_text(
                response.read_text(encoding="utf-8").replace(
                    "Keep this line.",
                    "Corrected second cue.",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "subtitle QA failed"):
                apply_source_review_fixes(job_file, response)

            reviewed_path = valid_artifact_path(load_job(job_file), "source_cues")
            assert reviewed_path is not None
            reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
            self.assertEqual(reviewed[0]["source_text"], "GPT's 5.6 soul.")
            self.assertEqual(reviewed[1]["source_text"], "Corrected second cue.")

    def test_source_review_srt_preserves_unedited_multiline_source_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            job = load_job(job_file)
            source_path = valid_artifact_path(job, "source_cues")
            assert source_path is not None
            cues = json.loads(source_path.read_text(encoding="utf-8"))
            cues[1]["source_text"] = (
                "First line.\n\n  indented second line.\n"
                "<i>literal</i> {\\an8}literal literal\\Ntext"
            )
            source_path.write_text(json.dumps(cues), encoding="utf-8")
            for artifact in job.artifacts:
                if artifact.kind == "source_cues":
                    artifact.checksum = file_checksum(source_path)
            save_job(job)
            response = prepare_source_review(job_file, [1, 2]).with_suffix(".srt")
            response.write_text(
                response.read_text(encoding="utf-8").replace(
                    "GPT's 5.6 soul.",
                    "Corrected first cue.",
                ),
                encoding="utf-8",
            )

            apply_source_review_fixes(job_file, response)

            reviewed_path = valid_artifact_path(load_job(job_file), "source_cues")
            assert reviewed_path is not None
            reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
            self.assertEqual(reviewed[0]["source_text"], "Corrected first cue.")
            self.assertEqual(
                reviewed[1]["source_text"],
                "First line.\n\n  indented second line.\n"
                "<i>literal</i> {\\an8}literal literal\\Ntext",
            )

    def test_response_rejects_timing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "Changed"
            payload["items"][0]["start_ms"] = 500
            response.write_text(json.dumps(payload), encoding="utf-8")
            before = job_file.read_bytes()

            with self.assertRaisesRegex(UserInputError, "only id and text"):
                apply_source_review_fixes(job_file, response)

            self.assertEqual(job_file.read_bytes(), before)

    def test_source_only_job_keeps_unrequested_stages_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nOriginal.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            job_file = get_job_file(job)
            response = prepare_source_review(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "Corrected."
            response.write_text(json.dumps(payload), encoding="utf-8")

            apply_source_review_fixes(job_file, response)

            persisted = load_job(job_file)
            for stage in (StageName.TRANSLATE, StageName.BURN, StageName.MEDIA_QA):
                self.assertEqual(persisted.stages[stage].status, StageStatus.SKIPPED)

    def test_qa_needs_review_persists_revision_without_source_srt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "x" * 80
            response.write_text(json.dumps(payload), encoding="utf-8")

            apply_source_review_fixes(job_file, response)

            job = load_job(job_file)
            self.assertEqual(
                job.stages[StageName.SUBTITLE_QA].status,
                StageStatus.AWAITING_INPUT,
            )
            self.assertIsNotNone(valid_artifact_path(job, "source_review_revision"))
            self.assertIsNotNone(valid_artifact_path(job, "subtitle_qa"))
            self.assertIsNone(valid_artifact_path(job, "source_srt"))
            self.assertTrue(source_selection_matches(job))

    def test_acknowledged_review_resumes_from_edited_source_cues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "x" * 80
            response.write_text(json.dumps(payload), encoding="utf-8")
            apply_source_review_fixes(job_file, response)

            acknowledge_subtitle_review(job_file)
            result = continue_workflow(job_file)

            job = load_job(job_file)
            source_path = valid_artifact_path(job, "source_cues")
            assert source_path is not None
            cues = json.loads(source_path.read_text(encoding="utf-8"))
            self.assertEqual(cues[0]["source_text"], "x" * 80)
            self.assertIsNotNone(valid_artifact_path(job, "source_srt"))
            self.assertEqual(result.status, "awaiting_input")

    def test_imported_translation_workflow_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "Changed"
            response.write_text(json.dumps(payload), encoding="utf-8")
            job = load_job(job_file)
            workflow = WorkflowConfig(
                target_language="zh-CN",
                translation_subtitles="/tmp/translated.srt",
            )
            set_workflow_config(job, workflow)
            save_job(job)

            with self.assertRaisesRegex(UserInputError, "imported translation"):
                prepare_source_review(job_file, [1])
            with self.assertRaisesRegex(UserInputError, "imported translation"):
                apply_source_review_fixes(job_file, response)

    def test_save_failure_keeps_previous_job_and_artifact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            response = self._response(job_file, [1])
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "Changed source text."
            response.write_text(json.dumps(payload), encoding="utf-8")
            job = load_job(job_file)
            old_source = valid_artifact_path(job, "source_cues")
            assert old_source is not None
            before_job = job_file.read_bytes()
            before_source = old_source.read_bytes()

            with (
                patch("xvt.pipeline.revision.save_job", side_effect=OSError("disk full")),
                self.assertRaisesRegex(OSError, "disk full"),
            ):
                apply_source_review_fixes(job_file, response)

            self.assertEqual(job_file.read_bytes(), before_job)
            self.assertEqual(old_source.read_bytes(), before_source)

    def test_events_do_not_include_subtitle_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            job_file = self._job(Path(directory))
            events = []
            response = prepare_source_review(job_file, [1], event_sink=events.append)
            payload = json.loads(response.read_text(encoding="utf-8"))
            payload["items"][0]["text"] = "TOP SECRET SOURCE TEXT"
            response.write_text(json.dumps(payload), encoding="utf-8")

            apply_source_review_fixes(job_file, response, event_sink=events.append)

            serialized = "\n".join(event.to_json() for event in events)
            self.assertNotIn("TOP SECRET SOURCE TEXT", serialized)
            self.assertIn("source_review_input_ready", serialized)
            self.assertIn("source_review_revised", serialized)
