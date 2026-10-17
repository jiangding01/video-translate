from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from xvt.config import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BATCH_SIZE,
    DEFAULT_ENGINE,
    DEFAULT_HARDWARE_ENCODING,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MIN_ALIGNMENT_CONFIDENCE,
    DEFAULT_PROVIDER,
    DEFAULT_SEGMENTATION_PRESET,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TRANSLATION_STYLE,
)
from xvt.errors import InternalError, UserInputError, XvtError
from xvt.events import EventSink, EventType, create_event, error_event_fields, job_event_fields
from xvt.job_store import get_job_directory, get_job_file, load_job, save_job
from xvt.models import Artifact, Job, StageName, StageStatus
from xvt.pipeline import (
    REPAIR_POLISH_DRAFT_MANUAL_FIX_MESSAGE,
    add_artifact,
    agent_semantic_review_waiting,
    agent_translation_polish_core_satisfied,
    agent_translation_polish_manifest_valid,
    agent_translation_polish_progress_ready,
    agent_translation_polish_started,
    agent_translation_polish_waiting,
    apply_subtitle_translation,
    artifact_checksum_for,
    burn_translated_job,
    complete_acknowledged_semantic_review,
    complete_agent_polished_outputs,
    complete_translated_outputs,
    cue_retranslation_waiting,
    ensure_semantic_review_inputs,
    error_category,
    finalize_acknowledged_subtitle_review,
    is_subtitle_input,
    resolved_translation_policy,
    resume_job,
    resume_remote_transcription,
    run_transcription,
    run_translation,
    run_translation_polish,
    run_translation_semantic_review,
    semantic_review_approved,
    semantic_review_satisfied,
    semantic_review_waiting,
    source_selection_matches,
    translation_polish_satisfied,
    translation_progress_rebuildable,
)
from xvt.recovery import (
    build_recovery_plan,
    file_checksum,
    readonly_checksum_scope,
    subtitle_qa_failure_guidance,
    valid_artifact_path,
)
from xvt.segment import SEGMENTATION_PRESETS
from xvt.translate import TranslationPolicy, load_translation_policy
from xvt.workflow_config import (
    WorkflowConfig,
    clear_burn_fingerprint,
    get_workflow_config,
    require_workflow_config,
)

# plan_advance_steps 可能返回的全部 Agent 等待步骤名。CLI 的回填路由表以此为准
# （不可自动路由的等待步骤在 cli._UNROUTABLE_AWAIT_STEPS 里显式登记并说明理由）。
AWAIT_STEPS: frozenset[str] = frozenset(
    {
        "await_agent_translation",
        "await_agent_polish",
        "await_agent_semantic_review",
        "await_translation_review",
        "await_cue_retranslation",
    }
)


@dataclass(frozen=True, slots=True)
class WorkflowOptions:
    input: str
    output: str | Path
    engine: str = DEFAULT_ENGINE
    language: str | None = None
    subtitles: str | Path | None = None
    source_strategy: str = "auto"
    align_subtitles: bool = False
    minimum_alignment_confidence: float = DEFAULT_MIN_ALIGNMENT_CONFIDENCE
    allow_low_confidence_alignment: bool = False
    cookies_from_browser: str | None = None
    proxy: str | None = None
    youtube_subtitles: bool = False
    subtitle_language: str | None = None
    allow_auto_subtitles: bool = False
    translation_subtitles: str | Path | None = None
    target_language: str | None = None
    provider: str = DEFAULT_PROVIDER
    base_url: str | None = None
    model: str | None = None
    api_key_env: str = DEFAULT_API_KEY_ENV
    batch_size: int = DEFAULT_BATCH_SIZE
    max_retries: int = DEFAULT_MAX_RETRIES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    glossary: str | Path | None = None
    translation_style: str = DEFAULT_TRANSLATION_STYLE
    translation_quality: str = "standard"
    segmentation_preset: str = DEFAULT_SEGMENTATION_PRESET
    burn: bool = False
    burn_media: str | Path | None = None
    burn_output: str | Path | None = None
    hardware_encoding: bool = DEFAULT_HARDWARE_ENCODING
    target_font: str | None = None
    source_font: str | None = None

    def validate(self) -> None:
        subtitle_input = is_subtitle_input(self.input)
        if subtitle_input and self.subtitles:
            raise ValueError("subtitle input cannot be combined with --subtitles")
        if subtitle_input and self.align_subtitles:
            raise ValueError("--align-subtitles requires media input")
        if subtitle_input and self.source_strategy == "asr":
            raise ValueError("--source-strategy asr requires media input")
        if subtitle_input and self.burn:
            raise ValueError("--burn requires media input")
        if self.align_subtitles and not self.subtitles:
            raise ValueError("--align-subtitles requires --subtitles")
        if self.source_strategy == "asr" and self.align_subtitles:
            raise ValueError("--source-strategy asr cannot be combined with --align-subtitles")
        if self.allow_low_confidence_alignment and not self.align_subtitles:
            raise ValueError("--allow-low-confidence-alignment requires --align-subtitles")
        if not 0 <= self.minimum_alignment_confidence <= 1:
            raise ValueError("--min-alignment-confidence must be between 0 and 1")
        if self.youtube_subtitles and self.subtitles:
            raise ValueError("--youtube-subtitles cannot be combined with --subtitles")
        if self.youtube_subtitles and not self.subtitle_language:
            raise ValueError("--youtube-subtitles requires --subtitle-language")
        if self.allow_auto_subtitles and not self.youtube_subtitles:
            raise ValueError("--allow-auto-subtitles requires --youtube-subtitles")
        if self.translation_subtitles and not self.target_language:
            raise ValueError("--translation-subtitles requires --target-language")
        if self.translation_quality not in {"standard", "polished"}:
            raise ValueError("--translation-quality must be standard or polished")
        if self.translation_quality == "polished" and not self.target_language:
            raise ValueError("--translation-quality polished requires --target-language")
        if self.translation_subtitles and self.translation_quality == "polished":
            raise ValueError(
                "--translation-subtitles cannot be combined with --translation-quality polished; "
                "import the translation first, then run polish-translation"
            )
        if self.burn and not self.target_language:
            raise ValueError("--burn requires --target-language")
        if self.burn_media and not self.burn:
            raise ValueError("--burn-media requires --burn")
        if self.burn_output and not self.burn:
            raise ValueError("--burn-output requires --burn")
        if not self.hardware_encoding and not self.burn:
            raise ValueError("--software requires --burn")
        if (self.target_font or self.source_font) and not self.burn:
            raise ValueError("--target-font and --source-font require --burn")
        if (self.base_url or self.model) and not self.target_language:
            raise ValueError("--base-url and --model require --target-language")
        if self.glossary and not self.target_language:
            raise ValueError("--glossary requires --target-language")
        if self.glossary and self.translation_subtitles:
            raise ValueError("--glossary cannot be combined with --translation-subtitles")
        if self.segmentation_preset not in SEGMENTATION_PRESETS:
            raise ValueError("--segmentation-preset must be conservative, balanced, or compact")
        if self.source_strategy not in {"auto", "subtitles", "asr"}:
            raise ValueError("--source-strategy must be auto, subtitles, or asr")

    def persisted_config(self, policy: TranslationPolicy | None = None) -> WorkflowConfig:
        requested_outputs: list[str] = []
        if self.target_language:
            requested_outputs.extend(["translated_subtitles", "bilingual_subtitles"])
        if self.burn:
            requested_outputs.append("burned_video")
        return WorkflowConfig(
            target_language=self.target_language,
            requested_outputs=tuple(requested_outputs),
            translation_subtitles=(
                str(Path(self.translation_subtitles).expanduser().resolve())
                if self.translation_subtitles
                else None
            ),
            provider=self.provider,
            base_url=self.base_url,
            model=self.model,
            api_key_env=self.api_key_env,
            batch_size=self.batch_size,
            max_retries=self.max_retries,
            timeout_seconds=self.timeout_seconds,
            translation_policy=policy,
            translation_quality=self.translation_quality,
            segmentation_preset=self.segmentation_preset,
            burn=self.burn,
            burn_media=(
                str(Path(self.burn_media).expanduser().resolve()) if self.burn_media else None
            ),
            burn_output=(
                str(Path(self.burn_output).expanduser().resolve()) if self.burn_output else None
            ),
            hardware_encoding=self.hardware_encoding,
            target_font=self.target_font,
            source_font=self.source_font,
        )


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    status: str
    job_file: Path
    artifacts: tuple[Path, ...] = ()
    message: str | None = None


def _burn_fingerprint(job: Job, config: WorkflowConfig) -> str | None:
    translated_checksum = artifact_checksum_for(job, "translated_cues")
    if not translated_checksum:
        return None

    media_value = config.burn_media or job.input
    parsed = urlparse(str(media_value))
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        media_identity: object = artifact_checksum_for(job, "source_video") or str(media_value)
    else:
        media_path = Path(str(media_value)).expanduser()
        if not media_path.is_file():
            return None
        stat = media_path.stat()
        media_identity = {
            "path": str(media_path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    payload = {
        "translated_cues_checksum": translated_checksum,
        "media": media_identity,
        "hardware_encoding": config.hardware_encoding,
        "target_font": config.target_font,
        "source_font": config.source_font,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _burn_source_media_checksum(job: Job, config: WorkflowConfig) -> str | None:
    media_value = config.burn_media or job.input
    parsed = urlparse(str(media_value))
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        media_path = valid_artifact_path(job, "source_video")
        return artifact_checksum_for(job, "source_video") if media_path is not None else None
    media_path = Path(str(media_value)).expanduser()
    return file_checksum(media_path) if media_path.is_file() else None


def _stored_burn_fingerprint(job: Job) -> str | None:
    """读取已登记的烧录指纹：优先 checksum 有效的 artifact，回退旧 workflow 字段。"""

    path = valid_artifact_path(job, "burn_fingerprint")
    if path is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("fingerprint")
        return value if isinstance(value, str) and value else None
    config = get_workflow_config(job)
    if config is not None and config.burn_fingerprint:
        return config.burn_fingerprint
    return None


def media_qa_failure(job: Job) -> str | None:
    """媒体 QA 放行判定的**唯一**实现；放行时返回 None，否则返回失败原因标签。

    标签供调用方派生各自的用户文案，调用方不得就地重造这四步判定：

    - ``"stage"``：``MEDIA_QA`` stage 不是 succeeded
    - ``"missing"``：媒体 QA 报告缺失或 checksum 失效
    - ``"unreadable"``：报告不可解析
    - ``"status"``：报告判定不是 ``pass``

    收敛前本文件的三处（relink 资格、``relink_burned_output``、``_burn_satisfied``）
    各写一遍同样四步，``recovery._burn_recovery_reason`` 还只看 stage 构成第四种口径。
    其中 ``_burn_satisfied`` 的 stage 与 status 两道判定长期零测试覆盖，放过了
    「原子重烧后媒体 QA 失败却判已完成」（违反不可破坏约束第 4 条）。
    """

    if job.stages[StageName.MEDIA_QA].status is not StageStatus.SUCCEEDED:
        return "stage"
    report_path = valid_artifact_path(job, "media_qa")
    if report_path is None:
        return "missing"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return "unreadable"
    if not isinstance(report, dict) or report.get("status") != "pass":
        return "status"
    return None


def media_qa_passed(job: Job) -> bool:
    """媒体 QA 是否放行（不关心失败原因）。"""

    return media_qa_failure(job) is None


def _burn_proof(job: Job) -> dict[str, str] | None:
    """读取 checksum 有效的 v2 烧录证明记录。"""

    path = valid_artifact_path(job, "burn_fingerprint")
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != 2:
        return None
    fields = ("fingerprint", "burned_video_checksum", "source_media_checksum")
    if not all(isinstance(data.get(field), str) and data[field] for field in fields):
        return None
    return {field: data[field] for field in fields}


def _burn_proof_fingerprint_matches(
    job: Job,
    config: WorkflowConfig,
    proof: dict[str, str],
    *,
    require_source_media: bool,
) -> bool:
    """v2 proof 的指纹（可选含源媒体内容 checksum）是否与当前 Job 匹配。

    ``require_source_media`` 是两个调用点**刻意**的差异，不是重复：
    ``status`` 侧（``burned_output_relink_recovery_available``）不哈希源媒体，免得
    每次 status 都读一遍大文件；真正改写登记的 ``relink_burned_output`` 必须哈希并
    比对，否则源媒体被同 size+mtime 的内容替换时会放过一次应有的重烧。

    ``burned_video`` 的 checksum 比对**不在**这里：两个调用点对它的时机与报错文案
    不同（relink 要先确认候选文件存在、再把候选与登记值一起比，报「候选 checksum
    不匹配」），放进来只会逼出一个带标签的返回值。
    """

    current_fingerprint = _burn_fingerprint(job, config)
    if current_fingerprint is None or proof["fingerprint"] != current_fingerprint:
        return False
    if _stored_burn_fingerprint(job) != current_fingerprint:
        return False
    if not require_source_media:
        return True
    current_source_media_checksum = _burn_source_media_checksum(job, config)
    return (
        current_source_media_checksum is not None
        and proof["source_media_checksum"] == current_source_media_checksum
    )


def burned_output_relink_recovery_available(
    job: Job,
    config: WorkflowConfig,
    *,
    burned_video_valid: bool,
) -> bool:
    """判断 status 是否可安全提示已移动成品的重新关联。

    ``burned_video_valid`` 由 recovery 已计算的 artifact 校验结果提供，避免 status
    重复读取大型成品；此检查也刻意不读取源媒体内容。真正的 relink 命令会在写入前
    重新计算并比对。
    """

    if not config.burn:
        return False
    burned_videos = [artifact for artifact in job.artifacts if artifact.kind == "burned_video"]
    if len(burned_videos) != 1:
        return False
    burned_video = burned_videos[0]
    if not burned_video.checksum or burned_video_valid:
        return False
    if not media_qa_passed(job):
        return False
    # translated_cues 是本处刻意多出来的条件：没有有效译文 cue 的成品重新关联没有意义。
    if valid_artifact_path(job, "translated_cues") is None:
        return False
    proof = _burn_proof(job)
    return bool(
        proof is not None
        and _burn_proof_fingerprint_matches(job, config, proof, require_source_media=False)
        and proof["burned_video_checksum"] == burned_video.checksum
    )


def _record_burn_fingerprint(job_file: Path, *, event_sink: EventSink | None = None) -> None:
    """将派生指纹登记为 ``burn_fingerprint`` artifact，并清除历史 workflow 旁路字段。

    登记走 ``add_artifact``，因此与其它产物一样发 ``artifact_created`` 事件
    （AGENTS 不可破坏约束 7）：这份 v2 proof 是 ``relink-burned-output`` 的唯一
    授权凭据，不能只存在于 ``job.json`` 而在事件流里不可见。
    """

    job = load_job(job_file)
    try:
        config = require_workflow_config(job)
    except ValueError:
        return
    fingerprint = _burn_fingerprint(job, config)
    source_media_checksum = _burn_source_media_checksum(job, config)
    burned_videos = [artifact for artifact in job.artifacts if artifact.kind == "burned_video"]
    if not fingerprint or not source_media_checksum or len(burned_videos) != 1:
        return
    burned_video = burned_videos[0]
    if not burned_video.checksum:
        return
    artifacts_directory = get_job_directory(job) / "artifacts"
    artifacts_directory.mkdir(parents=True, exist_ok=True)
    path = artifacts_directory / "burn-fingerprint.json"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "version": 2,
                "fingerprint": fingerprint,
                "burned_video_checksum": burned_video.checksum,
                "source_media_checksum": source_media_checksum,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    # 先清旧 artifact / workflow 旁路，再登记新文件，避免双写漂移。
    # add_artifact 负责 checksum、同 kind 去重、save_job 与 artifact_created 事件。
    clear_burn_fingerprint(job)
    add_artifact(job, "burn_fingerprint", path, event_sink)


#: ``media_qa_failure`` 的失败标签 → relink 的用户报错文案。文案逐项保留历史措辞，
#: 判定本身改由共享谓词给出（原先这里是三份媒体 QA 口径之一）。
_RELINK_MEDIA_QA_MESSAGES = {
    "stage": "burned video cannot be relinked before media QA passes",
    "missing": "burned video cannot be relinked without a valid media QA report",
    "unreadable": "burned video media QA report is unreadable",
    "status": "burned video cannot be relinked unless media QA passed",
}


def relink_burned_output(
    job_file: str | Path,
    candidate: str | Path,
    *,
    event_sink: EventSink | None = None,
) -> Path:
    """将原烧录成品登记重新绑定到 checksum 完全一致的现存文件。"""

    requested_job_file = Path(job_file).expanduser().resolve()
    job = load_job(requested_job_file)
    if requested_job_file != get_job_file(job).resolve():
        raise UserInputError("job file path does not match the Job's persisted identity")
    try:
        config = require_workflow_config(job)
    except ValueError as error:
        raise UserInputError("job has no persisted burned-video workflow goal") from error
    if not config.burn:
        raise UserInputError("job has no persisted burned-video goal")

    burned_artifacts = [artifact for artifact in job.artifacts if artifact.kind == "burned_video"]
    if len(burned_artifacts) != 1:
        raise UserInputError("burned video relink requires exactly one registered burned video")
    burned_artifact = burned_artifacts[0]
    if not burned_artifact.checksum:
        raise UserInputError("job has no checksummed burned video to relink")
    media_qa_failure_reason = media_qa_failure(job)
    if media_qa_failure_reason is not None:
        raise UserInputError(_RELINK_MEDIA_QA_MESSAGES[media_qa_failure_reason])

    if valid_artifact_path(job, "translated_cues") is None:
        raise UserInputError("burned video cannot be relinked without valid translated subtitles")
    proof = _burn_proof(job)
    if proof is None:
        raise UserInputError("burned video relink requires a checksum-valid v2 burn proof")
    # 与 status 侧的唯一差异是这里必须哈希并比对源媒体内容：写入登记前 fail closed。
    if not _burn_proof_fingerprint_matches(job, config, proof, require_source_media=True):
        raise UserInputError("burned video cannot be relinked because its fingerprint is stale")

    candidate_path = Path(candidate).expanduser()
    if not candidate_path.is_file():
        raise UserInputError(f"burned video candidate does not exist: {candidate_path}")
    candidate_path = candidate_path.resolve()
    candidate_checksum = file_checksum(candidate_path)
    if (
        burned_artifact.checksum != proof["burned_video_checksum"]
        or candidate_checksum != proof["burned_video_checksum"]
    ):
        raise UserInputError("burned video candidate checksum does not match the registered output")

    old_path = burned_artifact.path
    if candidate_path == Path(old_path).expanduser().resolve():
        return candidate_path
    job.artifacts = [
        Artifact("burned_video", str(candidate_path), burned_artifact.checksum)
        if artifact.kind == "burned_video"
        else artifact
        for artifact in job.artifacts
    ]
    save_job(job)
    if event_sink:
        fields = job_event_fields(job.id, get_job_file(job))
        event_sink(
            create_event(
                EventType.ARTIFACT_INVALIDATED,
                **fields,
                kind="burned_video",
                path=old_path,
            )
        )
        event_sink(
            create_event(
                EventType.ARTIFACT_CREATED,
                **fields,
                kind="burned_video",
                path=str(candidate_path),
            )
        )
    return candidate_path


def _current_artifacts(
    job: Job,
    *,
    waiting: bool = False,
    polishing: bool = False,
    semantic_reviewing: bool = False,
    retranslating: bool = False,
) -> tuple[Path, ...]:
    if waiting:
        if retranslating:
            kinds = ("cue_retranslation_batches",)
        elif semantic_reviewing:
            kinds = ("translation_semantic_qa", "translation_semantic_qa_final")
        elif polishing:
            kinds = ("translation_polish_batches",)
        else:
            kinds = ("translation_batches",)
    else:
        kinds = (
            "source_srt",
            "translated_srt",
            "bilingual_srt",
            "bilingual_ass",
            "burned_video",
        )
    return tuple(path for kind in kinds if (path := valid_artifact_path(job, kind)))


@dataclass(frozen=True, slots=True)
class WorkflowGoals:
    """端到端推进的用户目标（从 WorkflowConfig 派生）。

    权威字段仍是 ``target_language`` / ``burn``，与历史 Job 和 continue 语义一致。
    ``requested_outputs`` 目前是与这两项同步写入的派生清单，供文档与未来 planner
    消费；决策不单独依赖它，避免无语言却要求译文的不一致状态。
    """

    needs_translation: bool
    needs_burn: bool


def workflow_goals(config: WorkflowConfig) -> WorkflowGoals:
    return WorkflowGoals(
        needs_translation=bool(config.target_language),
        needs_burn=bool(config.burn),
    )


def _bilingual_ass_satisfied(job: Job) -> bool:
    """双语 ASS 交付物有效性：只对「曾经渲染过 ASS」的 Job 生效。

    渲染侧只在媒体有正整数宽高时写 ASS（见 ``pipeline/translation``
    的 ``_render_translated_outputs``），纯音频/字幕输入的 Job 从来不登记
    ``bilingual_ass``；对它们要求 ASS 会让 continue 永远收敛不了。因此这里以
    「Job 上是否登记过该 kind」为条件：登记过就必须 checksum 有效。
    """

    if not any(artifact.kind == "bilingual_ass" for artifact in job.artifacts):
        return True
    return valid_artifact_path(job, "bilingual_ass") is not None


def _translation_satisfied(job: Job, config: WorkflowConfig | None = None) -> bool:
    """译文目标完成：cue、用户可见的译文字幕文件与译文 QA 报告均 checksum 有效。

    QA 报告是 burn/preview 安全门的硬前置：门失去有效报告就会拒绝放行，因此它
    单独失效时必须被视为「译文目标未完成」，让 continue 能从现有译文 cue 零成本
    重建（重算 QA + 重渲染，不调用翻译 API）。否则「门让你跑 continue、continue
    说已完成」会闭环打死。

    ``bilingual_ass`` 同样是 README / ARCHITECTURE 列出的交付物，也是
    ``_current_artifacts`` 对外汇报的产物之一，所以它失效时也必须算「未完成」，
    否则删掉 bilingual.ass 后 continue/status/batch-continue 三路都会报完成、
    且没有任何命令能单独重建它。planner 据此给出 ``render``，走的是零 API 成本
    的重渲染路径。文档列产物时未按 burn 分叉，这里也不加 burn 守卫：burn=True
    的 Job 至多多渲染一次 ASS，代价为零且能把交付物补回来。
    """

    standard = (
        valid_artifact_path(job, "translated_cues") is not None
        and valid_artifact_path(job, "translated_srt") is not None
        and valid_artifact_path(job, "bilingual_srt") is not None
        and valid_artifact_path(job, "translated_subtitle_qa") is not None
        and _bilingual_ass_satisfied(job)
    )
    if not standard or config is None or config.translation_quality == "standard":
        return standard
    polished = translation_polish_satisfied(job) or agent_translation_polish_core_satisfied(job)
    return polished and semantic_review_satisfied(job)


def _burn_satisfied(job: Job, config: WorkflowConfig) -> bool:
    """烧录目标完成：成品 checksum 有效、媒体 QA 通过、派生指纹匹配。

    与 relink 两处的刻意差异：这里要求成品**有效**（relink 资格恰恰要求它已失效），
    不要求 ``translated_cues`` 有效（译文满足度由 ``_translation_satisfied`` 单独判），
    也不哈希源媒体内容。下面的 proof 分支同样刻意比 relink 宽松：没有 proof artifact
    或 proof 文档不是 v2 的 legacy Job 只认指纹匹配，不能因为缺 v2 proof 就判未完成
    （那会让所有 legacy Job 无条件重烧）。因此它不能复用
    ``_burn_proof_fingerprint_matches`` 的「proof 必须存在且匹配」口径。
    """

    burned_path = valid_artifact_path(job, "burned_video")
    if burned_path is None:
        return False
    if not media_qa_passed(job):
        return False
    if _stored_burn_fingerprint(job) != _burn_fingerprint(job, config):
        return False
    proof = _burn_proof(job)
    proof_path = valid_artifact_path(job, "burn_fingerprint")
    if proof_path is not None:
        try:
            proof_data = json.loads(proof_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return False
        if isinstance(proof_data, dict) and proof_data.get("version") == 2:
            if proof is None:
                return False
        else:
            return True
    if proof is None:
        return True
    burned_artifact = next(
        artifact for artifact in job.artifacts if artifact.kind == "burned_video"
    )
    return burned_artifact.checksum == proof["burned_video_checksum"]


def plan_advance_steps(job: Job, config: WorkflowConfig) -> tuple[str, ...]:
    """根据当前 Job 与持久化目标，列出仍需执行的推进步骤。

    返回值为有序步骤名：
    - ``await_cue_retranslation``：局部重译已进入 Agent 等待（可能叠加在一个端到端目标
      已经满足的 Job 上），调用方应停止并提示回填，不得按已完成处理
    - ``await_agent_translation``：已进入 Agent 等待，调用方应停止并提示回填
    - ``await_agent_polish``：润色已进入 Agent 等待，调用方应停止并提示回填
    - ``import_translation`` / ``translate``：补齐译文 cue
    - ``polish``：从有效 draft 继续润色
    - ``repair_polish_draft``：polished Job 的 draft 已失效，需要人工恢复
    - ``render``：已有译文 cue，补齐 SRT/ASS 渲染产物
    - ``burn``：补齐或重做烧录

    空元组表示翻译/烧录目标已满足（转写腿由 continue 在进入本函数前处理）。
    """

    # 必须是函数体的第一件事：原子重译等待叠加在一个「端到端目标可能已经满足」的 Job 上，
    # 不早退就会因 _translation_satisfied 为真而返回空计划，让 status 与 continue 一起
    # 报完成——而用户手里正握着一份未回填的 manifest。谓词是严格的（见
    # cue_retranslation_waiting），残留的过期 manifest 不会盖掉润色/语义等待。
    if cue_retranslation_waiting(job):
        return ("await_cue_retranslation",)

    goals = workflow_goals(config)
    steps: list[str] = []
    if goals.needs_translation and not _translation_satisfied(job, config):
        draft_path = valid_artifact_path(job, "translation_draft_cues")
        if config.translation_quality == "polished":
            polished_ready = translation_polish_satisfied(
                job
            ) or agent_translation_polish_core_satisfied(job)
            if polished_ready:
                if agent_semantic_review_waiting(job):
                    return ("await_agent_semantic_review",)
                if semantic_review_waiting(job):
                    return ("await_translation_review",)
                if semantic_review_approved(job):
                    steps.append("render")
                else:
                    steps.append("semantic_review")
                if goals.needs_burn and not _burn_satisfied(job, config):
                    steps.append("burn")
                return tuple(steps)
            if agent_translation_polish_progress_ready(job):
                steps.append("polish")
            elif agent_translation_polish_waiting(job):
                return ("await_agent_translation",)
            elif draft_path is None and agent_translation_polish_started(job):
                steps.append("translate")
            if steps:
                if goals.needs_burn and not _burn_satisfied(job, config):
                    steps.append("burn")
                return tuple(steps)
        polish_started = any(
            artifact.kind
            in {
                "translation_draft_cues",
                "translation_polish_batches",
                "translation_polish_progress",
                "translation_polished_cues",
            }
            for artifact in job.artifacts
        )
        if (
            config.translation_quality == "polished"
            and draft_path is None
            and (polish_started or valid_artifact_path(job, "translated_cues") is not None)
        ):
            return ("repair_polish_draft",)
        if config.translation_quality == "polished" and draft_path is not None:
            if (
                job.stages[StageName.TRANSLATE].status is StageStatus.AWAITING_INPUT
                and valid_artifact_path(job, "translation_polish_batches") is not None
                and agent_translation_polish_manifest_valid(job, config)
            ):
                return ("await_agent_polish",)
            steps.append("polish")
        elif (
            job.stages[StageName.TRANSLATE].status is StageStatus.AWAITING_INPUT
            and valid_artifact_path(job, "translation_batches") is not None
        ):
            # 必须排在下面的 translated_cues→render 之前：prepare_agent_translation 写出
            # 新的整轮请求时只把 TRANSLATE 置成 awaiting_input，不会失效旧译文（等待期间
            # 旧译文对用户仍有价值）。按 render 先判就会把「等 Agent 回填整轮译文」误报成
            # 「只差渲染」，统一 xvt apply 也因为拿不到 await_agent_translation 路由而拒收。
            # 批次 manifest 有效是必要条件（与 recovery 的等待态残留防御同口径）：manifest
            # 已经不在时没有任何响应能回填这次等待，那种 Job 仍按下面的既有分支处理。
            return ("await_agent_translation",)
        elif valid_artifact_path(job, "translated_cues") is not None:
            steps.append("render")
        else:
            # 这里会接住「TRANSLATE 停在 awaiting_input 但批次 manifest 已失效」的等待态残留：
            # 上面那条等待分支要求 manifest 有效，没有 manifest 时任何响应都回填不进这次等待，
            # 报等待就成了 status 说等回填 / apply 被 manifest 校验拒绝 / continue 只发 waiting
            # 的三方互指死局。与 recovery 的原子分支同口径，给出重建批次的可推进步骤。
            steps.append("import_translation" if config.translation_subtitles else "translate")
    if goals.needs_burn and not _burn_satisfied(job, config):
        steps.append("burn")
    return tuple(steps)


def planned_run_steps(options: WorkflowOptions) -> list[str]:
    """``xvt run`` 首跑的完整计划（含转写腿），用于 WORKFLOW_PLANNED 事件。"""

    steps = ["import_subtitles" if is_subtitle_input(options.input) else "transcribe"]
    if options.target_language:
        steps.append("import_translation" if options.translation_subtitles else "translate")
        if options.translation_quality == "polished":
            steps.append("polish")
            steps.append("semantic_review")
    if options.burn:
        steps.append("burn")
    return steps


_TRANSLATION_REVIEW_WAITING_REASON = "semantic review requires user acknowledgment"


def _translation_review_waiting_reason(job: Job) -> str:
    """人工语义校对等待的原因文案，直接取恢复计划的 reason，避免与真实阻断原因错位。

    ``build_recovery_plan`` 严格只读，这里只读它的结论。分类不属于语义校对等待
    （或 reason 为空）时退回英文常量，保证不会把无关分类的文案贴到等待事件上。
    """

    try:
        plan = build_recovery_plan(job)
    except (OSError, ValueError, KeyError, XvtError):
        # 等待事件不能因恢复判定失败而中断；但编程错误要照常抛出。
        return _TRANSLATION_REVIEW_WAITING_REASON
    if plan.category != "awaiting_input":
        return _TRANSLATION_REVIEW_WAITING_REASON
    return plan.reason or _TRANSLATION_REVIEW_WAITING_REASON


def _emit_waiting(
    *,
    job: Job,
    path: Path,
    message: str,
    artifacts: tuple[Path, ...],
    event_sink: EventSink | None,
) -> WorkflowResult:
    if event_sink:
        event_sink(
            create_event(
                EventType.WORKFLOW_WAITING,
                **job_event_fields(job.id, path),
                reason=message,
                artifacts=[str(item) for item in artifacts],
            )
        )
    return WorkflowResult("awaiting_input", path, artifacts, message)


def _ensure_translation(
    path: Path,
    job: Job,
    config: WorkflowConfig,
    *,
    event_sink: EventSink | None,
) -> WorkflowResult | None:
    """补齐译文。返回 WorkflowResult 表示应停止（Agent 等待）；None 表示已推进。"""

    target_language = config.target_language
    if not target_language:
        return None

    if config.translation_quality == "polished":
        polished_ready = translation_polish_satisfied(
            job
        ) or agent_translation_polish_core_satisfied(job)
        if not polished_ready:
            if agent_translation_polish_progress_ready(job):
                complete_agent_polished_outputs(path, event_sink=event_sink)
            elif agent_translation_polish_waiting(job):
                return _emit_waiting(
                    job=job,
                    path=path,
                    message="translate_and_polish requires Agent input",
                    artifacts=_current_artifacts(job, waiting=True),
                    event_sink=event_sink,
                )
            elif valid_artifact_path(job, "translation_draft_cues") is not None:
                polish = run_translation_polish(path, event_sink=event_sink)
                if polish.mode == "agent":
                    return _emit_waiting(
                        job=load_job(path),
                        path=path,
                        message=polish.fallback_reason or "translation polish requires Agent input",
                        artifacts=polish.paths,
                        event_sink=event_sink,
                    )
            else:
                invalid_agent_manifest = (
                    agent_translation_polish_started(job)
                    and not agent_translation_polish_waiting(job)
                    and not agent_translation_polish_progress_ready(job)
                )
                if (
                    job.stages[StageName.TRANSLATE].status is StageStatus.AWAITING_INPUT
                    and not invalid_agent_manifest
                ):
                    return _emit_waiting(
                        job=job,
                        path=path,
                        message="translation requires Agent input",
                        artifacts=_current_artifacts(job, waiting=True),
                        event_sink=event_sink,
                    )
                translation = run_translation(
                    path,
                    target_language=target_language,
                    provider=config.provider,
                    base_url=config.base_url,
                    model=config.model,
                    api_key_env=config.api_key_env,
                    batch_size=config.batch_size,
                    max_retries=config.max_retries,
                    timeout_seconds=config.timeout_seconds,
                    policy=resolved_translation_policy(job, config),
                    event_sink=event_sink,
                )
                if translation.mode == "agent":
                    return _emit_waiting(
                        job=load_job(path),
                        path=path,
                        message=translation.fallback_reason or "translation requires Agent input",
                        artifacts=translation.paths,
                        event_sink=event_sink,
                    )
                polish = run_translation_polish(path, event_sink=event_sink)
                if polish.mode == "agent":
                    return _emit_waiting(
                        job=load_job(path),
                        path=path,
                        message=polish.fallback_reason or "translation polish requires Agent input",
                        artifacts=polish.paths,
                        event_sink=event_sink,
                    )
            job = load_job(path)

        if agent_semantic_review_waiting(job):
            return _emit_waiting(
                job=job,
                path=path,
                message="semantic review requires Agent input",
                artifacts=_current_artifacts(job, waiting=True, semantic_reviewing=True),
                event_sink=event_sink,
            )
        if semantic_review_waiting(job):
            review_inputs = ensure_semantic_review_inputs(path, event_sink=event_sink)
            # ensure_semantic_review_inputs 只落盘 review 文件，不 save_job；
            # 重新载入是为了拿到与磁盘一致的 job 状态再派生等待原因。
            job = load_job(path)
            return _emit_waiting(
                job=job,
                path=path,
                message=_translation_review_waiting_reason(job),
                artifacts=(
                    *_current_artifacts(job, waiting=True, semantic_reviewing=True),
                    *review_inputs,
                ),
                event_sink=event_sink,
            )
        if semantic_review_approved(job):
            complete_acknowledged_semantic_review(path, event_sink=event_sink)
            return None
        semantic = run_translation_semantic_review(
            path,
            config=config,
            event_sink=event_sink,
        )
        if semantic.status != "completed":
            reviewed_job = load_job(path)
            return _emit_waiting(
                job=reviewed_job,
                path=path,
                message=(
                    "semantic review requires Agent input"
                    if semantic.status == "awaiting_agent"
                    else _translation_review_waiting_reason(reviewed_job)
                ),
                artifacts=semantic.paths,
                event_sink=event_sink,
            )
        return None

    # Standard quality: cues 已落盘但缺 SRT 时只补 QA+渲染，禁止再次调用翻译 API。
    if valid_artifact_path(job, "translated_cues") is not None:
        complete_translated_outputs(path, event_sink=event_sink)
        return None

    # 与 planner 的等待分支同口径：manifest 已失效时这次等待没有任何可回填的响应，
    # 必须落到下面的 run_translation 重建批次，否则 continue 只会反复发 waiting。
    if (
        job.stages[StageName.TRANSLATE].status is StageStatus.AWAITING_INPUT
        and valid_artifact_path(job, "translation_batches") is not None
    ):
        return _emit_waiting(
            job=job,
            path=path,
            message="translation requires Agent input",
            artifacts=_current_artifacts(job, waiting=True),
            event_sink=event_sink,
        )

    translation_subtitles = (
        valid_artifact_path(job, "input_translation_subtitles") or config.translation_subtitles
    )
    if translation_subtitles:
        apply_subtitle_translation(
            path,
            translation_subtitles,
            target_language=target_language,
            event_sink=event_sink,
        )
        return None

    translation = run_translation(
        path,
        target_language=target_language,
        provider=config.provider,
        base_url=config.base_url,
        model=config.model,
        api_key_env=config.api_key_env,
        batch_size=config.batch_size,
        max_retries=config.max_retries,
        timeout_seconds=config.timeout_seconds,
        policy=resolved_translation_policy(job, config),
        event_sink=event_sink,
    )
    if translation.mode == "agent":
        return _emit_waiting(
            job=job,
            path=path,
            message=translation.fallback_reason or "translation requires Agent input",
            artifacts=translation.paths,
            event_sink=event_sink,
        )
    return None


def _ensure_burn(
    path: Path,
    job: Job,
    config: WorkflowConfig,
    *,
    cookies_from_browser: str | None,
    proxy: str | None,
    event_sink: EventSink | None,
) -> None:
    burn_translated_job(
        path,
        media=config.burn_media,
        output=config.burn_output,
        hardware_encoding=config.hardware_encoding,
        cookies_from_browser=cookies_from_browser or job.config.get("cookies_from_browser"),
        proxy=proxy,
        target_font=config.target_font,
        source_font=config.source_font,
        persist_workflow_qa=True,
        event_sink=event_sink,
    )
    _record_burn_fingerprint(path, event_sink=event_sink)


def _advance_workflow(
    path: Path,
    *,
    config: WorkflowConfig | None = None,
    cookies_from_browser: str | None,
    proxy: str | None,
    event_sink: EventSink | None,
) -> WorkflowResult:
    """按持久化目标推进翻译与烧录，只补齐缺失或失效的产物。

    run 首跑与 continue 续跑共用本函数：转写完成后的所有推进决策（Agent 等待、
    译文回填、指纹感知的烧录复用）只有这一份实现。``config`` 为 None 时从
    Job 读取持久化目标（continue 路径）；run 首跑直接传入刚构造的同一份配置。

    满足度判定与 ``plan_advance_steps`` 共用 ``workflow_goals`` /
    ``_translation_satisfied`` / ``_burn_satisfied``。翻译成功后必须重新判定烧录
    （译文 checksum 变化会使旧指纹失效）；因此执行路径不会「冻结」入口时刻的 plan。
    """
    job = load_job(path)
    if config is None:
        config = require_workflow_config(job)
    goals = workflow_goals(config)

    # 与 planner 共用等待判定：Agent 等待是终态快照，不得继续烧录。
    advance_steps = plan_advance_steps(job, config)
    first_step = advance_steps[0] if advance_steps else None
    # 只认「下面两条分支真的会接住」的等待。await_cue_retranslation 之外的等待步骤都产自
    # plan_advance_steps 里被 needs_translation 守卫的那段，这里同口径复述一次守卫，免得
    # 将来放宽 planner 时出现「吞掉了 QA 引导又没人返回等待」的静默落空。
    waiting_step = (
        first_step
        if first_step in AWAIT_STEPS
        and (goals.needs_translation or first_step == "await_cue_retranslation")
        else None
    )

    # 字幕 QA 引导必须给在途 Agent 等待让路，且判定顺序要与 build_recovery_plan 一致
    # （那边同样是「等待优先于 qa_blocked」）。SUBTITLE_QA 停在 FAILED 时旁边完全可能挂着
    # 一次仍然有效的等待：术语死锁的出口就是重译，回填前 QA 不会重跑；用户改走整轮重翻时
    # prepare_agent_translation 也只置 TRANSLATE=awaiting_input，不复位 SUBTITLE_QA。
    # 先判引导会让 status 报「去回填」而 continue 抛错要求重跑，batch-continue 据此把条目
    # 误记成 failed 而不是停在 awaiting_input。
    if waiting_step is None:
        guidance = subtitle_qa_failure_guidance(job, path)
        if guidance is not None:
            if guidance.is_needs_review:
                return _emit_waiting(
                    job=job,
                    path=path,
                    message=guidance.message,
                    artifacts=tuple(Path(item) for item in guidance.report_paths),
                    event_sink=event_sink,
                )
            raise RuntimeError(guidance.message)

    # 必须排在下面那条被 goals.needs_translation 守卫的等待分支之前：局部重译可以发生在
    # workflow.target_language 为 None 的 Job 上（run 时没给目标语言，后来用原子
    # translate 补翻），那种 Job 走不进 needs_translation 分支，会一路落到「已完成」。
    if waiting_step == "await_cue_retranslation":
        return _emit_waiting(
            job=job,
            path=path,
            message="cue retranslation requires Agent input",
            artifacts=_current_artifacts(job, waiting=True, retranslating=True),
            event_sink=event_sink,
        )
    if goals.needs_translation and waiting_step is not None:
        polishing = advance_steps[0] == "await_agent_polish"
        semantic_reviewing = advance_steps[0] in {
            "await_agent_semantic_review",
            "await_translation_review",
        }
        waiting_artifacts = _current_artifacts(
            job,
            waiting=True,
            polishing=polishing,
            semantic_reviewing=semantic_reviewing,
        )
        if advance_steps[0] == "await_translation_review":
            waiting_artifacts = (
                *waiting_artifacts,
                *ensure_semantic_review_inputs(path, event_sink=event_sink),
            )
            # ensure_semantic_review_inputs 只落盘 review 文件，不 save_job；
            # 重新载入是为了拿到与磁盘一致的 job 状态再派生等待原因。
            job = load_job(path)
        return _emit_waiting(
            job=job,
            path=path,
            message=(
                _translation_review_waiting_reason(job)
                if advance_steps[0] == "await_translation_review"
                else "semantic review requires Agent input"
                if semantic_reviewing
                else "translation polish requires Agent input"
                if polishing
                else "translation requires Agent input"
            ),
            artifacts=waiting_artifacts,
            event_sink=event_sink,
        )

    # planner 的 repair_polish_draft 表示「draft 失效需人工恢复」。执行侧此前不拦截，
    # 直接落到 _ensure_translation 的 run_translation，在 checkpoint 也失效时会静默
    # 全量重翻 + 重润色（真实 API 成本）。只有 checkpoint 能零成本重建时才放行。
    if (
        goals.needs_translation
        and advance_steps
        and advance_steps[0] == "repair_polish_draft"
        and not translation_progress_rebuildable(job, config)
    ):
        raise RuntimeError(REPAIR_POLISH_DRAFT_MANUAL_FIX_MESSAGE)

    if goals.needs_translation:
        if not _translation_satisfied(job, config):
            waiting = _ensure_translation(path, job, config, event_sink=event_sink)
            if waiting is not None:
                return waiting
            job = load_job(path)
            guidance = subtitle_qa_failure_guidance(job, path)
            if guidance is not None:
                if guidance.is_needs_review:
                    return _emit_waiting(
                        job=job,
                        path=path,
                        message=guidance.message,
                        artifacts=tuple(Path(item) for item in guidance.report_paths),
                        event_sink=event_sink,
                    )
                raise RuntimeError(guidance.message)
        # 字幕 QA needs_review 已被确认且译文产物齐备时，清掉确认前留下的过期
        # translate 失败记录；旧 Job 也在这里收口。
        if finalize_acknowledged_subtitle_review(path, event_sink=event_sink):
            job = load_job(path)

    # 翻译副作用之后重新判定：不可使用入口处缓存的 plan 步骤表。
    if goals.needs_burn and not _burn_satisfied(job, config):
        try:
            _ensure_burn(
                path,
                job,
                config,
                cookies_from_browser=cookies_from_browser,
                proxy=proxy,
                event_sink=event_sink,
            )
        except UserInputError:
            refreshed = load_job(path)
            if refreshed.stages[StageName.SUBTITLE_QA].status is not StageStatus.AWAITING_INPUT:
                raise
            return _emit_waiting(
                job=refreshed,
                path=path,
                message="字幕布局需要确认；运行 continue --acknowledge-subtitle-review 后继续。",
                artifacts=_current_artifacts(refreshed),
                event_sink=event_sink,
            )
        job = load_job(path)

    # 终检守卫（fail closed）：执行侧自认为已完成，但 planner 还能列出步骤时，
    # 说明两套判定对同一份 Job 状态给出了相反结论——这是内部不一致，不能发
    # workflow_completed 后让 status / batch 以为任务结束（不可破坏约束第 4 条与
    # 「失败路径不会误报 workflow_completed」）。刻意不降级成警告：误报完成会让
    # 批处理把条目记成 done，用户再也不会回来看它。
    #
    # 与 _current_artifacts 共用一个只读 checksum 作用域：两者都会校验 burned_video，
    # 不共用就要把成品（可能是 GB 级）哈希两遍。作用域内不写任何产物。
    with readonly_checksum_scope():
        remaining = plan_advance_steps(job, config)
        if remaining:
            raise InternalError(
                "workflow reported completion while the planner still lists advance steps: "
                + ", ".join(remaining)
            )
        artifacts = _current_artifacts(job)
    if event_sink:
        event_sink(
            create_event(
                EventType.WORKFLOW_COMPLETED,
                **job_event_fields(job.id, path),
                artifacts=[str(item) for item in artifacts],
            )
        )
    return WorkflowResult("completed", path, artifacts)


def run_workflow(
    options: WorkflowOptions,
    *,
    event_sink: EventSink | None = None,
) -> WorkflowResult:
    options.validate()
    # validate 已保证 glossary 与 translation_subtitles 互斥，这里只按有无目标语言决定是否
    # 落策略快照。导入译文时 terms 必为空（glossary 传不进来），但 style 仍要落盘：后续
    # retranslate-cues / polish-translation 读的是同一份快照，用户在 run 时选的风格不应凭空消失。
    policy = (
        load_translation_policy(options.glossary, style=options.translation_style)
        if options.target_language
        else None
    )
    steps = planned_run_steps(options)
    if event_sink:
        event_sink(create_event(EventType.WORKFLOW_STARTED, **job_event_fields(None, None)))
        event_sink(create_event(EventType.WORKFLOW_PLANNED, steps=steps))

    job_file: Path | None = None
    job_id: str | None = None
    workflow_config = options.persisted_config(policy)
    try:
        job = run_transcription(
            options.input,
            options.output,
            engine=options.engine,
            language=options.language,
            subtitles=options.subtitles,
            source_strategy=options.source_strategy,
            align_subtitles=options.align_subtitles,
            minimum_alignment_confidence=options.minimum_alignment_confidence,
            allow_low_confidence_alignment=options.allow_low_confidence_alignment,
            segmentation_preset=options.segmentation_preset,
            cookies_from_browser=options.cookies_from_browser,
            proxy=options.proxy,
            youtube_subtitles=options.youtube_subtitles,
            subtitle_language=options.subtitle_language,
            allow_auto_subtitles=options.allow_auto_subtitles,
            workflow_config=workflow_config.to_dict(),
            event_sink=event_sink,
            emit_job_completed=not options.target_language,
        )
        job_file = get_job_file(job)
        job_id = job.id
        return _advance_workflow(
            job_file,
            config=workflow_config,
            cookies_from_browser=options.cookies_from_browser,
            proxy=options.proxy,
            event_sink=event_sink,
        )
    except Exception as error:
        if event_sink:
            event_sink(
                create_event(
                    EventType.WORKFLOW_FAILED,
                    **job_event_fields(job_id, job_file),
                    **error_event_fields(error_category(error), str(error)),
                )
            )
        raise


def continue_workflow(
    job_file: str | Path,
    *,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    event_sink: EventSink | None = None,
) -> WorkflowResult:
    path = Path(job_file).expanduser().resolve()
    job = load_job(path)
    require_workflow_config(job)

    if event_sink:
        event_sink(
            create_event(
                EventType.WORKFLOW_STARTED,
                **job_event_fields(job.id, path),
                continuation=True,
            )
        )
    try:
        # source_srt 缺失说明转写腿在 QA/渲染处中断（例如 QA needs_review 后
        # 被人工确认放行）：与缺 source_cues 一样先补齐转写腿再推进。
        source_cues = valid_artifact_path(job, "source_cues")
        source_srt = valid_artifact_path(job, "source_srt")
        source_selection_missing = "source_strategy" in job.config and not source_selection_matches(
            job
        )
        if source_cues is None or source_srt is None or source_selection_missing:
            if source_cues is not None:
                resume_job(path, event_sink=event_sink)
            else:
                parsed_input = urlparse(job.input)
                media_info = valid_artifact_path(job, "media_info")
                transcription_checkpoint = valid_artifact_path(
                    job, "word_transcript"
                ) or valid_artifact_path(job, "normalized_audio")
                if (
                    parsed_input.scheme in {"http", "https"}
                    and parsed_input.hostname
                    and not (media_info and transcription_checkpoint)
                ):
                    resume_remote_transcription(
                        path,
                        cookies_from_browser=cookies_from_browser,
                        proxy=proxy,
                        event_sink=event_sink,
                    )
                else:
                    resume_job(path, event_sink=event_sink)

        return _advance_workflow(
            path,
            cookies_from_browser=cookies_from_browser,
            proxy=proxy,
            event_sink=event_sink,
        )
    except Exception as error:
        if event_sink:
            event_sink(
                create_event(
                    EventType.WORKFLOW_FAILED,
                    **job_event_fields(job.id, path),
                    **error_event_fields(error_category(error), str(error)),
                )
            )
        raise
