import json
import sys
import unittest

from xvt.events import EventType, create_event
from xvt.process import CommandError, run_command


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
