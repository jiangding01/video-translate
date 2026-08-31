import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from xvt.doctor import _ffmpeg_capability, run_doctor
from xvt.process import CommandError, CommandResult


def _assert_process_dead(test: unittest.TestCase, pid: int, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        # macOS 上刚被 SIGKILL 的进程可能短暂处于僵尸态，os.kill(pid, 0) 仍会成功；
        # 用 waitpid 无法回收非子进程，只能轮询等待其消失。
        time.sleep(0.05)
    test.fail(f"process {pid} is still alive")


def _capability_result(stdout: str = "subtitles libx264 h264_videotoolbox") -> CommandResult:
    return CommandResult(args=(), returncode=0, stdout=stdout, stderr="")


def _timeout_error() -> CommandError:
    return CommandError(
        CommandResult(args=(), returncode=124, stdout="", stderr="command timed out after 30s")
    )


class DoctorTest(unittest.TestCase):
    def test_doctor_passes_when_dependencies_are_available(self) -> None:
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("xvt.doctor.run_command", return_value=_capability_result()),
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
        capability = _capability_result()
        font_failure = CommandError(
            CommandResult(args=(), returncode=1, stdout="", stderr="Error opening font")
        )
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch(
                "xvt.doctor.run_command",
                # 调用顺序：1=libass `-filters`、2=字体渲染、3=h264_videotoolbox、4=libx264。
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
            patch("xvt.doctor.run_command", side_effect=_timeout_error()),
            patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
        ):
            report = run_doctor()

        self.assertEqual(report["status"], "fail")
        libass_check = next(check for check in report["checks"] if check["name"] == "libass")
        self.assertEqual(libass_check["status"], "fail")
        self.assertIn("timed out", libass_check["message"])

    def test_doctor_marks_font_check_failed_on_timeout(self) -> None:
        capability = _capability_result()
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch(
                "xvt.doctor.run_command",
                # 调用顺序：1=libass `-filters`、2=字体渲染、3=h264_videotoolbox、4=libx264。
                side_effect=[capability, _timeout_error(), capability, capability],
            ),
            patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
        ):
            report = run_doctor()

        self.assertEqual(report["status"], "fail")
        font_check = next(check for check in report["checks"] if check["name"] == "subtitle_font")
        self.assertEqual(font_check["status"], "fail")
        self.assertIn("timed out", font_check["message"])

    def test_doctor_always_passes_a_timeout_to_run_command(self) -> None:
        # 防回归：任何一处 ffmpeg 调用漏掉 timeout，doctor 都可能无限期挂住。
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("xvt.doctor.run_command", return_value=_capability_result()) as run_command_mock,
            patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
        ):
            run_doctor()

        self.assertEqual(run_command_mock.call_count, 4)
        self.assertTrue(all(call.kwargs.get("timeout") for call in run_command_mock.call_args_list))

    def test_doctor_checks_output_parent_without_creating_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new" / "jobs"
            with (
                patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
                patch("xvt.doctor.run_command", return_value=_capability_result()),
                patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
            ):
                report = run_doctor(output)

            output_check = next(check for check in report["checks"] if check["name"] == "output")
            self.assertEqual(output_check["status"], "pass")
            self.assertFalse(output.exists())

    def test_ffmpeg_capability_timeout_kills_grandchild_process_group(self) -> None:
        # 反回归：doctor 的 ffmpeg 探测必须像 run_command 一样以进程组方式清理超时
        # 命令，否则 PATH 里的 wrapper 脚本派生的孙进程（如 yt-dlp 派生 ffmpeg）会
        # 被遗留成孤儿。这里让伪 ffmpeg 派生一个孙进程并把 pid 写到文件，超时后
        # 校验孙进程确实被清理掉。
        # 伪 ffmpeg 必须用 shell 而非 python：超时时钟从 Popen 返回就开始计时，
        # python 解释器冷启动在高负载下可能耗尽整个超时窗口，孙进程还没派生就被
        # 整组清理，pid 文件永远不会出现（历史 flake）。sh 派生 + 写 pid 是毫秒级，
        # 配合 3 秒窗口余量充足。
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "grandchild.pid"
            script = Path(directory) / "fake-ffmpeg"
            script.write_text(f'#!/bin/sh\nsleep 60 &\necho $! > "{pid_file}"\nwait\n')
            script.chmod(0o755)

            with (
                patch("xvt.doctor.shutil.which", return_value=str(script)),
                patch("xvt.doctor._FFMPEG_CAPABILITY_TIMEOUT_SECONDS", 3.0),
            ):
                started = time.monotonic()
                result = _ffmpeg_capability(["-filters"], "subtitles", "libass")

            self.assertLess(time.monotonic() - started, 30)
            self.assertEqual(result["status"], "fail")
            self.assertIn("timed out", result["message"])

            deadline = time.monotonic() + 5.0
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "grandchild pid was never written")
            grandchild_pid = int(pid_file.read_text())
            _assert_process_dead(self, grandchild_pid)


if __name__ == "__main__":
    unittest.main()
