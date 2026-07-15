from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import TextIO
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from xvt.events import EventSink, create_event

_REDACTED = "<redacted>"
_SENSITIVE_OPTIONS = {
    "--cookies",
    "--cookies-from-browser",
    "--password",
    "--proxy",
    "--username",
    "--video-password",
}
_SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization", "x-api-key"}
_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "authorization",
    "key",
    "signature",
    "sig",
    "token",
    "x-amz-credential",
    "x-amz-security-token",
    "x-amz-signature",
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        super().__init__(f"Command failed with exit code {result.returncode}: {detail}")


def _run_streaming_command(
    command: tuple[str, ...],
    *,
    cwd: str | Path | None,
    timeout: float | None,
    line_callback: Callable[[str, str], None],
) -> CommandResult:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    lines: Queue[tuple[str, str]] = Queue()
    output = {"stdout": [], "stderr": []}

    def read_stream(name: str, stream: TextIO) -> None:
        for line in stream:
            lines.put((name, line))
        stream.close()

    threads = [
        Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout if timeout is not None else None
    timed_out = False
    while process.poll() is None:
        if deadline is not None and time.monotonic() >= deadline:
            process.kill()
            process.wait()
            timed_out = True
            break
        try:
            name, line = lines.get(timeout=0.1)
        except Empty:
            continue
        output[name].append(line)
        line_callback(name, line.rstrip("\r\n"))

    for thread in threads:
        thread.join()
    while not lines.empty():
        name, line = lines.get_nowait()
        output[name].append(line)
        line_callback(name, line.rstrip("\r\n"))

    return CommandResult(
        args=command,
        returncode=124 if timed_out else process.returncode,
        stdout="".join(output["stdout"]),
        stderr=(
            "".join(output["stderr"])
            + (f"command timed out after {timeout} seconds" if timed_out else "")
        ),
    )


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return value

    netloc = (
        f"{_REDACTED}@{parsed.netloc.rsplit('@', 1)[1]}"
        if parsed.username is not None
        else parsed.netloc
    )
    query = urlencode(
        [
            (key, _REDACTED if key.lower() in _SENSITIVE_QUERY_KEYS else query_value)
            for key, query_value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def _redact_header(value: str) -> str:
    name, separator, _ = value.partition(":")
    if separator and name.strip().lower() in _SENSITIVE_HEADERS:
        return f"{name}:{_REDACTED}"
    return value


def _redact_command(command: tuple[str, ...]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    redact_header = False
    for argument in command:
        if redact_next:
            redacted.append(_REDACTED)
            redact_next = False
            continue
        if redact_header:
            redacted.append(_redact_header(argument))
            redact_header = False
            continue

        option, separator, _ = argument.partition("=")
        if option in _SENSITIVE_OPTIONS:
            if separator:
                redacted.append(f"{option}={_REDACTED}")
            else:
                redacted.append(argument)
                redact_next = True
            continue
        if option == "--add-header":
            if separator:
                redacted.append(f"{option}={_redact_header(argument.partition('=')[2])}")
            else:
                redacted.append(argument)
                redact_header = True
            continue
        redacted.append(_redact_url(argument))
    return redacted


def run_command(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = None,
    event_sink: EventSink | None = None,
    line_callback: Callable[[str, str], None] | None = None,
) -> CommandResult:
    command = tuple(str(arg) for arg in args)
    if not command:
        raise ValueError("command must not be empty")

    event_command = _redact_command(command)

    if event_sink:
        event_sink(create_event("command_started", command=event_command))

    try:
        if line_callback is not None:
            result = _run_streaming_command(
                command,
                cwd=cwd,
                timeout=timeout,
                line_callback=line_callback,
            )
        else:
            completed = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                check=False,
                text=True,
                timeout=timeout,
            )
            result = CommandResult(
                args=command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
    except FileNotFoundError as error:
        result = CommandResult(command, 127, "", str(error))
        if event_sink:
            event_sink(create_event("command_failed", command=event_command, exit_code=127))
        raise CommandError(result) from error

    if result.returncode != 0:
        if event_sink:
            event_sink(
                create_event(
                    "command_failed",
                    command=event_command,
                    exit_code=result.returncode,
                )
            )
        raise CommandError(result)

    if event_sink:
        event_sink(create_event("command_completed", command=event_command, exit_code=0))
    return result
