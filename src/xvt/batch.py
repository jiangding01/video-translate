from __future__ import annotations

import json
import os
import shlex
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from xvt.events import Event, EventSink, EventType, create_event, error_event_fields
from xvt.job_store import load_job, normalize_output_root, utc_now
from xvt.process import CommandCancelled, raise_if_cancelled
from xvt.recovery import build_recovery_plan
from xvt.subtitles import SUPPORTED_SUBTITLE_EXTENSIONS
from xvt.workflow import WorkflowOptions, continue_workflow, run_workflow

BATCH_SCHEMA_VERSION = 1

SUPPORTED_AUDIO_EXTENSIONS = frozenset(
    {
        ".aac",
        ".flac",
        ".m4a",
        ".mp3",
        ".ogg",
        ".opus",
        ".wav",
        ".wma",
    }
)
SUPPORTED_VIDEO_EXTENSIONS = frozenset(
    {
        ".avi",
        ".flv",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mts",
        ".ts",
        ".webm",
        ".wmv",
    }
)
SUPPORTED_MEDIA_EXTENSIONS = SUPPORTED_AUDIO_EXTENSIONS | SUPPORTED_VIDEO_EXTENSIONS
SUPPORTED_BATCH_EXTENSIONS = SUPPORTED_MEDIA_EXTENSIONS | SUPPORTED_SUBTITLE_EXTENSIONS
_BATCH_ITEM_STATUSES = {
    "pending",
    "running",
    "completed",
    "awaiting_input",
    "failed",
    "interrupted",
}


@dataclass(frozen=True, slots=True)
class BatchResult:
    status: str
    batch_file: Path
    total: int
    completed: int
    awaiting_input: int
    failed: int


def discover_batch_inputs(
    input_directory: str | Path,
    *,
    output_root: str | Path,
    recursive: bool = False,
) -> tuple[Path, ...]:
    directory = Path(input_directory).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError(f"input directory does not exist: {directory}")
    output = normalize_output_root(output_root)
    excluded_roots = (output / "jobs", output / "batches") if output == directory else (output,)
    candidates = directory.rglob("*") if recursive else directory.iterdir()
    inputs: set[Path] = set()
    for path in candidates:
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_BATCH_EXTENSIONS:
            continue
        resolved = path.resolve()
        relative_parts = path.relative_to(directory).parts
        if any(part.startswith(".") for part in relative_parts):
            continue
        if any(root == resolved or root in resolved.parents for root in excluded_roots):
            continue
        inputs.add(resolved)
    return tuple(sorted(inputs, key=lambda path: (str(path).casefold(), str(path))))


def _json_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value.expanduser().resolve())
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _workflow_config(options: WorkflowOptions) -> dict[str, Any]:
    config = asdict(options)
    config.pop("input")
    config.pop("output")
    config["cookies_from_browser"] = None
    config["proxy"] = None
    if options.glossary:
        config["glossary"] = str(Path(options.glossary).expanduser().resolve())
    return _json_value(config)


def _write_batch(batch_file: Path, batch: dict[str, Any]) -> None:
    batch["updated_at"] = utc_now()
    batch["summary"] = _summary(batch)
    temporary = batch_file.with_name(f".{batch_file.name}.tmp")
    temporary.write_text(
        json.dumps(batch, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, batch_file)


def _load_batch(batch_file: str | Path) -> tuple[Path, dict[str, Any]]:
    path = Path(batch_file).expanduser().resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("batch file must contain a JSON object")
    if data.get("schema_version") != BATCH_SCHEMA_VERSION:
        raise ValueError(f"unsupported batch schema version: {data.get('schema_version')}")
    if not isinstance(data.get("items"), list) or not isinstance(data.get("workflow"), dict):
        raise ValueError("batch file is missing items or workflow configuration")
    output_root = data.get("output_root")
    if not isinstance(output_root, str) or not Path(output_root).is_absolute():
        raise ValueError("batch output_root must be an absolute path")
    jobs_root = Path(output_root).resolve() / "jobs"
    for index, item in enumerate(data["items"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"batch item {index} must be a JSON object")
        input_value = item.get("input")
        if not isinstance(input_value, str) or not Path(input_value).is_absolute():
            raise ValueError(f"batch item {index} input must be an absolute path")
        if item.get("status") not in _BATCH_ITEM_STATUSES:
            raise ValueError(f"batch item {index} has an invalid status")
        job_file = item.get("job_file")
        if job_file is not None and (
            not isinstance(job_file, str) or not Path(job_file).is_absolute()
        ):
            raise ValueError(f"batch item {index} job_file must be an absolute path or null")
        if isinstance(job_file, str):
            resolved_job = Path(job_file).resolve()
            if jobs_root not in resolved_job.parents:
                raise ValueError(f"batch item {index} job_file must stay inside output_root/jobs")
    return path, data


def _summary(batch: dict[str, Any]) -> dict[str, int]:
    items = batch.get("items", [])
    statuses = [item.get("status") for item in items if isinstance(item, dict)]
    return {
        "total": len(statuses),
        "completed": statuses.count("completed"),
        "awaiting_input": statuses.count("awaiting_input"),
        "failed": statuses.count("failed") + statuses.count("interrupted"),
        "pending": statuses.count("pending") + statuses.count("running"),
    }


def _result_status(summary: dict[str, int]) -> str:
    if summary["failed"] or summary["pending"]:
        return "partial_failure"
    if summary["awaiting_input"]:
        return "awaiting_input"
    return "completed"


def _result(batch_file: Path, batch: dict[str, Any]) -> BatchResult:
    summary = _summary(batch)
    return BatchResult(
        status=_result_status(summary),
        batch_file=batch_file,
        total=summary["total"],
        completed=summary["completed"],
        awaiting_input=summary["awaiting_input"],
        failed=summary["failed"],
    )


def _record_recovery(item: dict[str, Any]) -> str | None:
    job_file = item.get("job_file")
    if not isinstance(job_file, str):
        item["recovery_plan"] = None
        return None
    plan = build_recovery_plan(job_file)
    item["recovery_plan"] = plan.to_dict()
    return plan.category


def _record_artifacts(item: dict[str, Any]) -> None:
    job_file = item.get("job_file")
    plan = item.get("recovery_plan")
    if not isinstance(job_file, str) or not isinstance(plan, dict):
        item["artifacts"] = []
        return
    reusable = set(plan.get("reusable_artifacts", []))
    job = load_job(job_file)
    item["artifacts"] = [artifact.path for artifact in job.artifacts if artifact.kind in reusable]


def _all_artifacts_valid(item: dict[str, Any]) -> bool:
    job_file = item.get("job_file")
    plan = item.get("recovery_plan")
    if not isinstance(job_file, str) or not isinstance(plan, dict):
        return False
    reusable = set(plan.get("reusable_artifacts", []))
    return all(artifact.kind in reusable for artifact in load_job(job_file).artifacts)


def _item_event_sink(
    batch_file: Path,
    batch: dict[str, Any],
    item: dict[str, Any],
    event_sink: EventSink | None,
) -> EventSink:
    def sink(event: Event) -> None:
        job_file = event.data.get("job_file")
        if isinstance(job_file, str) and item.get("job_file") != job_file:
            item["job_file"] = job_file
            _write_batch(batch_file, batch)
        if event_sink:
            event_sink(event)

    return sink


def _error_category(error: Exception) -> str:
    return getattr(error, "category", "workflow")


def _mark_unfinished_item(
    batch_file: Path,
    batch: dict[str, Any],
    item: dict[str, Any],
    *,
    status: str,
    event_sink: EventSink | None,
) -> None:
    item["status"] = status
    _write_batch(batch_file, batch)
    if event_sink:
        event_type = (
            EventType.BATCH_ITEM_WAITING
            if status == "awaiting_input"
            else EventType.BATCH_ITEM_FAILED
        )
        event_sink(
            create_event(
                event_type,
                batch_file=str(batch_file),
                input=item["input"],
                job_file=item.get("job_file"),
                recovery_plan=item.get("recovery_plan"),
            )
        )


def _process_item(
    batch_file: Path,
    batch: dict[str, Any],
    item: dict[str, Any],
    *,
    index: int,
    event_sink: EventSink | None,
) -> None:
    item["status"] = "running"
    item["error"] = None
    _write_batch(batch_file, batch)
    if event_sink:
        event_sink(
            create_event(
                EventType.BATCH_ITEM_STARTED,
                batch_file=str(batch_file),
                input=item["input"],
                index=index,
                total=len(batch["items"]),
            )
        )

    try:
        raise_if_cancelled()
        job_file = item.get("job_file")
        if isinstance(job_file, str):
            category = _record_recovery(item)
            _record_artifacts(item)
            if category == "complete":
                if not _all_artifacts_valid(item):
                    item["recovery_plan"]["category"] = "manual_fix"
                    item["recovery_plan"]["reason"] = (
                        "已完成 Job 存在缺失或 checksum 无效的登记产物，请先运行 verify。"
                    )
                    _mark_unfinished_item(
                        batch_file,
                        batch,
                        item,
                        status="failed",
                        event_sink=event_sink,
                    )
                    return
                item["status"] = "completed"
                _write_batch(batch_file, batch)
                if event_sink:
                    event_sink(
                        create_event(
                            EventType.BATCH_ITEM_COMPLETED,
                            batch_file=str(batch_file),
                            input=item["input"],
                            job_file=item["job_file"],
                            index=index,
                            total=len(batch["items"]),
                            reused=True,
                        )
                    )
                return
            if category in {"awaiting_input", "manual_fix", "unrecoverable"}:
                status = "awaiting_input" if category == "awaiting_input" else "failed"
                _mark_unfinished_item(
                    batch_file,
                    batch,
                    item,
                    status=status,
                    event_sink=event_sink,
                )
                return
            workflow_result = continue_workflow(
                job_file,
                event_sink=_item_event_sink(batch_file, batch, item, event_sink),
            )
        else:
            options = WorkflowOptions(
                input=str(item["input"]),
                output=str(batch["output_root"]),
                **batch["workflow"],
            )
            workflow_result = run_workflow(
                options,
                event_sink=_item_event_sink(batch_file, batch, item, event_sink),
            )

        item["job_file"] = str(workflow_result.job_file)
        item["status"] = workflow_result.status
        item["error"] = (
            create_event(EventType.BATCH_MESSAGE, message=workflow_result.message).data["message"]
            if workflow_result.message
            else None
        )
        _record_recovery(item)
        _record_artifacts(item)
        _write_batch(batch_file, batch)
        if event_sink:
            event_type = (
                EventType.BATCH_ITEM_COMPLETED
                if workflow_result.status == "completed"
                else EventType.BATCH_ITEM_WAITING
            )
            event_sink(
                create_event(
                    event_type,
                    batch_file=str(batch_file),
                    input=item["input"],
                    job_file=item["job_file"],
                    index=index,
                    total=len(batch["items"]),
                    recovery_plan=item.get("recovery_plan"),
                )
            )
    except (KeyboardInterrupt, CommandCancelled):
        item["status"] = "interrupted"
        batch["status"] = "interrupted"
        batch["next_action"] = f"xvt batch-continue {shlex.quote(str(batch_file))}"
        _write_batch(batch_file, batch)
        if event_sink:
            event_sink(
                create_event(
                    EventType.BATCH_ITEM_INTERRUPTED,
                    batch_file=str(batch_file),
                    input=item["input"],
                    job_file=item.get("job_file"),
                    index=index,
                    total=len(batch["items"]),
                    next_action=batch["next_action"],
                )
            )
        raise
    except Exception as error:
        category = _error_category(error)
        sanitized = create_event(EventType.BATCH_ERROR, **error_event_fields(category, str(error)))
        item["status"] = "failed"
        item["error"] = sanitized.data["error"]["message"]
        try:
            _record_recovery(item)
            _record_artifacts(item)
        except (OSError, RuntimeError, ValueError):
            item["recovery_plan"] = None
            item["artifacts"] = []
        _write_batch(batch_file, batch)
        if event_sink:
            event_sink(
                create_event(
                    EventType.BATCH_ITEM_FAILED,
                    batch_file=str(batch_file),
                    input=item["input"],
                    job_file=item.get("job_file"),
                    index=index,
                    total=len(batch["items"]),
                    **error_event_fields(category, item["error"]),
                    recovery_plan=item.get("recovery_plan"),
                )
            )


def _process_batch(
    batch_file: Path,
    batch: dict[str, Any],
    *,
    event_sink: EventSink | None,
) -> BatchResult:
    if event_sink:
        event_sink(
            create_event(
                EventType.BATCH_STARTED,
                batch_file=str(batch_file),
                total=len(batch["items"]),
            )
        )
    for index, item in enumerate(batch["items"], start=1):
        if not isinstance(item, dict):
            raise ValueError("batch item must be a JSON object")
        _process_item(batch_file, batch, item, index=index, event_sink=event_sink)

    result = _result(batch_file, batch)
    batch["status"] = result.status
    batch["next_action"] = (
        f"xvt batch-continue {shlex.quote(str(batch_file))}"
        if result.status != "completed"
        else None
    )
    _write_batch(batch_file, batch)
    if event_sink:
        event_sink(
            create_event(
                EventType.BATCH_FINISHED,
                batch_file=str(batch_file),
                status=result.status,
                total=result.total,
                completed=result.completed,
                awaiting_input=result.awaiting_input,
                failed=result.failed,
                next_action=batch["next_action"],
            )
        )
    return result


def run_batch(
    input_directory: str | Path,
    options: WorkflowOptions,
    *,
    recursive: bool = False,
    event_sink: EventSink | None = None,
) -> BatchResult:
    replace(options, input="batch-input.mp4").validate()
    output_root = normalize_output_root(options.output)
    inputs = discover_batch_inputs(
        input_directory,
        output_root=output_root,
        recursive=recursive,
    )
    if not inputs:
        raise ValueError("input directory contains no supported media or subtitle files")
    non_video_inputs = [
        path for path in inputs if path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS
    ]
    if options.burn and non_video_inputs:
        raise ValueError(
            "--burn cannot process audio or subtitle-only batch inputs; "
            "move non-video files to a separate batch"
        )

    batch_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    batch_directory = output_root / "batches" / batch_id
    batch_directory.mkdir(parents=True, exist_ok=False)
    batch_file = batch_directory / "batch.json"
    now = utc_now()
    batch: dict[str, Any] = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "id": batch_id,
        "created_at": now,
        "updated_at": now,
        "status": "running",
        "input_directory": str(Path(input_directory).expanduser().resolve()),
        "output_root": str(output_root),
        "recursive": recursive,
        "workflow": _workflow_config(options),
        "items": [
            {
                "input": str(path),
                "status": "pending",
                "job_file": None,
                "artifacts": [],
                "error": None,
                "recovery_plan": None,
            }
            for path in inputs
        ],
    }
    _write_batch(batch_file, batch)
    return _process_batch(batch_file, batch, event_sink=event_sink)


def continue_batch(
    batch_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> BatchResult:
    path, batch = _load_batch(batch_file)
    batch["status"] = "running"
    _write_batch(path, batch)
    return _process_batch(path, batch, event_sink=event_sink)
