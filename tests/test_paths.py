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


if __name__ == "__main__":
    unittest.main()
