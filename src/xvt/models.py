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

        stages = {
            name: StageRecord(
                status=StageStatus(record["status"]),
                started_at=record.get("started_at"),
                completed_at=record.get("completed_at"),
                error=record.get("error"),
            )
            for name, record in data["stages"].items()
        }
        artifacts = [Artifact(**artifact) for artifact in data.get("artifacts", [])]
        return cls(
            id=data["id"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            input=data["input"],
            output_root=data["output_root"],
            config=data.get("config", {}),
            stages=stages,
            artifacts=artifacts,
            schema_version=schema_version,
        )
