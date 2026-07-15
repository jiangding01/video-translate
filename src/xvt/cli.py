import argparse
import json
import sys
from collections.abc import Sequence

from xvt import __version__
from xvt.doctor import run_doctor
from xvt.events import create_event
from xvt.job_store import get_job_file, load_job
from xvt.pipeline import (
    apply_agent_translation,
    apply_subtitle_translation,
    burn_translated_job,
    inspect_input,
    prepare_agent_translation,
    resume_job,
    run_translation,
)
from xvt.process import CommandError
from xvt.recovery import verify_job
from xvt.workflow import WorkflowOptions, continue_workflow, run_workflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xvt",
        description="Local-first video transcription and subtitle translation",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    inspect_parser = subparsers.add_parser("inspect", help="Inspect local media or a remote URL")
    inspect_parser.add_argument("input")
    inspect_parser.add_argument("--json", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="Check runtime dependencies")
    doctor_parser.add_argument("--output")
    doctor_parser.add_argument("--json", action="store_true")

    run_parser = subparsers.add_parser("run", help="Transcribe media or import source subtitles")
    run_parser.add_argument("input")
    run_parser.add_argument("--output", required=True)
    run_parser.add_argument("--engine", choices=["mlx", "faster"], default="mlx")
    run_parser.add_argument("--language")
    run_parser.add_argument("--subtitles")
    run_parser.add_argument("--align-subtitles", action="store_true")
    run_parser.add_argument("--min-alignment-confidence", type=float, default=0.55)
    run_parser.add_argument("--allow-low-confidence-alignment", action="store_true")
    run_parser.add_argument("--cookies-from-browser")
    run_parser.add_argument("--proxy")
    run_parser.add_argument("--youtube-subtitles", action="store_true")
    run_parser.add_argument("--subtitle-language")
    run_parser.add_argument("--allow-auto-subtitles", action="store_true")
    run_parser.add_argument("--translation-subtitles")
    run_parser.add_argument("--target-language")
    run_parser.add_argument(
        "--provider",
        choices=["auto", "openai-compatible", "agent"],
        default="auto",
    )
    run_parser.add_argument("--base-url")
    run_parser.add_argument("--model")
    run_parser.add_argument("--api-key-env", default="XVT_TRANSLATION_API_KEY")
    run_parser.add_argument("--batch-size", type=int, default=40)
    run_parser.add_argument("--max-retries", type=int, default=2)
    run_parser.add_argument("--timeout-seconds", type=float, default=60)
    run_parser.add_argument("--burn", action="store_true")
    run_parser.add_argument("--burn-media")
    run_parser.add_argument("--burn-output")
    run_parser.add_argument("--software", action="store_true")
    run_parser.add_argument("--target-font")
    run_parser.add_argument("--source-font")
    run_parser.add_argument("--events-jsonl", action="store_true")

    status_parser = subparsers.add_parser("status", help="Read a persisted job file")
    status_parser.add_argument("job_file")
    status_parser.add_argument("--json", action="store_true")

    verify_parser = subparsers.add_parser("verify", help="Verify persisted job artifacts")
    verify_parser.add_argument("job_file")
    verify_parser.add_argument("--json", action="store_true")

    resume_parser = subparsers.add_parser("resume", help="Resume from valid job checkpoints")
    resume_parser.add_argument("job_file")
    resume_parser.add_argument("--events-jsonl", action="store_true")

    continue_parser = subparsers.add_parser(
        "continue", help="Continue a persisted end-to-end workflow goal"
    )
    continue_parser.add_argument("job_file")
    continue_parser.add_argument("--cookies-from-browser")
    continue_parser.add_argument("--proxy")
    continue_parser.add_argument("--events-jsonl", action="store_true")

    prepare_parser = subparsers.add_parser(
        "prepare-translation",
        help="Create deterministic translation batches for an agent",
    )
    prepare_parser.add_argument("job_file")
    prepare_parser.add_argument("--source-language", required=True)
    prepare_parser.add_argument("--target-language", required=True)
    prepare_parser.add_argument("--batch-size", type=int, default=40)
    prepare_parser.add_argument("--events-jsonl", action="store_true")

    apply_parser = subparsers.add_parser(
        "apply-translation",
        help="Validate translated cue IDs and export translated subtitles",
    )
    apply_parser.add_argument("job_file")
    apply_parser.add_argument("response_file")
    apply_parser.add_argument("--target-language", required=True)
    apply_parser.add_argument("--events-jsonl", action="store_true")

    import_translation_parser = subparsers.add_parser(
        "import-translation",
        help="Merge an existing translated subtitle file onto source cue timing",
    )
    import_translation_parser.add_argument("job_file")
    import_translation_parser.add_argument("subtitle_file")
    import_translation_parser.add_argument("--target-language", required=True)
    import_translation_parser.add_argument("--events-jsonl", action="store_true")

    translate_parser = subparsers.add_parser(
        "translate",
        help="Translate a job with a compatible API or prepare Agent batches",
    )
    translate_parser.add_argument("job_file")
    translate_parser.add_argument("--target-language", required=True)
    translate_parser.add_argument(
        "--provider",
        choices=["auto", "openai-compatible", "agent"],
        default="auto",
    )
    translate_parser.add_argument("--base-url")
    translate_parser.add_argument("--model")
    translate_parser.add_argument("--api-key-env", default="XVT_TRANSLATION_API_KEY")
    translate_parser.add_argument("--batch-size", type=int, default=40)
    translate_parser.add_argument("--max-retries", type=int, default=2)
    translate_parser.add_argument("--timeout-seconds", type=float, default=60)
    translate_parser.add_argument("--events-jsonl", action="store_true")

    burn_parser = subparsers.add_parser("burn", help="Burn bilingual ASS into a video")
    burn_parser.add_argument("job_file")
    burn_parser.add_argument("--media")
    burn_parser.add_argument("--output")
    burn_parser.add_argument("--software", action="store_true")
    burn_parser.add_argument("--cookies-from-browser")
    burn_parser.add_argument("--proxy")
    burn_parser.add_argument("--target-font")
    burn_parser.add_argument("--source-font")
    burn_parser.add_argument("--events-jsonl", action="store_true")
    return parser


def _print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _event_sink(arguments: argparse.Namespace):
    if not getattr(arguments, "events_jsonl", False):
        return None
    return lambda event: print(event.to_json(), flush=True)


def _complete(arguments: argparse.Namespace, **data: object) -> None:
    event_sink = _event_sink(arguments)
    if event_sink:
        event_sink(create_event("cli_completed", command=arguments.command, **data))


def _next_action(job) -> str:
    job_file = get_job_file(job)
    workflow = job.config.get("workflow")
    if isinstance(workflow, dict):
        target_language = workflow.get("target_language") or "LANGUAGE"
        if job.stages["translate"].status.value == "awaiting_input":
            return (
                f'xvt apply-translation "{job_file}" RESPONSE '
                f'--target-language {target_language} && xvt continue "{job_file}"'
            )
        artifact_kinds = {artifact.kind for artifact in job.artifacts}
        if "source_cues" not in artifact_kinds:
            return f'xvt continue "{job_file}"'
        if target_language != "LANGUAGE" and "translated_cues" not in artifact_kinds:
            return f'xvt continue "{job_file}"'
        if workflow.get("burn") and "burned_video" not in artifact_kinds:
            return f'xvt continue "{job_file}"'
        return f'xvt verify "{job_file}"'
    failed_stages = [name for name, record in job.stages.items() if record.status.value == "failed"]
    if failed_stages:
        if failed_stages[-1] in {"burn", "media_qa"}:
            return f'xvt burn "{job_file}"'
        if failed_stages[-1] in {"translate", "subtitle_qa", "render"}:
            return f'xvt translate "{job_file}" --target-language LANGUAGE'
        return f'xvt resume "{job_file}"'
    artifact_kinds = {artifact.kind for artifact in job.artifacts}
    if job.stages["translate"].status.value == "awaiting_input":
        return f'xvt apply-translation "{job_file}" RESPONSE --target-language LANGUAGE'
    if "translated_cues" not in artifact_kinds:
        return f'xvt translate "{job_file}" --target-language LANGUAGE'
    if "burned_video" not in artifact_kinds:
        return f'xvt burn "{job_file}"'
    return f'xvt verify "{job_file}"'


def _run_command(arguments: argparse.Namespace) -> int:
    if arguments.command == "inspect":
        result = inspect_input(arguments.input)
        if arguments.json:
            _print_json(result)
        else:
            print(f"{result['kind']}: {result.get('title') or result.get('path')}")
        return 0

    if arguments.command == "doctor":
        report = run_doctor(arguments.output)
        if arguments.json:
            _print_json(report)
        else:
            for check in report["checks"]:
                print(f"{check['status'].upper():4} {check['name']}: {check['message']}")
            print(f"overall: {report['status']}")
        return 0 if report["status"] == "pass" else 3

    if arguments.command == "run":
        event_sink = _event_sink(arguments)
        result = run_workflow(
            WorkflowOptions(
                input=arguments.input,
                output=arguments.output,
                engine=arguments.engine,
                language=arguments.language,
                subtitles=arguments.subtitles,
                align_subtitles=arguments.align_subtitles,
                minimum_alignment_confidence=arguments.min_alignment_confidence,
                allow_low_confidence_alignment=arguments.allow_low_confidence_alignment,
                cookies_from_browser=arguments.cookies_from_browser,
                proxy=arguments.proxy,
                youtube_subtitles=arguments.youtube_subtitles,
                subtitle_language=arguments.subtitle_language,
                allow_auto_subtitles=arguments.allow_auto_subtitles,
                translation_subtitles=arguments.translation_subtitles,
                target_language=arguments.target_language,
                provider=arguments.provider,
                base_url=arguments.base_url,
                model=arguments.model,
                api_key_env=arguments.api_key_env,
                batch_size=arguments.batch_size,
                max_retries=arguments.max_retries,
                timeout_seconds=arguments.timeout_seconds,
                burn=arguments.burn,
                burn_media=arguments.burn_media,
                burn_output=arguments.burn_output,
                hardware_encoding=not arguments.software,
                target_font=arguments.target_font,
                source_font=arguments.source_font,
            ),
            event_sink=event_sink,
        )
        if event_sink and result.status == "completed":
            _complete(
                arguments,
                job_file=str(result.job_file),
                artifacts=[str(path) for path in result.artifacts],
            )
        elif not event_sink:
            print(result.job_file)
            for path in result.artifacts:
                print(path)
            if result.status == "awaiting_input":
                print(f"translation is awaiting input: {result.message}")
        return 0

    if arguments.command == "status":
        job = load_job(arguments.job_file)
        next_action = _next_action(job)
        if arguments.json:
            data = job.to_dict()
            data["next_action"] = next_action
            _print_json(data)
        else:
            stages = ", ".join(f"{name}={record.status}" for name, record in job.stages.items())
            print(f"{job.id}: {stages}")
            print(f"next: {next_action}")
        return 0

    if arguments.command == "verify":
        report = verify_job(arguments.job_file)
        if arguments.json:
            _print_json(report)
        else:
            for artifact in report["artifacts"]:
                print(f"{artifact['status'].upper():4} {artifact['kind']}: {artifact['message']}")
            print(f"overall: {report['status']}")
        return 0 if report["status"] == "pass" else 4

    if arguments.command == "resume":
        event_sink = _event_sink(arguments)
        job = resume_job(arguments.job_file, event_sink=event_sink)
        if event_sink:
            _complete(arguments, job_file=str(get_job_file(job)))
        else:
            print(get_job_file(job))
        return 0

    if arguments.command == "continue":
        event_sink = _event_sink(arguments)
        result = continue_workflow(
            arguments.job_file,
            cookies_from_browser=arguments.cookies_from_browser,
            proxy=arguments.proxy,
            event_sink=event_sink,
        )
        if event_sink and result.status == "completed":
            _complete(
                arguments,
                job_file=str(result.job_file),
                artifacts=[str(path) for path in result.artifacts],
            )
        elif not event_sink:
            print(result.job_file)
            for path in result.artifacts:
                print(path)
            if result.status == "awaiting_input":
                print(f"translation is awaiting input: {result.message}")
        return 0

    if arguments.command == "prepare-translation":
        event_sink = _event_sink(arguments)
        path = prepare_agent_translation(
            arguments.job_file,
            source_language=arguments.source_language,
            target_language=arguments.target_language,
            batch_size=arguments.batch_size,
            event_sink=event_sink,
        )
        if event_sink:
            _complete(arguments, artifacts=[str(path)])
        else:
            print(path)
        return 0

    if arguments.command == "apply-translation":
        event_sink = _event_sink(arguments)
        paths = apply_agent_translation(
            arguments.job_file,
            arguments.response_file,
            target_language=arguments.target_language,
            event_sink=event_sink,
        )
        if event_sink:
            _complete(arguments, artifacts=[str(path) for path in paths])
        else:
            for path in paths:
                print(path)
        return 0

    if arguments.command == "import-translation":
        event_sink = _event_sink(arguments)
        paths = apply_subtitle_translation(
            arguments.job_file,
            arguments.subtitle_file,
            target_language=arguments.target_language,
            event_sink=event_sink,
        )
        if event_sink:
            _complete(arguments, artifacts=[str(path) for path in paths])
        else:
            for path in paths:
                print(path)
        return 0

    if arguments.command == "translate":
        event_sink = _event_sink(arguments)
        result = run_translation(
            arguments.job_file,
            target_language=arguments.target_language,
            provider=arguments.provider,
            base_url=arguments.base_url,
            model=arguments.model,
            api_key_env=arguments.api_key_env,
            batch_size=arguments.batch_size,
            max_retries=arguments.max_retries,
            timeout_seconds=arguments.timeout_seconds,
            event_sink=event_sink,
        )
        if event_sink:
            _complete(
                arguments,
                mode=result.mode,
                artifacts=[str(path) for path in result.paths],
            )
        else:
            if result.fallback_reason:
                print(
                    f"translation provider unavailable ({result.fallback_reason}); using Agent mode"
                )
            print(f"mode: {result.mode}")
            for path in result.paths:
                print(path)
        return 0

    if arguments.command == "burn":
        event_sink = _event_sink(arguments)
        path = burn_translated_job(
            arguments.job_file,
            media=arguments.media,
            output=arguments.output,
            hardware_encoding=not arguments.software,
            cookies_from_browser=arguments.cookies_from_browser,
            proxy=arguments.proxy,
            target_font=arguments.target_font,
            source_font=arguments.source_font,
            event_sink=event_sink,
        )
        if event_sink:
            _complete(arguments, artifacts=[str(path)])
        else:
            print(path)
        return 0

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        parser.print_help()
        return 0

    parsed = parser.parse_args(arguments)
    try:
        return _run_command(parsed)
    except (OSError, RuntimeError, ValueError) as error:
        if isinstance(error, ValueError):
            category, return_code = "invalid_input", 2
        elif isinstance(error, (CommandError, OSError)):
            category, return_code = "dependency_or_process", 3
        else:
            category, return_code = "workflow", 4
        if getattr(parsed, "events_jsonl", False):
            print(
                create_event(
                    "cli_failed",
                    command=parsed.command,
                    category=category,
                    message=str(error),
                ).to_json(),
                flush=True,
            )
        print(f"error: {error}", file=sys.stderr)
        return return_code


if __name__ == "__main__":
    raise SystemExit(main())
