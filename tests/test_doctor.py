import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.doctor import run_doctor


class DoctorTest(unittest.TestCase):
    def test_doctor_passes_when_dependencies_are_available(self) -> None:
        completed = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "subtitles libx264 h264_videotoolbox", "stderr": ""},
        )()
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("xvt.doctor.subprocess.run", return_value=completed),
            patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
        ):
            report = run_doctor()

        self.assertEqual(report["status"], "pass")
        self.assertTrue(all(check["status"] == "pass" for check in report["checks"]))

    def test_doctor_reports_missing_required_dependency(self) -> None:
        with (
            patch("xvt.doctor.shutil.which", return_value=None),
            patch("xvt.doctor.importlib.util.find_spec", return_value=None),
        ):
            report = run_doctor()

        self.assertEqual(report["status"], "fail")
        self.assertIn(
            "ffmpeg", {check["name"] for check in report["checks"] if check["status"] == "fail"}
        )

    def test_doctor_reports_default_subtitle_font_failure(self) -> None:
        capability = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "subtitles libx264 h264_videotoolbox", "stderr": ""},
        )()
        font_failure = type(
            "Completed",
            (),
            {"returncode": 1, "stdout": "", "stderr": "Error opening font"},
        )()
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch(
                "xvt.doctor.subprocess.run",
                side_effect=[capability, font_failure, capability, capability],
            ),
            patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
        ):
            report = run_doctor()

        font_check = next(check for check in report["checks"] if check["name"] == "subtitle_font")
        self.assertEqual(font_check["status"], "fail")

    def test_doctor_marks_capability_check_failed_on_timeout(self) -> None:
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch(
                "xvt.doctor.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=30),
            ),
            patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
        ):
            report = run_doctor()

        self.assertEqual(report["status"], "fail")
        libass_check = next(check for check in report["checks"] if check["name"] == "libass")
        self.assertEqual(libass_check["status"], "fail")
        self.assertIn("timed out", libass_check["message"])

    def test_doctor_marks_font_check_failed_on_timeout(self) -> None:
        capability = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "subtitles libx264 h264_videotoolbox", "stderr": ""},
        )()
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch(
                "xvt.doctor.subprocess.run",
                # 调用顺序：1=libass `-filters`、2=字体渲染、3=h264_videotoolbox、4=libx264。
                side_effect=[
                    capability,
                    subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=60),
                    capability,
                    capability,
                ],
            ),
            patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
        ):
            report = run_doctor()

        self.assertEqual(report["status"], "fail")
        font_check = next(check for check in report["checks"] if check["name"] == "subtitle_font")
        self.assertEqual(font_check["status"], "fail")
        self.assertIn("timed out", font_check["message"])

    def test_doctor_always_passes_a_timeout_to_subprocess(self) -> None:
        # 防回归：任何一处 ffmpeg 调用漏掉 timeout，doctor 都可能无限期挂住。
        completed = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "subtitles libx264 h264_videotoolbox", "stderr": ""},
        )()
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("xvt.doctor.subprocess.run", return_value=completed) as run_mock,
            patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
        ):
            run_doctor()

        self.assertEqual(run_mock.call_count, 4)
        self.assertTrue(all(call.kwargs.get("timeout") for call in run_mock.call_args_list))

    def test_doctor_checks_output_parent_without_creating_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new" / "jobs"
            completed = type(
                "Completed",
                (),
                {"returncode": 0, "stdout": "subtitles libx264 h264_videotoolbox", "stderr": ""},
            )()
            with (
                patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
                patch("xvt.doctor.subprocess.run", return_value=completed),
                patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
            ):
                report = run_doctor(output)

            output_check = next(check for check in report["checks"] if check["name"] == "output")
            self.assertEqual(output_check["status"], "pass")
            self.assertFalse(output.exists())
