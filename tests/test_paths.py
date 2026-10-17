"""输出路径安全 helper 的单元测试。"""

import os
import tempfile
import unittest
from pathlib import Path

from xvt.errors import UserInputError
from xvt.job_store import create_job, get_job_file, save_job
from xvt.models import Artifact
from xvt.paths import (
    assert_safe_output,
    paths_collide,
    protected_job_paths,
    resolve_absolute_output,
)
from xvt.recovery import file_checksum


class PathsSafetyTest(unittest.TestCase):
    def test_resolve_absolute_output_rejects_relative_path(self) -> None:
        with self.assertRaisesRegex(UserInputError, "absolute"):
            resolve_absolute_output("relative/out.mp4", label="burn output")

    def test_paths_collide_detects_same_resolve_and_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.mp4"
            original.write_bytes(b"video")
            alias = root / "alias.mp4"
            os.link(original, alias)
            other = root / "other.mp4"
            other.write_bytes(b"other")

            self.assertTrue(paths_collide(original, alias))
            self.assertTrue(paths_collide(original, original.resolve()))
            self.assertFalse(paths_collide(original, other))

    def test_assert_safe_output_rejects_protected_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "source.mp4"
            protected.write_bytes(b"video")
            with self.assertRaisesRegex(UserInputError, "source media"):
                assert_safe_output(
                    protected,
                    protected=[protected],
                    label="burn output",
                )

    def test_protected_job_paths_skips_writing_kind_and_keeps_others(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "input.mp4"
            media.write_bytes(b"video")
            job = create_job(str(media), root)
            artifacts = root / "jobs" / job.id / "artifacts"
            artifacts.mkdir()
            burned = artifacts / "burned.mp4"
            burned.write_bytes(b"burned")
            source_srt = artifacts / "source.srt"
            source_srt.write_text("subtitle", encoding="utf-8")
            job.artifacts.extend(
                [
                    Artifact("burned_video", str(burned), file_checksum(burned)),
                    Artifact("source_srt", str(source_srt), file_checksum(source_srt)),
                ]
            )
            save_job(job)

            protected = protected_job_paths(job, media, writing_kind="burned_video")
            protected_resolved = {path.resolve() for path in protected}

            self.assertIn(media.resolve(), protected_resolved)
            self.assertIn(get_job_file(job).resolve(), protected_resolved)
            self.assertIn(source_srt.resolve(), protected_resolved)
            self.assertNotIn(burned.resolve(), protected_resolved)

            assert_safe_output(
                burned,
                protected=protected,
                label="burn output",
            )
            with self.assertRaisesRegex(UserInputError, "existing artifact"):
                assert_safe_output(
                    source_srt,
                    protected=protected,
                    label="burn output",
                )

    def test_protected_job_paths_covers_local_job_input_and_user_subtitles(self) -> None:
        """O4：Job 记着的用户原始输入文件不得被 ``ffmpeg -y`` 静默覆盖。

        纯音频 Job 的 ``xvt burn JOB --media video.mp4`` 里，受保护的「源媒体」是
        ``--media`` 给的视频，而 ``job.input`` 指向的原始音频既不是 ``media_path``
        也不是已登记 artifact（``input_subtitles`` 之类登记的是 Job 目录内的副本），
        于是把 ``--output`` 指到原始音频上会直接丢失用户数据。用户传入的原始字幕
        （``job.config["subtitles"]`` 与 ``workflow.translation_subtitles``）同理。
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "input.m4a"
            audio.write_bytes(b"audio")
            source_subtitles = root / "source.srt"
            source_subtitles.write_text("source", encoding="utf-8")
            translation_subtitles = root / "translated.srt"
            translation_subtitles.write_text("translated", encoding="utf-8")
            media = root / "video.mp4"
            media.write_bytes(b"video")
            job = create_job(
                str(audio),
                root,
                {
                    "subtitles": str(source_subtitles),
                    "workflow": {
                        "target_language": "zh-CN",
                        "burn": True,
                        "burn_media": str(media),
                        "translation_subtitles": str(translation_subtitles),
                    },
                },
            )
            save_job(job)

            protected = protected_job_paths(job, media, writing_kind="burned_video")
            protected_resolved = {path.resolve() for path in protected}

            self.assertIn(audio.resolve(), protected_resolved)
            self.assertIn(source_subtitles.resolve(), protected_resolved)
            self.assertIn(translation_subtitles.resolve(), protected_resolved)
            for user_file in (audio, source_subtitles, translation_subtitles):
                with self.assertRaisesRegex(UserInputError, "must not overwrite"):
                    assert_safe_output(user_file, protected=protected, label="burn output")

    def test_protected_job_paths_ignores_remote_job_input(self) -> None:
        """远程 Job 的 ``job.input`` 是 URL，不能被当成本地路径塞进保护集。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "video.mp4"
            media.write_bytes(b"video")
            job = create_job("https://example.com/watch?v=abc", root)
            save_job(job)

            protected = protected_job_paths(job, media, writing_kind="burned_video")

            self.assertNotIn(
                "https://example.com/watch?v=abc",
                {str(path) for path in protected},
            )
            # URL 不应把任意输出路径变成「冲突」，正常输出仍要放行。
            assert_safe_output(root / "burned.mp4", protected=protected, label="burn output")


if __name__ == "__main__":
    unittest.main()
