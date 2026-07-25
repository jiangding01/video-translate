"""Deterministic source-subtitle candidate QA and selection decisions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from xvt.models import Cue
from xvt.qa import SubtitleIssue, SubtitleQaReport, check_subtitles


class SourceStrategy(StrEnum):
    AUTO = "auto"
    SUBTITLES = "subtitles"
    ASR = "asr"


class SourceCandidateKind(StrEnum):
    NONE = "none"
    USER = "user"
    MANUAL = "manual"
    AUTOMATIC = "automatic"


class SourceSelectionAction(StrEnum):
    SUBTITLES = "subtitles"
    ALIGN = "align"
    ASR = "asr"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class CandidateRepair:
    code: str
    cue_id: int
    previous_end_ms: int
    new_end_ms: int


@dataclass(frozen=True, slots=True)
class SourceCandidateEvaluation:
    kind: SourceCandidateKind
    cues: tuple[Cue, ...]
    report: SubtitleQaReport
    repairs: tuple[CandidateRepair, ...]

    @property
    def has_errors(self) -> bool:
        return any(issue.severity == "error" for issue in self.report.issues)


@dataclass(frozen=True, slots=True)
class SourceSelectionPlan:
    action: SourceSelectionAction
    reason: str
    reference_allowed: bool


def invalid_source_candidate(
    kind: SourceCandidateKind,
    message: str,
) -> SourceCandidateEvaluation:
    return SourceCandidateEvaluation(
        kind=kind,
        cues=(),
        report=SubtitleQaReport(
            status="failed",
            cue_count=0,
            issues=(SubtitleIssue("invalid_candidate", "error", 0, message),),
        ),
        repairs=(),
    )


def evaluate_source_candidate(
    cues: list[Cue],
    *,
    kind: SourceCandidateKind,
    media_duration_ms: int | None,
    video_width: int | None = None,
    video_height: int | None = None,
) -> SourceCandidateEvaluation:
    evaluated = list(cues)
    repairs: list[CandidateRepair] = []
    if (
        kind in {SourceCandidateKind.MANUAL, SourceCandidateKind.AUTOMATIC}
        and evaluated
        and media_duration_ms is not None
        and media_duration_ms > 0
    ):
        last = evaluated[-1]
        overshoot_ms = last.end_ms - media_duration_ms
        if last.start_ms < media_duration_ms and 0 < overshoot_ms <= 500:
            evaluated[-1] = replace(last, end_ms=media_duration_ms)
            repairs.append(
                CandidateRepair(
                    code="clip_last_cue_end",
                    cue_id=last.id,
                    previous_end_ms=last.end_ms,
                    new_end_ms=media_duration_ms,
                )
            )

    report = check_subtitles(
        evaluated,
        media_duration_ms=media_duration_ms,
        video_width=video_width,
        video_height=video_height,
    )
    return SourceCandidateEvaluation(kind, tuple(evaluated), report, tuple(repairs))


def plan_source_selection(
    strategy: SourceStrategy,
    *,
    candidate_kind: SourceCandidateKind,
    candidate_has_errors: bool,
    has_translation_goal: bool,
) -> SourceSelectionPlan:
    candidate_present = candidate_kind is not SourceCandidateKind.NONE
    reference_allowed = candidate_present and not candidate_has_errors

    if strategy is SourceStrategy.ASR:
        return SourceSelectionPlan(
            SourceSelectionAction.ASR,
            "forced_asr",
            reference_allowed,
        )

    if strategy is SourceStrategy.SUBTITLES:
        if not candidate_present:
            return SourceSelectionPlan(
                SourceSelectionAction.FAIL,
                "platform_subtitles_missing",
                False,
            )
        if candidate_kind is SourceCandidateKind.USER:
            reason = "user_subtitles_preserved"
        elif candidate_has_errors:
            reason = "candidate_qa_failed"
        elif candidate_kind is SourceCandidateKind.MANUAL:
            reason = "manual_subtitles_passed"
        else:
            reason = "automatic_subtitles_source_only"
        return SourceSelectionPlan(SourceSelectionAction.SUBTITLES, reason, False)

    if candidate_kind is SourceCandidateKind.NONE:
        return SourceSelectionPlan(
            SourceSelectionAction.ASR,
            "platform_subtitles_missing",
            False,
        )
    if candidate_kind is SourceCandidateKind.USER:
        return SourceSelectionPlan(
            SourceSelectionAction.SUBTITLES,
            "user_subtitles_preserved",
            False,
        )
    if candidate_kind is SourceCandidateKind.MANUAL:
        if candidate_has_errors:
            return SourceSelectionPlan(
                SourceSelectionAction.ALIGN,
                "candidate_qa_failed",
                False,
            )
        return SourceSelectionPlan(
            SourceSelectionAction.SUBTITLES,
            "manual_subtitles_passed",
            False,
        )
    if has_translation_goal:
        return SourceSelectionPlan(
            SourceSelectionAction.ASR,
            (
                "candidate_qa_failed"
                if candidate_has_errors
                else "automatic_subtitles_reference_only"
            ),
            reference_allowed,
        )
    if candidate_has_errors:
        return SourceSelectionPlan(
            SourceSelectionAction.ASR,
            "candidate_qa_failed",
            False,
        )
    return SourceSelectionPlan(
        SourceSelectionAction.SUBTITLES,
        "automatic_subtitles_source_only",
        False,
    )
