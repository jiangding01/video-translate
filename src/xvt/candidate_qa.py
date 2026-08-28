from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xvt.checksums import file_checksum
from xvt.models import Job

TRANSLATION_CANDIDATE_QA_VERSION = 1


def translation_candidate_qa_issues(
    job: Job,
    candidate_path: Path,
) -> tuple[dict[str, Any], ...] | None:
    """Return target-editable blockers, or None when the bound report is invalid."""

    artifact = next(
        (item for item in job.artifacts if item.kind == "translation_candidate_qa"),
        None,
    )
    if artifact is None or artifact.checksum is None or not candidate_path.is_file():
        return None
    path = Path(artifact.path)
    if not path.is_file() or file_checksum(path) != artifact.checksum:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        issues = data.get("issues")
        if (
            data.get("task") != "translation_candidate_qa"
            or data.get("version") != TRANSLATION_CANDIDATE_QA_VERSION
            or data.get("candidate_cues_checksum") != file_checksum(candidate_path)
            or data.get("status") not in {"pass", "warning", "needs_review", "failed"}
            or not isinstance(issues, list)
            or any(
                not isinstance(issue, dict)
                or not isinstance(issue.get("code"), str)
                or issue.get("severity") not in {"warning", "review", "error"}
                or not isinstance(issue.get("cue_id"), int)
                or isinstance(issue.get("cue_id"), bool)
                or not isinstance(issue.get("message"), str)
                for issue in issues
            )
        ):
            return None
        severities = {issue["severity"] for issue in issues}
        expected_status = (
            "failed"
            if "error" in severities
            else "needs_review"
            if "review" in severities
            else "warning"
            if "warning" in severities
            else "pass"
        )
        if data.get("status") != expected_status:
            return None
        return tuple(
            issue
            for issue in issues
            if issue["severity"] in {"error", "review"}
            and (
                issue["code"] in {"missing_translation", "glossary_violation"}
                or (
                    issue["code"] == "bilingual_overflow_risk"
                    and issue["message"].startswith("target line width ")
                )
            )
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
