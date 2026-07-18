from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = 1


class StageName(StrEnum):
    INSPECT = "inspect"
    ACQUIRE = "acquire"
    NORMALIZE_AUDIO = "normalize_audio"
    IMPORT_SUBTITLES = "import_subtitles"
    TRANSCRIBE = "transcribe"
    ALIGN = "align"
    SEGMENT = "segment"
    TRANSLATE = "translate"
    SUBTITLE_QA = "subtitle_qa"
    RENDER = "render"
    BURN = "burn"
    MEDIA_QA = "media_qa"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class StageRecord:
    status: StageStatus = StageStatus.PENDING
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None


@dataclass(slots=True)
class Artifact:
    kind: str
    path: str
    checksum: str | None = None


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    start_ms: int
    end_ms: int
    probability: float | None = None


@dataclass(slots=True)
class Cue:
    id: int
    start_ms: int
    end_ms: int
    source_text: str
    target_text: str | None = None
    words: list[Word] = field(default_factory=list)
    source_language: str | None = None
    target_language: str | None = None
    text_confidence: float | None = None
    timing_confidence: float | None = None
    provenance: str = "asr"
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Transcript:
    language: str
    engine: str
    words: tuple[Word, ...]
    language_probability: float | None = None


def _known_fields(cls: type) -> set[str]:
    """Return the set of declared field names for a dataclass."""

    return {f.name for f in dataclasses.fields(cls)}


_STAGE_RECORD_FIELDS = _known_fields(StageRecord)
_ARTIFACT_FIELDS = _known_fields(Artifact)


_MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}
"""Job schema migration chain, keyed by the source ``schema_version``.

When to add an entry
    Only when a *structural* change is made to the job dict that older
    parsing code cannot tolerate (a field is renamed, removed, repurposed,
    or a previously optional field becomes required with new semantics) and
    ``SCHEMA_VERSION`` is bumped as a result. Purely additive changes (new
    optional fields on ``Job``, ``Artifact``, or stage records) are already
    forward compatible: unknown fields are filtered out and ignored by
    ``Job.from_dict``, so no migration or version bump is needed for those.

Migration function contract
    - Registered under the *source* version, e.g. ``_MIGRATIONS[0]`` maps a
      version-0 job dict to its version-1 shape.
    - Must be a pure function: ``(dict) -> dict``. Do not mutate the input
      dict in place; return a new (or copied) dict.
    - Must set the returned dict's ``schema_version`` field to the next
      version number (the one right after the source version).
    - ``Job.from_dict`` applies migrations repeatedly, one version at a
      time, until the dict reaches ``SCHEMA_VERSION`` or no further
      migration is registered for the current version. In the latter case,
      the existing "Unsupported job schema version" error is raised.
"""


@dataclass(slots=True)
class Job:
    id: str
    created_at: str
    updated_at: str
    input: str
    output_root: str
    config: dict[str, Any]
    stages: dict[str, StageRecord] = field(
        default_factory=lambda: {stage.value: StageRecord() for stage in StageName}
    )
    artifacts: list[Artifact] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        schema_version = data.get("schema_version")
        while schema_version != SCHEMA_VERSION and schema_version in _MIGRATIONS:
            data = _MIGRATIONS[schema_version](data)
            schema_version = data.get("schema_version")
        if schema_version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported job schema version: {schema_version}")

        required_fields = ("id", "created_at", "updated_at", "input", "output_root", "stages")
        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            raise ValueError(f"Invalid job file: missing fields: {', '.join(missing_fields)}")

        string_fields = ("id", "created_at", "updated_at", "input", "output_root")
        if any(not isinstance(data[field], str) for field in string_fields):
            raise ValueError("Invalid job file: identity and path fields must be strings")

        stages_data = data["stages"]
        if not isinstance(stages_data, dict):
            raise ValueError("Invalid job file: stages must be an object")
        missing_stages = [stage.value for stage in StageName if stage.value not in stages_data]
        if missing_stages:
            raise ValueError(f"Invalid job file: missing stages: {', '.join(missing_stages)}")

        config = data.get("config", {})
        artifacts_data = data.get("artifacts", [])
        if not isinstance(config, dict):
            raise ValueError("Invalid job file: config must be an object")
        if not isinstance(artifacts_data, list):
            raise ValueError("Invalid job file: artifacts must be an array")

        try:
            stages: dict[str, StageRecord] = {}
            for name, record in stages_data.items():
                if not isinstance(name, str) or not isinstance(record, dict):
                    raise TypeError("stage names must be strings and records must be objects")
                for field in ("started_at", "completed_at", "error"):
                    if record.get(field) is not None and not isinstance(record[field], str):
                        raise TypeError(f"stage {field} must be a string or null")
                # Filter to known fields so unknown fields from newer schema
                # versions are silently ignored instead of raising TypeError.
                known_record = {k: v for k, v in record.items() if k in _STAGE_RECORD_FIELDS}
                known_record["status"] = StageStatus(record["status"])
                stages[name] = StageRecord(**known_record)

            artifacts: list[Artifact] = []
            for artifact in artifacts_data:
                if not isinstance(artifact, dict):
                    raise TypeError("artifact records must be objects")
                if not isinstance(artifact.get("kind"), str) or not isinstance(
                    artifact.get("path"), str
                ):
                    raise TypeError("artifact kind and path must be strings")
                checksum = artifact.get("checksum")
                if checksum is not None and not isinstance(checksum, str):
                    raise TypeError("artifact checksum must be a string or null")
                # Filter to known fields so unknown fields from newer schema
                # versions are silently ignored instead of raising TypeError.
                known_artifact = {k: v for k, v in artifact.items() if k in _ARTIFACT_FIELDS}
                artifacts.append(Artifact(**known_artifact))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid job file: {error}") from error

        return cls(
            id=data["id"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            input=data["input"],
            output_root=data["output_root"],
            config=config,
            stages=stages,
            artifacts=artifacts,
            schema_version=schema_version,
        )
