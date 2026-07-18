import json
import os
import sys
import threading
import time
import unittest

from xvt.events import EventType, create_event
from xvt.process import CommandCancelled, CommandError, run_command


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


class ProcessTest(unittest.TestCase):
    def test_run_command_returns_output_and_events(self) -> None:
        events = []

        result = run_command(
            [sys.executable, "-c", "print('ready')"],
            event_sink=events.append,
        )

        self.assertEqual(result.stdout, "ready\n")
        self.assertEqual([event.type for event in events], ["command_started", "command_completed"])

    def test_run_command_streams_stdout_and_stderr_lines(self) -> None:
        lines = []
        result = run_command(
            [
                sys.executable,
                "-c",
                "import sys; print('one', flush=True); print('two', file=sys.stderr, flush=True)",
            ],
            line_callback=lambda stream, line: lines.append((stream, line)),
        )

        self.assertEqual(result.stdout, "one\n")
        self.assertEqual(result.stderr, "two\n")
        self.assertCountEqual(lines, [("stdout", "one"), ("stderr", "two")])

    def test_run_command_streaming_timeout_is_structured_error(self) -> None:
        with self.assertRaises(CommandError) as context:
            run_command(
                [sys.executable, "-c", "import time; time.sleep(1)"],
                timeout=0.01,
                line_callback=lambda _stream, _line: None,
            )

        self.assertEqual(context.exception.result.returncode, 124)
        self.assertIn("timed out", context.exception.result.stderr)

    def test_run_command_inactivity_timeout_kills_silent_command(self) -> None:
        started = time.monotonic()
        with self.assertRaises(CommandError) as context:
            run_command(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                inactivity_timeout=0.3,
            )

        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(context.exception.result.returncode, 124)
        self.assertIn("no output", context.exception.result.stderr)

    def test_run_command_activity_resets_inactivity_timer(self) -> None:
        # 总运行时长（约 1.2 秒）超过静默阈值（0.6 秒），但每 0.1 秒都有输出；
        # 只有计时器随活动重置，命令才能成功结束。
        result = run_command(
            [
                sys.executable,
                "-c",
                "import time\nfor i in range(12):\n    print(i, flush=True)\n    time.sleep(0.1)",
            ],
            inactivity_timeout=0.6,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("11", result.stdout)

    def test_run_command_cancel_terminates_process_group(self) -> None:
        cancel = threading.Event()
        pids: list[int] = []

        def capture_pid(_stream: str, line: str) -> None:
            if line.strip().isdigit():
                pids.append(int(line.strip()))
            cancel.set()

        started = time.monotonic()
        with self.assertRaises(CommandCancelled):
            run_command(
                [
                    sys.executable,
                    "-c",
                    "import os, time; print(os.getpid(), flush=True); time.sleep(60)",
                ],
                cancel=cancel,
                line_callback=capture_pid,
            )

        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(len(pids), 1)
        _assert_process_dead(self, pids[0])

    def test_run_command_timeout_kills_grandchild_processes(self) -> None:
        # 子进程再派生孙进程（模拟 yt-dlp 派生 ffmpeg），硬超时后整个进程组都必须被清理。
        child_script = (
            "import subprocess, sys, time\n"
            "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(grandchild.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        grandchild_pids: list[int] = []

        def capture_pid(_stream: str, line: str) -> None:
            if line.strip().isdigit():
                grandchild_pids.append(int(line.strip()))

        with self.assertRaises(CommandError) as context:
            run_command(
                [sys.executable, "-c", child_script],
                timeout=2.0,
                line_callback=capture_pid,
            )

        self.assertEqual(context.exception.result.returncode, 124)
        self.assertEqual(len(grandchild_pids), 1)
        _assert_process_dead(self, grandchild_pids[0])

    def test_run_command_raises_structured_error(self) -> None:
        with self.assertRaises(CommandError) as context:
            run_command([sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"])

        self.assertEqual(context.exception.result.returncode, 3)
        self.assertEqual(context.exception.result.stderr, "bad")

    def test_run_command_redacts_secrets_from_events_only(self) -> None:
        events = []
        proxy = "https://user:password@proxy.example.com:8443"
        signed_url = "https://cdn.example.com/video.mp4?id=42&X-Amz-Signature=secret"
        result = run_command(
            [
                sys.executable,
                "-c",
                "import sys; print('\\n'.join(sys.argv[1:]))",
                "--proxy",
                proxy,
                "--cookies=/tmp/cookies.txt",
                "--add-header",
                "Authorization: Bearer secret",
                signed_url,
            ],
            event_sink=events.append,
        )

        self.assertIn(proxy, result.stdout)
        self.assertIn(signed_url, result.stdout)
        event_command = events[0].data["command"]
        self.assertEqual(event_command[event_command.index("--proxy") + 1], "<redacted>")
        self.assertIn("--cookies=<redacted>", event_command)
        self.assertIn("Authorization:<redacted>", event_command)
        self.assertIn(
            "https://cdn.example.com/video.mp4?id=42&X-Amz-Signature=%3Credacted%3E",
            event_command,
        )
        self.assertNotIn("secret", json.dumps(events[0].data))

    def test_run_command_redacts_equals_header_and_proxy_credentials(self) -> None:
        events = []
        run_command(
            [
                sys.executable,
                "-c",
                "pass",
                "--add-header=Cookie: session=secret",
                "https://user:password@proxy.example.com/path",
            ],
            event_sink=events.append,
        )

        event_command = events[0].data["command"]
        self.assertIn("--add-header=Cookie:<redacted>", event_command)
        self.assertIn("https://<redacted>@proxy.example.com/path", event_command)
        self.assertNotIn("secret", json.dumps(events[0].data))
        self.assertNotIn("password", json.dumps(events[0].data))

    def test_event_serializes_as_json(self) -> None:
        event = create_event(EventType.STAGE_STARTED, stage="inspect")

        data = json.loads(event.to_json())

        self.assertEqual(data["type"], "stage_started")
        self.assertEqual(data["data"], {"stage": "inspect"})

    def test_event_redacts_credentials_and_signed_url_query(self) -> None:
        event = create_event(
            EventType.JOB_STARTED,
            input=("https://user:password@example.com/video.mp4?id=42&X-Amz-Signature=secret"),
        )

        serialized = event.to_json()
        self.assertNotIn("password", serialized)
        self.assertNotIn("secret", serialized)
        self.assertIn("id=42", serialized)
        self.assertIn("redacted", serialized)


if __name__ == "__main__":
    unittest.main()
