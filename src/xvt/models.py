from __future__ import annotations

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
                stages[name] = StageRecord(
                    status=StageStatus(record["status"]),
                    started_at=record.get("started_at"),
                    completed_at=record.get("completed_at"),
                    error=record.get("error"),
                )

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
                artifacts.append(Artifact(**artifact))
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
