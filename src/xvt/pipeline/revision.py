"""人工修订语义校对后的译文，不改变字幕时间轴。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from xvt.artifacts import ArtifactKind, downstream_of
from xvt.errors import UserInputError
from xvt.events import EventSink, EventType, create_event, job_event_fields
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job
from xvt.models import Artifact, Job, StageName, StageStatus
from xvt.pipeline.common import _artifact_checksum_for, _checksum, _load_cues, _write_json
from xvt.recovery import valid_artifact_path
from xvt.translation_provider import SEMANTIC_REVIEW_PROMPT_VERSION
from xvt.workflow_config import clear_burn_fingerprint


def _target_kinds(job: Job) -> tuple[str, str, Path, Path] | None:
    from xvt.pipeline.semantic import semantic_review_target

    target = semantic_review_target(job)
    if target is None:
        return None
    report, candidate = target
    for report_kind, candidate_kind in (
        ("translation_semantic_qa_reviewed", "translation_reviewed_cues"),
        ("translation_semantic_qa_final", "translation_repaired_cues"),
        ("translation_semantic_qa", "translation_polished_cues"),
    ):
        if (
            valid_artifact_path(job, report_kind) == report
            and valid_artifact_path(job, candidate_kind) == candidate
        ):
            return report_kind, candidate_kind, report, candidate
    return None


def _issue_set(report: dict[str, Any]) -> list[dict[str, Any]]:
    issues = report.get("issues")
    if report.get("status") != "needs_review" or not isinstance(issues, list) or not issues:
        raise UserInputError("semantic review report does not require translation revision")
    normalized: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            raise UserInputError("semantic review report contains an invalid issue")
        category = issue.get("category")
        cue_ids = issue.get("cue_ids")
        severity = issue.get("severity")
        suggested_items = issue.get("suggested_items", [])
        if (
            not isinstance(category, str)
            or severity != "review"
            or not isinstance(cue_ids, list)
            or not cue_ids
            or any(not isinstance(cue_id, int) or isinstance(cue_id, bool) for cue_id in cue_ids)
            or not isinstance(suggested_items, list)
        ):
            raise UserInputError("semantic review report contains an invalid issue")
        normalized_suggestions: list[dict[str, Any]] = []
        for item in suggested_items:
            cue_id = item.get("id") if isinstance(item, dict) else None
            text = item.get("text") if isinstance(item, dict) else None
            if not isinstance(cue_id, int) or isinstance(cue_id, bool) or not isinstance(text, str):
                raise UserInputError("semantic review report contains an invalid suggestion")
            normalized_suggestions.append({"id": cue_id, "text": text})
        normalized.append(
            {
                "category": category,
                "cue_ids": sorted(cue_ids),
                "severity": severity,
                "message": str(issue.get("message", "")),
                "suggested_items": sorted(
                    normalized_suggestions, key=lambda item: (item["id"], item["text"])
                ),
            }
        )
    return sorted(normalized, key=lambda item: (item["category"], item["cue_ids"]))


def _revision_fingerprint(
    job: Job,
    report_kind: str,
    candidate_kind: str,
    report: dict[str, Any],
) -> str:
    payload = {
        "job_id": job.id,
        "source_cues_checksum": _artifact_checksum_for(job, "source_cues"),
        "candidate_kind": candidate_kind,
        "candidate_cues_checksum": _artifact_checksum_for(job, candidate_kind),
        "report_kind": report_kind,
        "report_checksum": _artifact_checksum_for(job, report_kind),
        "prompt_version": report.get("prompt_version", SEMANTIC_REVIEW_PROMPT_VERSION),
        "issues": _issue_set(report),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def translation_review_input_path(job: Job) -> Path | None:
    target = _target_kinds(job)
    if target is None:
        return None
    report_kind, candidate_kind, report_path, _candidate_path = target
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        fingerprint = _revision_fingerprint(job, report_kind, candidate_kind, report)
        path = get_job_directory(job) / "review" / f"translation-review-{fingerprint[:12]}.json"
        return path if path.is_file() else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError, UserInputError):
        return None
    return None


def ensure_translation_review_input(
    job_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> Path:
    job = load_job(job_file)
    existing = translation_review_input_path(job)
    if existing is not None:
        return existing
    target = _target_kinds(job)
    if target is None:
        raise UserInputError("no current semantic review report is available for revision")
    report_kind, candidate_kind, report_path, candidate_path = target
    report = json.loads(report_path.read_text(encoding="utf-8"))
    issues = _issue_set(report)
    fingerprint = _revision_fingerprint(job, report_kind, candidate_kind, report)
    cues = _load_cues(candidate_path)
    affected = {cue_id for issue in issues for cue_id in issue["cue_ids"]}
    path = get_job_directory(job) / "review" / f"translation-review-{fingerprint[:12]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        path,
        {
            "task": "translation_review_fixes",
            "revision_fingerprint": fingerprint,
            "scope": "translation",
            "report": str(report_path),
            "issues": issues,
            "items": [
                {"id": cue.id, "text": cue.target_text or ""} for cue in cues if cue.id in affected
            ],
        },
    )
    if event_sink:
        event_sink(
            create_event(
                EventType.TRANSLATION_REVIEW_INPUT_READY,
                **job_event_fields(job.id, get_job_file(job)),
                path=str(path),
                scope="translation",
                item_count=len(affected),
            )
        )
    return path


def apply_translation_review_fixes(
    job_file: str | Path,
    response_file: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> tuple[Path, Path]:
    job = load_job(job_file)
    target = _target_kinds(job)
    if target is None:
        raise UserInputError("no current semantic review report is available for revision")
    report_kind, candidate_kind, report_path, candidate_path = target
    report = json.loads(report_path.read_text(encoding="utf-8"))
    expected_fingerprint = _revision_fingerprint(job, report_kind, candidate_kind, report)
    try:
        response = json.loads(Path(response_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UserInputError(f"cannot read translation review response: {error}") from error
    if not isinstance(response, dict):
        raise UserInputError("translation review response must be a JSON object")
    if response.get("task") != "translation_review_fixes" or response.get("scope") != "translation":
        raise UserInputError("translation review response task or scope is invalid")
    if response.get("revision_fingerprint") != expected_fingerprint:
        raise UserInputError("translation review response is stale for the current report")
    items = response.get("items")
    if not isinstance(items, list) or not items:
        raise UserInputError("translation review response items must be a non-empty array")
    changes: dict[int, str] = {}
    for item in items:
        cue_id = item.get("id") if isinstance(item, dict) else None
        text = item.get("text") if isinstance(item, dict) else None
        if (
            not isinstance(cue_id, int)
            or isinstance(cue_id, bool)
            or cue_id in changes
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise UserInputError(
                "translation review response contains an invalid or duplicate item"
            )
        changes[cue_id] = text.strip()
    candidate = _load_cues(candidate_path)
    known_ids = {cue.id for cue in candidate}
    if not changes.keys() <= known_ids:
        raise UserInputError("translation review response contains an unknown cue id")
    current_text = {cue.id: cue.target_text or "" for cue in candidate}
    changed_ids = {cue_id for cue_id, text in changes.items() if current_text[cue_id] != text}
    if not changed_ids:
        raise UserInputError("translation review response does not change any cue text")
    reviewed = [replace(cue, target_text=changes.get(cue.id, cue.target_text)) for cue in candidate]

    artifacts_dir = get_job_directory(job) / "artifacts"
    revision_suffix = expected_fingerprint[:12]
    revision_path = artifacts_dir / f"translation-review-revision-{revision_suffix}.json"
    reviewed_path = artifacts_dir / f"translation-reviewed-cues-{revision_suffix}.json"
    _write_json(
        revision_path,
        {
            "task": "translation_review_fixes",
            "revision_fingerprint": expected_fingerprint,
            "scope": "translation",
            "source_report_kind": report_kind,
            "source_report_checksum": _artifact_checksum_for(job, report_kind),
            "source_candidate_kind": candidate_kind,
            "source_candidate_checksum": _artifact_checksum_for(job, candidate_kind),
            "items": [{"id": cue.id, "text": cue.target_text or ""} for cue in reviewed],
        },
    )
    _write_json(reviewed_path, [asdict(cue) for cue in reviewed])

    invalidated = {
        ArtifactKind.TRANSLATION_SEMANTIC_QA,
        ArtifactKind.TRANSLATION_SEMANTIC_REPAIR,
        ArtifactKind.TRANSLATION_REPAIRED_CUES,
        ArtifactKind.TRANSLATION_SEMANTIC_QA_FINAL,
        ArtifactKind.TRANSLATION_REVIEW_REVISION,
        ArtifactKind.TRANSLATION_REVIEWED_CUES,
        ArtifactKind.TRANSLATION_SEMANTIC_QA_REVIEWED,
    }
    for root in tuple(invalidated):
        invalidated.update(downstream_of(root))
    job.artifacts = [artifact for artifact in job.artifacts if artifact.kind not in invalidated]
    job.artifacts.extend(
        (
            Artifact("translation_review_revision", str(revision_path), _checksum(revision_path)),
            Artifact("translation_reviewed_cues", str(reviewed_path), _checksum(reviewed_path)),
        )
    )
    job.config.pop("translation_review_acknowledgment", None)
    job.config.pop("subtitle_review_acknowledgment", None)
    clear_burn_fingerprint(job)
    for stage in (
        StageName.TRANSLATE,
        StageName.SUBTITLE_QA,
        StageName.RENDER,
        StageName.BURN,
        StageName.MEDIA_QA,
    ):
        record = job.stages[stage]
        record.status = StageStatus.PENDING
        record.started_at = None
        record.completed_at = None
        record.error = None
    save_job(job)
    if event_sink:
        for kind, path in (
            ("translation_review_revision", revision_path),
            ("translation_reviewed_cues", reviewed_path),
        ):
            event_sink(
                create_event(
                    EventType.ARTIFACT_CREATED,
                    **job_event_fields(job.id, get_job_file(job)),
                    kind=kind,
                    path=str(path),
                )
            )
        event_sink(
            create_event(
                EventType.TRANSLATION_REVIEW_REVISED,
                **job_event_fields(job.id, get_job_file(job)),
                scope="translation",
                item_count=len(changed_ids),
                checksum=_artifact_checksum_for(job, "translation_reviewed_cues"),
            )
        )
    return revision_path, reviewed_path
