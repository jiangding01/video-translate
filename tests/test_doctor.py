import os
import tempfile
import time
import unittest
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _Usage:
    """``shutil.disk_usage`` 的替身：余量检查只读 ``free``。"""

    free: int


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
                # 调用顺序：1=libass `-filters`、2-5=zh/ja/ko/en 四次字体渲染、
                # 6=h264_videotoolbox、7=libx264。
                side_effect=[
                    capability,
                    font_failure,
                    capability,
                    capability,
                    capability,
                    capability,
                    capability,
                ],
            ),
            patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
        ):
            report = run_doctor()

        font_check = next(check for check in report["checks"] if check["name"] == "subtitle_font")
        self.assertEqual(font_check["status"], "fail")
        # 失败要点名是哪种语言的字体，否则用户只知道「有问题」不知道装什么。
        self.assertIn("zh-CN", font_check["message"])

    def test_doctor_reports_a_non_chinese_subtitle_font_failure(self) -> None:
        """只探 zh-CN 会让日语用户拿到全绿的 doctor 和一屏豆腐块。"""

        capability = _capability_result()
        font_failure = CommandError(
            CommandResult(args=(), returncode=1, stdout="", stderr="failed to find any fallback")
        )
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch(
                "xvt.doctor.run_command",
                # 第 3 次调用是 ja 那一探。
                side_effect=[
                    capability,
                    capability,
                    font_failure,
                    capability,
                    capability,
                    capability,
                    capability,
                ],
            ),
            patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
        ):
            report = run_doctor()

        font_check = next(check for check in report["checks"] if check["name"] == "subtitle_font")
        self.assertEqual(font_check["status"], "fail")
        self.assertIn("ja", font_check["message"])
        self.assertNotIn("zh-CN", font_check["message"])

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
                # 调用顺序：1=libass `-filters`、2-5=四次字体渲染、6=h264、7=libx264。
                side_effect=[
                    capability,
                    _timeout_error(),
                    capability,
                    capability,
                    capability,
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

    def test_doctor_always_passes_a_timeout_to_run_command(self) -> None:
        # 防回归：任何一处 ffmpeg 调用漏掉 timeout，doctor 都可能无限期挂住。
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("xvt.doctor.run_command", return_value=_capability_result()) as run_command_mock,
            patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
        ):
            run_doctor()

        # 1=libass `-filters`、2-5=zh/ja/ko/en 四次字体渲染、6=h264、7=libx264。
        self.assertEqual(run_command_mock.call_count, 7)
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

    def test_missing_hardware_encoder_is_only_a_warning(self) -> None:
        # h264_videotoolbox 是 Apple 独有的，非 macOS 上必然缺失；burn.py 会回退软件
        # 编码，所以这是「慢」不是「跑不了」。此前它报 fail，于是 Linux 上整体永远 fail。
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("xvt.doctor.run_command", return_value=_capability_result("subtitles libx264")),
            patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
        ):
            report = run_doctor()

        hardware = next(c for c in report["checks"] if c["name"] == "hardware_encoder")
        self.assertEqual(hardware["status"], "warn")
        self.assertEqual(report["status"], "warn")
        # 软件编码器仍在，所以没有任何一项是 fail。
        self.assertEqual([c["name"] for c in report["checks"] if c["status"] == "fail"], [])

    def test_fail_wins_over_warn(self) -> None:
        # ASR 缺失（fail）与硬件编码器缺失（warn）同时出现：整体必须是 fail，不被
        # 「其余只是建议」冲淡。
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("xvt.doctor.run_command", return_value=_capability_result("subtitles libx264")),
            patch("xvt.doctor.importlib.util.find_spec", return_value=None),
        ):
            report = run_doctor()

        statuses = {c["name"]: c["status"] for c in report["checks"]}
        self.assertEqual(statuses["hardware_encoder"], "warn")
        self.assertEqual(statuses["asr"], "fail")
        self.assertEqual(report["status"], "fail")

    def test_timeout_still_fails_even_for_a_warn_capability(self) -> None:
        # missing_status 只覆盖「ffmpeg 正常返回但没有这个 token」。超时是判不出来，
        # 不能因为这个能力可以降级就说成没事。
        with (
            patch("xvt.doctor.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("xvt.doctor.run_command", side_effect=_timeout_error()),
        ):
            result = _ffmpeg_capability(
                ["-encoders"], "h264_videotoolbox", "hardware_encoder", missing_status="warn"
            )

        self.assertEqual(result["status"], "fail")
        self.assertIn("timed out", result["message"])

    def _output_report(self, free_bytes):
        """带 --output 跑一次 doctor；``free_bytes`` 传 OSError 则模拟拿不到可用空间。"""

        usage = (
            patch("xvt.doctor.shutil.disk_usage", side_effect=free_bytes)
            if isinstance(free_bytes, OSError)
            else patch("xvt.doctor.shutil.disk_usage", return_value=_Usage(free_bytes))
        )
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
                patch("xvt.doctor.run_command", return_value=_capability_result()),
                patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
                usage,
            ):
                return run_doctor(Path(directory) / "jobs")

    def test_low_free_space_is_a_warning_not_a_failure(self) -> None:
        report = self._output_report(512 * 2**20)

        space = next(c for c in report["checks"] if c["name"] == "output_free_space")
        self.assertEqual(space["status"], "warn")
        self.assertIn("0.5 GiB", space["message"])
        self.assertEqual(report["status"], "warn")
        self.assertEqual([c["name"] for c in report["checks"] if c["status"] == "fail"], [])

    def test_ample_free_space_passes(self) -> None:
        report = self._output_report(100 * 2**30)

        space = next(c for c in report["checks"] if c["name"] == "output_free_space")
        self.assertEqual(space["status"], "pass")
        self.assertEqual(report["status"], "pass")

    def test_exactly_the_threshold_passes(self) -> None:
        report = self._output_report(2 * 2**30)

        space = next(c for c in report["checks"] if c["name"] == "output_free_space")
        self.assertEqual(space["status"], "pass")

    def test_unreadable_disk_usage_omits_the_check(self) -> None:
        # 拿不到可用空间是「判不出来」，不是「空间不足」：这一项整个不出现，也不 warn。
        report = self._output_report(OSError("network volume"))

        self.assertEqual([c for c in report["checks"] if c["name"] == "output_free_space"], [])
        self.assertEqual(report["status"], "pass")

    def test_free_space_check_is_absent_without_output(self) -> None:
        # 「不该红时不红」：不给 --output 时检查项不变，新项不会凭空出现。
        with (
            patch("xvt.doctor.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
            patch("xvt.doctor.run_command", return_value=_capability_result()),
            patch("xvt.doctor.importlib.util.find_spec", return_value=object()),
        ):
            report = run_doctor()

        names = [c["name"] for c in report["checks"]]
        self.assertNotIn("output_free_space", names)
        self.assertNotIn("output", names)
        self.assertEqual(report["status"], "pass")

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
