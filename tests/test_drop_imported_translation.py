"""`xvt drop-imported-translation`：显式放弃导入译文，回到重新翻译的常规路径。

场景全部用**真实 CLI 命令**造（`xvt run --subtitles --translation-subtitles`，
必要时再加一次 `xvt rebuild-source --strategy asr`）。打桩只替掉 ffprobe、
ffmpeg 抽音、ASR 模型与翻译 provider 四处网络/模型调用，产物照真实运行落盘、
artifact 照真实登记、stage 照真实推进。
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from xvt.cli import main
from xvt.job_store import get_job_file, load_job, save_job
from xvt.media import MediaInfo
from xvt.models import StageName, StageStatus, Transcript, Word
from xvt.pipeline import run_subtitle_transcription
from xvt.process import CommandResult
from xvt.recovery import build_recovery_plan, valid_artifact_path, verify_job
from xvt.workflow import plan_advance_steps
from xvt.workflow_config import get_workflow_config, require_workflow_config

PROVIDER_ENV = {
    "XVT_TRANSLATION_BASE_URL": "https://provider.example.com/v1",
    "XVT_TRANSLATION_MODEL": "test-model",
    "XVT_TRANSLATION_API_KEY": "secret",
}

# `rebuild-source` 用的 ASR 词表：新时间轴与已存译文完全对不上（P4 死局）。
UNMAPPABLE_WORDS = (Word("Hello.", 0, 500, 0.9),)
# 对照组：重建后的时间轴仍能套回已存译文。
MAPPABLE_WORDS = (Word("Hello.", 200, 1_500, 0.9), Word("Second.", 3_000, 4_500, 0.9))


def _media_info(media: Path) -> MediaInfo:
    return MediaInfo(
        path=str(media),
        duration_ms=5_000,
        size_bytes=media.stat().st_size,
        format_name="mp4",
        width=1920,
        height=1080,
        frame_rate=30.0,
        video_codec="h264",
        audio_codec="aac",
        has_video=True,
        has_audio=True,
    )


def _provider(batch, _config, *, correction=None, **_kwargs):
    return {item.id: "你好。" for item in batch.items}


class DropImportedTranslationTest(unittest.TestCase):
    def _imported_job(self, root: Path) -> Path:
        """`xvt run --translation-subtitles` 造出的、译文来自导入字幕的健康 Job。"""

        media = root / "input.mp4"
        media.write_bytes(b"video")
        source = root / "source.srt"
        source.write_text(
            "1\n00:00:00,200 --> 00:00:01,500\nHello world.\n\n"
            "2\n00:00:03,000 --> 00:00:04,500\nSecond line.\n",
            encoding="utf-8",
        )
        translated = root / "translated.srt"
        translated.write_text(
            "1\n00:00:00,200 --> 00:00:01,500\n你好，世界。\n\n"
            "2\n00:00:03,000 --> 00:00:04,500\n第二行。\n",
            encoding="utf-8",
        )
        info = _media_info(media)
        with (
            patch("xvt.pipeline.common.probe_media", return_value=info),
            patch("xvt.pipeline.transcription.probe_media", return_value=info),
        ):
            self.assertEqual(
                main(
                    [
                        "run",
                        str(media),
                        "--output",
                        str(root / "work"),
                        "--subtitles",
                        str(source),
                        "--translation-subtitles",
                        str(translated),
                        "--target-language",
                        "zh-CN",
                    ]
                ),
                0,
            )
        return next((root / "work").rglob("job.json"))

    def _rebuilt_job(self, root: Path, *, words: tuple[Word, ...]) -> Path:
        job_file = self._imported_job(root)
        self._rebuild_source(job_file, root, words=words)
        return job_file

    def _rebuild_source(self, job_file: Path, root: Path, *, words: tuple[Word, ...]) -> None:
        info = _media_info(root / "input.mp4")

        def run_ffmpeg(args, **_kwargs):
            Path(args[-1]).write_bytes(b"audio")
            return CommandResult(tuple(args), 0, "", "")

        with (
            patch("xvt.pipeline.common.probe_media", return_value=info),
            patch("xvt.pipeline.transcription.probe_media", return_value=info),
            patch("xvt.pipeline.transcription.run_command", side_effect=run_ffmpeg),
            patch(
                "xvt.pipeline.transcription.transcribe_audio",
                return_value=Transcript(language="en", engine="mlx", words=words),
            ),
            patch("sys.stdout", new_callable=StringIO),
        ):
            self.assertEqual(main(["rebuild-source", str(job_file), "--strategy", "asr"]), 0)

    def _registered_kinds(self, job_file: Path) -> set[str]:
        return {artifact.kind for artifact in load_job(job_file).artifacts}

    # --- 端到端自证：P4 死局被真的拆掉，而且 Job 能走到 completed ---------------

    def test_drop_clears_the_deadlock_and_the_job_reaches_completed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._rebuilt_job(root, words=UNMAPPABLE_WORDS)
            before = load_job(job_file)
            retained = valid_artifact_path(before, "input_translation_subtitles")
            assert retained is not None
            self.assertEqual(
                require_workflow_config(before).translation_subtitles,
                str((root / "translated.srt").resolve()),
            )

            with patch("sys.stdout", new_callable=StringIO):
                self.assertEqual(main(["drop-imported-translation", str(job_file)]), 0)

            after = load_job(job_file)
            # 双源同时清：少清一处，workflow 会从另一处把它捡回来。
            self.assertNotIn("input_translation_subtitles", self._registered_kinds(job_file))
            self.assertIsNone(require_workflow_config(after).translation_subtitles)
            # 保留文件，不删字节。
            self.assertTrue(retained.is_file())
            self.assertTrue((root / "translated.srt").is_file())
            # 失效闭包：导入译文的全部下游都不再登记。
            self.assertEqual(
                self._registered_kinds(job_file)
                & {
                    "translation_mapping",
                    "translated_cues",
                    "translated_srt",
                    "bilingual_srt",
                    "bilingual_ass",
                    "translated_subtitle_qa",
                },
                set(),
            )
            self.assertIs(after.stages[StageName.TRANSLATE].status, StageStatus.PENDING)
            # 计划从 import_translation 变回 translate：死局本身消失。
            self.assertEqual(
                plan_advance_steps(after, require_workflow_config(after)), ("translate",)
            )

            info = _media_info(root / "input.mp4")
            with (
                patch.dict(os.environ, PROVIDER_ENV, clear=False),
                patch("xvt.pipeline.common.probe_media", return_value=info),
                patch("xvt.pipeline.translation.translate_batch", side_effect=_provider),
                patch("sys.stdout", new_callable=StringIO),
            ):
                self.assertEqual(main(["continue", str(job_file)]), 0)

            self.assertEqual(build_recovery_plan(job_file).category, "complete")
            self.assertEqual(verify_job(job_file)["status"], "pass")
            self.assertIs(
                load_job(job_file).stages[StageName.TRANSLATE].status, StageStatus.SUCCEEDED
            )

    def test_drop_defuses_the_mine_that_a_plain_retranslation_leaves_behind(self) -> None:
        """对照：原子 `translate` 绕过之后死局复发，drop 之后不复发。

        这是这条命令存在的全部理由，所以用同一套真实 CLI 跑两遍对照：两条路径都能
        把 Job 送到 `completed`，但只有 drop 那条在**再跑一次 `rebuild-source`** 之后
        仍然是可推进的 `retryable`；绕过那条原地回到 `manual_fix`。
        """

        second_pass = (Word("Hi.", 0, 400, 0.9),)
        info_env = PROVIDER_ENV

        def finish(job_file: Path, root: Path) -> None:
            with (
                patch.dict(os.environ, info_env, clear=False),
                patch(
                    "xvt.pipeline.common.probe_media",
                    return_value=_media_info(root / "input.mp4"),
                ),
                patch("xvt.pipeline.translation.translate_batch", side_effect=_provider),
                patch("sys.stdout", new_callable=StringIO),
            ):
                self.assertEqual(main(["continue", str(job_file)]), 0)
            self.assertEqual(build_recovery_plan(job_file).category, "complete")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._rebuilt_job(root, words=UNMAPPABLE_WORDS)
            with (
                patch.dict(os.environ, info_env, clear=False),
                patch(
                    "xvt.pipeline.common.probe_media",
                    return_value=_media_info(root / "input.mp4"),
                ),
                patch("xvt.pipeline.translation.translate_batch", side_effect=_provider),
                patch("sys.stdout", new_callable=StringIO),
            ):
                self.assertEqual(
                    main(
                        [
                            "translate",
                            str(job_file),
                            "--target-language",
                            "zh-CN",
                            "--provider",
                            "openai-compatible",
                        ]
                    ),
                    0,
                )
            finish(job_file, root)
            # 绕过之后两处来源原样留着。
            bypassed = load_job(job_file)
            self.assertIn("input_translation_subtitles", self._registered_kinds(job_file))
            self.assertIsNotNone(require_workflow_config(bypassed).translation_subtitles)
            self._rebuild_source(job_file, root, words=second_pass)
            self.assertEqual(build_recovery_plan(job_file).category, "manual_fix")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._rebuilt_job(root, words=UNMAPPABLE_WORDS)
            with patch("sys.stdout", new_callable=StringIO):
                self.assertEqual(main(["drop-imported-translation", str(job_file)]), 0)
            finish(job_file, root)
            self._rebuild_source(job_file, root, words=second_pass)
            self.assertEqual(build_recovery_plan(job_file).category, "retryable")

    def test_dropping_twice_is_refused_once_there_is_nothing_left_to_drop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._rebuilt_job(root, words=UNMAPPABLE_WORDS)
            with patch("sys.stdout", new_callable=StringIO):
                self.assertEqual(main(["drop-imported-translation", str(job_file)]), 0)
            self.assertEqual(main(["drop-imported-translation", str(job_file)]), 2)

    # --- 设计要点 2 的守卫：drop 不能是一句空话 --------------------------------

    def test_drop_invalidates_downstream_even_when_the_translation_still_maps(self) -> None:
        """健康导入 Job 上 drop 之后，译文必须真的失效，`continue` 不得判满足完成。

        只摘 `input_translation_subtitles` 登记而不带失效闭包时，`translated_cues`
        仍然 checksum 有效，`_ensure_translation` 更早的一道早退会判满足，产物
        **仍然来自被放弃的译文**——drop 等于没做。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._imported_job(root)
            self.assertEqual(build_recovery_plan(job_file).category, "complete")
            translated_before = valid_artifact_path(load_job(job_file), "translated_cues")
            assert translated_before is not None

            with patch("sys.stdout", new_callable=StringIO):
                self.assertEqual(main(["drop-imported-translation", str(job_file)]), 0)

            after = load_job(job_file)
            self.assertIsNone(valid_artifact_path(after, "translated_cues"))
            self.assertNotEqual(build_recovery_plan(job_file).category, "complete")
            self.assertEqual(
                plan_advance_steps(after, require_workflow_config(after)), ("translate",)
            )

            # 没有 API 配置时 `continue` 落到 Agent 等待，而**不是**宣告完成。
            info = _media_info(root / "input.mp4")
            missing_env = dict.fromkeys(PROVIDER_ENV, "")
            with (
                patch.dict(os.environ, missing_env, clear=False),
                patch("xvt.pipeline.common.probe_media", return_value=info),
                patch("sys.stdout", new_callable=StringIO),
            ):
                self.assertEqual(main(["continue", str(job_file)]), 0)
            self.assertIs(
                load_job(job_file).stages[StageName.TRANSLATE].status,
                StageStatus.AWAITING_INPUT,
            )

    # --- 边界与语义 -------------------------------------------------------------

    def test_rejects_a_job_that_never_imported_a_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nHello.\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            job_file = get_job_file(job)

            self.assertEqual(main(["drop-imported-translation", str(job_file)]), 2)

    def test_supports_an_atomic_job_without_a_persisted_workflow(self) -> None:
        """原子导入 Job（`config.workflow` 为 None）同样支持：只有 artifact 一源。

        drop 之后 `revision` / `resegment` 的「config 或 artifact」双源守卫会翻转，
        这条测试把翻转后的语义钉住。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            translated = root / "zh.srt"
            translated.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\n你好，世界。\n",
                encoding="utf-8",
            )
            job = run_subtitle_transcription(source, root, language="en")
            job_file = get_job_file(job)
            save_job(job)
            with patch("sys.stdout", new_callable=StringIO):
                self.assertEqual(
                    main(
                        [
                            "import-translation",
                            str(job_file),
                            str(translated),
                            "--target-language",
                            "zh-CN",
                        ]
                    ),
                    0,
                )
            self.assertIsNone(get_workflow_config(load_job(job_file)))
            from xvt.pipeline import source_review_supported

            self.assertFalse(source_review_supported(load_job(job_file)))

            with patch("sys.stdout", new_callable=StringIO):
                self.assertEqual(main(["drop-imported-translation", str(job_file)]), 0)

            after = load_job(job_file)
            self.assertNotIn("input_translation_subtitles", self._registered_kinds(job_file))
            self.assertIsNone(valid_artifact_path(after, "translated_cues"))
            # 守卫翻转：不再有导入译文，人工源文修订重新可用。
            self.assertTrue(source_review_supported(after))

    def test_events_record_the_abandoned_file_and_the_invalidated_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # 用健康导入 Job：P4 状态下 `rebuild-source` 已经把下游带走了，
            # 留痕要连「失效了哪些下游」一起验，必须有下游可失效。
            job_file = self._imported_job(root)
            retained = valid_artifact_path(load_job(job_file), "input_translation_subtitles")
            assert retained is not None

            output = StringIO()
            with patch("sys.stdout", output):
                self.assertEqual(
                    main(["drop-imported-translation", str(job_file), "--events-jsonl"]), 0
                )
            events = [json.loads(line) for line in output.getvalue().splitlines() if line]
            types = [event["type"] for event in events]

            self.assertIn("artifact_invalidated", types)
            self.assertIn("translation_import_dropped", types)
            dropped = next(
                event for event in events if event["type"] == "translation_import_dropped"
            )
            self.assertEqual(dropped["data"]["kind"], "input_translation_subtitles")
            self.assertEqual(dropped["data"]["path"], str(retained))
            self.assertIsInstance(dropped["data"]["checksum"], str)
            self.assertEqual(
                dropped["data"]["translation_subtitles"],
                str((root / "translated.srt").resolve()),
            )
            invalidated = {
                event["data"]["kind"] for event in events if event["type"] == "artifact_invalidated"
            }
            self.assertIn("input_translation_subtitles", invalidated)
            self.assertIn("translated_cues", invalidated)

    def test_recovery_navigation_offers_drop_as_an_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._rebuilt_job(root, words=UNMAPPABLE_WORDS)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "manual_fix")
            drop_command = next(
                (
                    command
                    for command in plan.alternative_commands
                    if "xvt drop-imported-translation" in command
                ),
                None,
            )
            assert drop_command is not None
            argv = shlex.split(drop_command)
            self.assertEqual(argv[:2], ["xvt", "drop-imported-translation"])
            self.assertEqual(Path(argv[2]), job_file.resolve())
            self.assertEqual(argv[3:], [])

    def test_mappable_job_is_untouched_when_drop_is_not_run(self) -> None:
        """反证：没跑 drop 时既有语义零变化（映射得上的 Job 仍走 continue）。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job_file = self._rebuilt_job(root, words=MAPPABLE_WORDS)

            plan = build_recovery_plan(job_file)

            self.assertEqual(plan.category, "retryable")
            self.assertIn("xvt continue", plan.primary_command or "")


if __name__ == "__main__":
    unittest.main()
