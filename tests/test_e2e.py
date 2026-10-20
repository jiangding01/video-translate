import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pysubs2
from ffmpeg_support import FFMPEG_REQUIREMENT, ffmpeg_can_render_ass

from xvt.cli import main
from xvt.events import EventType
from xvt.job_store import get_job_file, load_job
from xvt.media import probe_media
from xvt.models import StageName, StageStatus, Transcript, Word
from xvt.pipeline import (
    apply_subtitle_translation,
    burn_translated_job,
    preview_translated_job,
    run_local_transcription,
)
from xvt.recovery import build_recovery_plan, file_checksum, verify_job
from xvt.workflow import (
    WorkflowOptions,
    plan_advance_steps,
    relink_source_media,
    run_workflow,
)
from xvt.workflow_config import require_workflow_config


@unittest.skipUnless(ffmpeg_can_render_ass(), FFMPEG_REQUIREMENT)
class EndToEndTest(unittest.TestCase):
    def _create_video(self, path: Path) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x18202b:s=640x360:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(path),
            ],
            capture_output=True,
            check=True,
        )

    def test_existing_subtitles_to_burned_bilingual_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            self._create_video(media)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\n你好，世界。\n",
                encoding="utf-8",
            )

            job = run_local_transcription(media, root / "output", subtitles=source, language="en")
            apply_subtitle_translation(
                get_job_file(job),
                translated,
                target_language="zh-CN",
            )
            burned = burn_translated_job(get_job_file(job), hardware_encoding=False)
            info = probe_media(burned)

            self.assertTrue(burned.is_file())
            self.assertTrue(info.has_video)
            self.assertTrue(info.has_audio)
            self.assertEqual(info.video_codec, "h264")
            self.assertEqual(info.audio_codec, "aac")
            self.assertLessEqual(abs(info.duration_ms - 2000), 500)

    def test_touching_source_media_keeps_a_really_burned_job_complete(self) -> None:
        """真实 Job 路径：``run`` 跑完烧录后，只动源媒体 mtime 不得要求整段重编码。

        这条刻意不打桩（零网络、零模型：源字幕与译文都由用户提供，FFmpeg 真跑），
        产物真的落盘、artifact 真的登记、stage 真的推进、v2 proof 真的由工作流签发，
        否则「touch 后仍判完成」这条性质在生产路径上并没有被验到。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            self._create_video(media)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\n你好，世界。\n",
                encoding="utf-8",
            )

            result = run_workflow(
                WorkflowOptions(
                    input=str(media),
                    output=root / "work",
                    subtitles=source,
                    translation_subtitles=translated,
                    target_language="zh-CN",
                    burn=True,
                    hardware_encoding=False,
                )
            )

            self.assertEqual(result.status, "completed")
            job_file = result.job_file
            job = load_job(job_file)
            self.assertIs(job.stages[StageName.BURN].status, StageStatus.SUCCEEDED)
            self.assertIs(job.stages[StageName.MEDIA_QA].status, StageStatus.SUCCEEDED)
            proof_path = Path(
                next(item.path for item in job.artifacts if item.kind == "burn_fingerprint")
            )
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            self.assertEqual(proof["version"], 2)
            self.assertEqual(proof["source_media_checksum"], file_checksum(media))
            self.assertEqual(build_recovery_plan(job_file).category, "complete")

            before = media.stat()
            os.utime(media, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            self.assertNotEqual(media.stat().st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(file_checksum(media), proof["source_media_checksum"])

            job = load_job(job_file)
            self.assertEqual(plan_advance_steps(job, require_workflow_config(job)), ())
            plan = build_recovery_plan(job_file)
            self.assertEqual(plan.category, "complete")
            self.assertEqual(plan.advance_steps, ())
            # 回退的授权依据就在这份 proof 里：没有烧录时的媒体身份就证明不了
            # 「指纹差异只来自文件元数据」。
            self.assertEqual(proof["source_media_identity"]["path"], str(media.resolve()))

    def test_relink_source_media_keeps_a_really_burned_job_complete(self) -> None:
        """真实 Job 路径：源媒体永久搬家后，``relink-source-media`` 必须零重烧回到完成。

        与上一条同样不打桩（零网络、零模型，FFmpeg 真跑，产物真的落盘、artifact 真的
        登记、v2 proof 真的由工作流签发）。搬家后 ``status`` 先落到 ``manual_fix``
        （render / burn 都要读实际媒体），命令把持久化的 ``burn_media`` 指到新位置后
        Job 直接回到 ``complete``——「没有重烧」由成品的 checksum **与 mtime** 都没变
        以及 proof 文件未被重新签发共同证明。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            self._create_video(media)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\n你好，世界。\n",
                encoding="utf-8",
            )

            result = run_workflow(
                WorkflowOptions(
                    input=str(media),
                    output=root / "work",
                    subtitles=source,
                    translation_subtitles=translated,
                    target_language="zh-CN",
                    burn=True,
                    hardware_encoding=False,
                )
            )

            self.assertEqual(result.status, "completed")
            job_file = result.job_file
            job = load_job(job_file)
            self.assertIsNone(require_workflow_config(job).burn_media)
            burned = Path(next(item.path for item in job.artifacts if item.kind == "burned_video"))
            proof_path = Path(
                next(item.path for item in job.artifacts if item.kind == "burn_fingerprint")
            )
            burned_checksum = file_checksum(burned)
            burned_mtime_ns = burned.stat().st_mtime_ns
            proof_checksum = file_checksum(proof_path)
            self.assertEqual(build_recovery_plan(job_file).category, "complete")

            moved = root / "newhome" / "input.mp4"
            moved.parent.mkdir()
            media.rename(moved)

            stale = build_recovery_plan(job_file)
            self.assertEqual(stale.category, "manual_fix")
            self.assertIn(str(media), stale.reason)
            self.assertEqual(stale.advance_steps, ("burn",))

            self.assertEqual(relink_source_media(job_file, moved), moved.resolve())

            job = load_job(job_file)
            config = require_workflow_config(job)
            self.assertEqual(config.burn_media, str(moved.resolve()))
            self.assertEqual(plan_advance_steps(job, config), ())
            self.assertEqual(build_recovery_plan(job_file).category, "complete")
            self.assertEqual(verify_job(job_file)["status"], "pass")
            # 没有重烧：成品内容与 mtime 都没动，proof 也没有被重新签发。
            self.assertEqual(file_checksum(burned), burned_checksum)
            self.assertEqual(burned.stat().st_mtime_ns, burned_mtime_ns)
            self.assertEqual(file_checksum(proof_path), proof_checksum)

    def test_recovery_navigation_hands_over_a_runnable_relink_source_media_command(self) -> None:
        """导航推荐的那条命令必须真的能推进：原样取出、替换占位符、照常执行。

        只断言 ``primary_command`` 等于某个字符串不算数——本仓库同类缺陷（推荐一条在
        该状态下必然失败的命令）已经出现过六次，字符串相等正是它们每次都满足的条件。
        这里从真实的「源媒体搬家」状态出发（不打桩，FFmpeg 真跑、产物真落盘、v2 proof
        真的由工作流签发），把恢复计划吐出来的命令行 ``shlex.split`` 开、只把占位符换成
        真实新路径，然后原样喂给 CLI ``main``，断言它退出码 0 且 Job 真的回到 ``complete``。
        「没有重烧」仍由成品 checksum 与 mtime 都未变证明。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            self._create_video(media)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\n你好，世界。\n",
                encoding="utf-8",
            )

            result = run_workflow(
                WorkflowOptions(
                    input=str(media),
                    output=root / "work",
                    subtitles=source,
                    translation_subtitles=translated,
                    target_language="zh-CN",
                    burn=True,
                    hardware_encoding=False,
                )
            )

            self.assertEqual(result.status, "completed")
            job_file = result.job_file
            job = load_job(job_file)
            burned = Path(next(item.path for item in job.artifacts if item.kind == "burned_video"))
            burned_checksum = file_checksum(burned)
            burned_mtime_ns = burned.stat().st_mtime_ns

            moved = root / "newhome" / "input.mp4"
            moved.parent.mkdir()
            media.rename(moved)

            stale = build_recovery_plan(job_file)
            self.assertEqual(stale.category, "manual_fix")
            self.assertIsNotNone(stale.primary_command)
            argv = shlex.split(str(stale.primary_command))
            self.assertEqual(argv[:2], ["xvt", "relink-source-media"])
            self.assertEqual(Path(argv[2]), Path(job_file))
            self.assertEqual(argv[3:], ["NEW_MEDIA_PATH"])
            argv[3] = str(moved)

            self.assertEqual(main(argv[1:]), 0)

            self.assertEqual(build_recovery_plan(job_file).category, "complete")
            self.assertEqual(
                require_workflow_config(load_job(job_file)).burn_media,
                str(moved.resolve()),
            )
            # 推荐的命令零重烧：成品内容与 mtime 都没动。
            self.assertEqual(file_checksum(burned), burned_checksum)
            self.assertEqual(burned.stat().st_mtime_ns, burned_mtime_ns)

    def test_workflow_burn_reuses_the_rendered_layout_qa_and_ass(self) -> None:
        """真实 Job 路径：烧录腿对渲染腿刚产出的译文布局 QA 与双语 ASS 的完整复用判定。

        这条刻意不打桩（零网络、零模型：源字幕与译文都由用户提供，FFmpeg 真跑）：
        渲染腿与烧录腿都真的执行，产物真的落盘、artifact 真的登记、stage 真的推进。
        按**事件计数**断言，而不是只断言产物存在——重复做功恰恰会把同一份产物算两遍，
        逐字相同、状态正确，只有事件数会说出真话。

        复用成立要求报告自己的 ``layout["measurement"] == "measured"``，而行宽测量能否
        成功取决于**本机有没有能渲染译文每个字形的字幕字体**（`qa.check_subtitles` 在
        `measure_bilingual_line_widths` 抛错时回退保守显示单位上限，并记一条
        ``layout_measurement_unavailable``）。装了 CJK 字体的机器（本项目主力平台 macOS）
        走测量成功一路；CI 的 ubuntu-latest 走降级一路（那边装的是 apt 的 ffmpeg，CI 从未
        安装任何字幕字体，而 `render.font_for_language` 在 Linux 上给 zh 要 Noto Sans CJK SC）。
        两条都是真实可达的行为，所以**两条都断言确定的计数**，而不是 skip 掉一条、也不是
        放宽成「1 或 2 都行」：

        - ``measured`` → 复用发生，两份产物各恰登记 1 次。
        - 非 ``measured`` → fail closed 拒绝复用，烧录腿重测重渲，各恰 2 次，**并且**那份
          报告里确实带着 ``layout_measurement_unavailable``。最后这条是关键：它把「为什么
          没复用」钉死在降级上，复用若因别的原因（`_reusable_rendered_layout` 里的真 bug）
          失效，这条测试照样红。

        读的是落盘的 `qa-translated.json`：复用发生时它就是渲染腿那份；没复用时烧录腿按
        同样五项输入重算后原样覆写（确定性报告，无时间戳），`measurement` 仍是同一结论。
        """

        events: list = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            self._create_video(media)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\n你好，世界。\n",
                encoding="utf-8",
            )

            result = run_workflow(
                WorkflowOptions(
                    input=str(media),
                    output=root / "work",
                    subtitles=source,
                    translation_subtitles=translated,
                    target_language="zh-CN",
                    burn=True,
                    hardware_encoding=False,
                ),
                event_sink=events.append,
            )

            self.assertEqual(result.status, "completed")
            job = load_job(result.job_file)
            self.assertIs(job.stages[StageName.BURN].status, StageStatus.SUCCEEDED)
            self.assertIs(job.stages[StageName.MEDIA_QA].status, StageStatus.SUCCEEDED)

            translated_qa_runs = [
                event
                for event in events
                if event.type == EventType.STAGE_STARTED.value
                and event.data.get("stage") == StageName.SUBTITLE_QA.value
                and event.data.get("detail") == "translated"
            ]
            translated_renders = [
                event
                for event in events
                if event.type == EventType.STAGE_STARTED.value
                and event.data.get("stage") == StageName.RENDER.value
                and event.data.get("detail") == "translated"
            ]
            render_runs = [
                event
                for event in events
                if event.type == EventType.STAGE_STARTED.value
                and event.data.get("stage") == StageName.RENDER.value
            ]
            qa_reports = [
                event
                for event in events
                if event.type == EventType.ARTIFACT_CREATED.value
                and event.data.get("kind") == "translated_subtitle_qa"
            ]
            ass_renders = [
                event
                for event in events
                if event.type == EventType.ARTIFACT_CREATED.value
                and event.data.get("kind") == "bilingual_ass"
            ]

            report = json.loads(
                (Path(result.job_file).parent / "artifacts" / "qa-translated.json").read_text(
                    encoding="utf-8"
                )
            )
            layout = report.get("layout")
            self.assertIsInstance(layout, dict, f"report has no layout block: {report}")
            issue_codes = sorted(issue["code"] for issue in report["issues"])
            # 失败时把整条判定依据打进断言消息：CI 上只有这一行能说明复用为何没发生。
            decision = (
                f"measurement={layout.get('measurement')!r} status={report['status']!r} "
                f"issues={issue_codes} qa_reports={len(qa_reports)} ass={len(ass_renders)} "
                f"renders={[event.data.get('detail') for event in render_runs]}"
            )
            print(f"[observe] {decision}")  # TEMP: CI 取证用，取证后回退

            # 两腿都走的记账与复用无关：译文 QA 的 stage_started 只有收尾腿发（烧录腿的
            # `_check_render_layout` 不发），译文渲染的 detail=translated 同理只有渲染腿发。
            self.assertEqual(len(translated_qa_runs), 1, decision)
            self.assertEqual(len(translated_renders), 1, decision)

            if layout.get("measurement") == "measured":
                # 真实测量成立 → 复用发生：两份产物各只登记一次。
                self.assertEqual(len(qa_reports), 1, decision)
                self.assertEqual(len(ass_renders), 1, decision)
                # 源腿渲染（detail=source）之后只应再有译文渲染这一次。
                self.assertEqual(len(render_runs), 2, decision)
            else:
                # 降级报告不复用（fail closed）：烧录腿重测重渲，并自己再记一次 RENDER。
                self.assertIn("layout_measurement_unavailable", issue_codes, decision)
                self.assertEqual(len(qa_reports), 2, decision)
                self.assertEqual(len(ass_renders), 2, decision)
                self.assertEqual(len(render_runs), 3, decision)

    def test_apply_continue_still_blocks_on_failing_translated_subtitle_qa(self) -> None:
        """约束 4 的反证：复用不得让一次本该拦住的译文 QA 被跳过。

        术语表要求 ``world`` 译成 ``世界``，回填的译文故意违反它——``glossary_violation``
        是 error 级，译文 QA 判 ``failed``。``xvt apply --continue`` 必须就此失败，
        既不渲染、也不烧录，更不能因为「上一次布局结果可以复用」而放行。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            self._create_video(media)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            glossary = root / "glossary.json"
            glossary.write_text(
                json.dumps({"terms": [{"source": "world", "target": "世界"}]}),
                encoding="utf-8",
            )

            self.assertEqual(
                main(
                    [
                        "run",
                        str(media),
                        "--output",
                        str(root / "work"),
                        "--subtitles",
                        str(source),
                        "--target-language",
                        "zh-CN",
                        "--provider",
                        "agent",
                        "--glossary",
                        str(glossary),
                        "--burn",
                        "--software",
                    ]
                ),
                0,
            )

            job_file = next((root / "work" / "jobs").glob("*/job.json"))
            manifest = json.loads(
                (job_file.parent / "artifacts" / "translation-batches.json").read_text(
                    encoding="utf-8"
                )
            )
            cue_ids = [item["id"] for batch in manifest["batches"] for item in batch["items"]]
            response = root / "response.json"
            response.write_text(
                json.dumps({"items": [{"id": cue_id, "text": "你好 地球"} for cue_id in cue_ids]}),
                encoding="utf-8",
            )

            self.assertNotEqual(main(["apply", str(job_file), str(response), "--continue"]), 0)

            job = load_job(job_file)
            self.assertIs(job.stages[StageName.SUBTITLE_QA].status, StageStatus.FAILED)
            self.assertIsNot(job.stages[StageName.BURN].status, StageStatus.SUCCEEDED)
            kinds = {item.kind for item in job.artifacts}
            self.assertNotIn("burned_video", kinds)
            self.assertNotIn("bilingual_ass", kinds)

    def test_real_audio_normalization_with_mocked_asr_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            self._create_video(media)
            transcript = Transcript(
                language="en",
                engine="test",
                words=(Word("Hello.", 200, 1200, 1.0),),
            )

            with patch("xvt.pipeline.transcription.transcribe_audio", return_value=transcript):
                job = run_local_transcription(media, root / "output")

            artifacts = {artifact.kind: Path(artifact.path) for artifact in job.artifacts}
            audio = probe_media(artifacts["normalized_audio"])
            self.assertTrue(audio.has_audio)
            self.assertFalse(audio.has_video)
            self.assertEqual(audio.audio_codec, "pcm_s16le")
            self.assertTrue(artifacts["source_srt"].is_file())

    def test_nonzero_start_bilingual_preview_has_expected_duration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            self._create_video(media)
            source = root / "source.srt"
            source.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\nHello world.\n",
                encoding="utf-8",
            )
            translated = root / "translated.srt"
            translated.write_text(
                "1\n00:00:00,200 --> 00:00:01,500\n你好，世界。\n",
                encoding="utf-8",
            )

            job = run_local_transcription(media, root / "output", subtitles=source, language="en")
            job_file = get_job_file(job)
            apply_subtitle_translation(job_file, translated, target_language="zh-CN")
            preview = preview_translated_job(
                job_file,
                start_seconds=0.5,
                duration_seconds=1,
                hardware_encoding=False,
            )
            info = probe_media(preview)
            persisted = load_job(job_file)
            preview_ass = pysubs2.load(
                next(
                    artifact.path
                    for artifact in persisted.artifacts
                    if artifact.kind == "preview_ass"
                )
            )

            self.assertTrue(info.has_video)
            self.assertTrue(info.has_audio)
            self.assertLessEqual(abs(info.duration_ms - 1000), 250)
            self.assertEqual(
                {(event.start, event.end) for event in preview_ass.events},
                {(0, 1000)},
            )
            self.assertIn("preview_ass", {artifact.kind for artifact in persisted.artifacts})
            self.assertIn("preview_video", {artifact.kind for artifact in persisted.artifacts})


if __name__ == "__main__":
    unittest.main()
