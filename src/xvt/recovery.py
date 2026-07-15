from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from xvt.job_store import load_job
from xvt.models import Artifact, Job


def file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(artifact: Artifact) -> dict[str, Any]:
    path = Path(artifact.path)
    result: dict[str, Any] = {
        "kind": artifact.kind,
        "path": str(path),
        "expected_checksum": artifact.checksum,
    }
    if not path.is_file():
        return {**result, "status": "fail", "message": "artifact file is missing"}
    actual_checksum = file_checksum(path)
    result["actual_checksum"] = actual_checksum
    if artifact.checksum is None:
        return {**result, "status": "warn", "message": "artifact has no checksum"}
    if actual_checksum != artifact.checksum:
        return {**result, "status": "fail", "message": "artifact checksum mismatch"}
    return {**result, "status": "pass", "message": "artifact is valid"}


def valid_artifact_path(job: Job, kind: str) -> Path | None:
    artifact = next((item for item in job.artifacts if item.kind == kind), None)
    if artifact is None:
        return None
    result = verify_artifact(artifact)
    return Path(artifact.path) if result["status"] == "pass" else None


def verify_job(job_file: str | Path) -> dict[str, Any]:
    job = load_job(job_file)
    artifacts = [verify_artifact(artifact) for artifact in job.artifacts]
    failed = [artifact for artifact in artifacts if artifact["status"] == "fail"]
    warnings = [artifact for artifact in artifacts if artifact["status"] == "warn"]
    return {
        "status": "fail" if failed else "pass",
        "job_id": job.id,
        "job_file": str(Path(job_file).expanduser().resolve()),
        "artifact_count": len(artifacts),
        "failed_count": len(failed),
        "warning_count": len(warnings),
        "artifacts": artifacts,
    }
