from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
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
    agent_translate_and_polish_core_satisfied,
    agent_translate_and_polish_progress_ready,
    agent_translate_and_polish_started,
    agent_translate_and_polish_waiting,
    agent_translation_polish_manifest_valid,
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
    pending_translation_import_blocker,
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
    set_workflow_config,
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

# 「在途等待优先」的表态表。本仓库已经四次踩同一类缺陷：一个真实的在途等待被某个
# 「已满足」判定盖掉（qa_blocked、F3 manifest 失效死局、F4 原子分支让路、整轮 Agent
# 翻译等待）。收敛手段是让判定顺序变成必须表态的事情：
#
# - 不在本集合里的等待步骤，必须由 ``plan_advance_steps`` 在 ``_translation_satisfied``
#   判定**之前**早退给出（目前是 ``await_cue_retranslation`` 与
#   ``await_agent_translation``），这样它们不会被「译文目标已满足」短路成空计划。
# - 本集合里的等待步骤刻意只在「译文目标未满足」的分支里判定，理由见下面各条注释。
#
# 新增等待步骤时必须二选一：要么写一条满足度判定之前的早退，要么登记进本集合并说明
# 理由。``tests/test_workflow.py::AwaitStepsCoverageTest`` 用 AST 强制这件事。
_SATISFIED_GATED_AWAIT_STEPS: frozenset[str] = frozenset(
    {
        # 润色与语义校对的产物（polished cues、语义报告与确认）本身就是 polished 质量下
        # 译文满足度的组成部分，正常流程里「满足」与「自己那次等待仍挂着」互斥，因此
        # 放在满足度之后判定不会丢等待。已知遗留：用户在一个已满足的 polished Job 上
        # 显式重开 polish-translation / 语义校对的 Agent 等待时，计划仍会是空的
        # （见 docs/RECOVERY.md「端到端推进计划的判定顺序」）。
        "await_agent_polish",
        "await_agent_semantic_review",
        "await_translation_review",
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


def _burn_media_value(job: Job, config: WorkflowConfig) -> str:
    """烧录所用媒体的来源值：持久化的 ``burn_media`` 优先，回退 Job 原始输入。"""

    return str(config.burn_media or job.input)


def _burn_media_is_remote(media_value: str) -> bool:
    """媒体来源是否是远程 URL（本地路径与远程 URL 的身份口径不同）。"""

    parsed = urlparse(media_value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _burn_media_identity(job: Job, config: WorkflowConfig) -> object | None:
    """源媒体在烧录指纹里的身份；本地媒体不在原路径时返回 None。

    本地媒体刻意用 ``{path, size, mtime_ns}`` 而不是内容 checksum：这是 ``status``
    的成本边界（见 ``docs/RECOVERY.md``），不为每次查询读整个视频。元数据漂移带来的
    「内容其实没变却判失效」由 ``_burn_fingerprint_satisfied`` 的内容 checksum 回退
    兜住，不在这里改身份口径——改了会让全部存量 v2 proof 一次性失效。
    """

    media_value = _burn_media_value(job, config)
    if _burn_media_is_remote(media_value):
        return artifact_checksum_for(job, "source_video") or media_value
    media_path = Path(media_value).expanduser()
    if not media_path.is_file():
        return None
    stat = media_path.stat()
    return {
        "path": str(media_path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _burn_fingerprint_for_media_identity(
    job: Job,
    config: WorkflowConfig,
    media_identity: object,
) -> str | None:
    """按给定的源媒体身份算出烧录派生指纹（其余输入一律取当前 Job / config）。

    指纹载荷的格式是全部存量 v2 proof 的比对基准，**不得改动**。把媒体身份抽成参数
    只是为了让「用 proof 里登记的烧录时身份重算一次」成为可能：重算结果仍等于已登记
    指纹，就证明指纹差异**只**来自媒体身份，译文 cue、字体与编码选项都没有漂移。
    """

    translated_checksum = artifact_checksum_for(job, "translated_cues")
    if not translated_checksum:
        return None
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


def _burn_fingerprint(job: Job, config: WorkflowConfig) -> str | None:
    media_identity = _burn_media_identity(job, config)
    if media_identity is None:
        return None
    return _burn_fingerprint_for_media_identity(job, config, media_identity)


def _burn_source_media_checksum(job: Job, config: WorkflowConfig) -> str | None:
    media_value = _burn_media_value(job, config)
    if _burn_media_is_remote(media_value):
        media_path = valid_artifact_path(job, "source_video")
        return artifact_checksum_for(job, "source_video") if media_path is not None else None
    media_path = Path(media_value).expanduser()
    return file_checksum(media_path) if media_path.is_file() else None


def missing_burn_source_media(
    job: Job,
    config: WorkflowConfig,
    *,
    local_only: bool = False,
) -> Path | None:
    """烧录所需源媒体当前不在它应在的位置时返回该位置，否则返回 None。

    只 ``stat``，**不哈希内容**：调用方（`status` 导航与 relink 的前置检查）要的是
    「这个文件还在不在」，不是「内容对不对」。内容比对仍归
    ``_burn_fingerprint_satisfied`` / ``_burn_proof_fingerprint_matches``。

    ``local_only`` 是两个调用点刻意的差异：recovery 侧只认本地媒体，因为远程 Job
    的下载产物缺失时 ``continue`` 能重新获取，那是真出口，不该归 ``manual_fix``；
    relink 侧两种都点名，它对两者都已经 fail closed。远程 Job 从未登记过
    ``source_video`` 时返回 None——没有可点名的位置，不猜。
    """

    media_value = _burn_media_value(job, config)
    if _burn_media_is_remote(media_value):
        if local_only:
            return None
        registered = next(
            (artifact.path for artifact in job.artifacts if artifact.kind == "source_video"),
            None,
        )
        if registered is None:
            return None
        remote_path = Path(registered).expanduser()
        return None if remote_path.is_file() else remote_path
    media_path = Path(media_value).expanduser()
    return None if media_path.is_file() else media_path


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


#: v2 proof 里「烧录时源媒体身份」的键。**可选字段**：存量 v2 proof 没有它，照旧只
#: 走快路径（fail closed，拿不到内容 checksum 回退），下一次重烧即补上。只对本地媒体
#: 登记——远程媒体的身份本来就是内容 checksum，不含 mtime，不会假失效；顺带也不把
#: 媒体 URL 再抄一份进产物。
_BURN_PROOF_MEDIA_IDENTITY = "source_media_identity"


def _burn_proof(job: Job) -> dict[str, Any] | None:
    """读取 checksum 有效的 v2 烧录证明记录。

    三个必需字段仍按原口径校验（缺任一即视作无 proof）；``source_media_identity``
    是可选字段，形状不对就当它不存在，不因此否掉整份 proof——否则一份身份字段被改坏
    的 proof 会连既有的快路径完成语义一起失效。
    """

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
    proof: dict[str, Any] = {field: data[field] for field in fields}
    identity = data.get(_BURN_PROOF_MEDIA_IDENTITY)
    if _valid_burn_media_identity(identity):
        proof[_BURN_PROOF_MEDIA_IDENTITY] = identity
    return proof


def _valid_burn_media_identity(identity: object) -> bool:
    """proof 里的本地媒体身份是否形状完好（与 ``_burn_media_identity`` 同构）。"""

    if not isinstance(identity, dict) or set(identity) != {"path", "size", "mtime_ns"}:
        return False
    # bool 是 int 的子类；JSON 里不会出现，但别让它混进指纹载荷。
    return (
        isinstance(identity["path"], str)
        and bool(identity["path"])
        and isinstance(identity["size"], int)
        and not isinstance(identity["size"], bool)
        and isinstance(identity["mtime_ns"], int)
        and not isinstance(identity["mtime_ns"], bool)
    )


def _burn_fingerprint_satisfied(job: Job, config: WorkflowConfig) -> bool:
    """当前烧录派生指纹是否仍然成立（烧录满足度与 proof 比对共用的**唯一**判定）。

    **快路径**：已登记指纹与按 ``{path, size, mtime_ns}`` 现算的指纹逐字节相等。常见
    情形走这里，零额外 IO，``status`` 的成本边界不变。

    **内容 checksum 回退**：快路径判不匹配时，再查一次「源媒体内容是不是其实没变」。
    不带 ``-p`` 的 ``cp``、不带 ``-a`` 的 ``rsync``、备份还原和一次 ``touch`` 都只动
    文件元数据，此前它们会让一个内容上完全有效的完成态被判失效，代价是整段重编码。
    回退只在这条**本来就要报失效**的分支上多哈希一次源媒体——那条分支的后果是重新
    编码整部视频，一次哈希远比它便宜。

    回退要求三条**全部**成立，否则照旧判失效（fail closed）：

    1. 存在 checksum 有效的 v2 proof，且 proof 的 ``fingerprint`` 就是已登记那份
       （legacy fingerprint 拿不到这条回退）；
    2. proof 里登记了烧录时的源媒体身份，用它和**当前**其余输入重算出的指纹仍等于
       已登记指纹——这一步证明差异**只**来自媒体身份。少了它，手改 ``job.json`` 换
       字体或编码选项后 ``continue`` 会被误判成已完成，新设置永远烧不上去；
    3. 源媒体当前内容 checksum 等于 proof 登记的那份内容 checksum。
    """

    stored_fingerprint = _stored_burn_fingerprint(job)
    if stored_fingerprint is None:
        return False
    if _burn_fingerprint(job, config) == stored_fingerprint:
        return True
    proof = _burn_proof(job)
    if proof is None or proof["fingerprint"] != stored_fingerprint:
        return False
    identity = proof.get(_BURN_PROOF_MEDIA_IDENTITY)
    if identity is None:
        return False
    if _burn_fingerprint_for_media_identity(job, config, identity) != stored_fingerprint:
        return False
    source_media_checksum = _burn_source_media_checksum(job, config)
    return source_media_checksum is not None and (
        source_media_checksum == proof["source_media_checksum"]
    )


def _burn_proof_fingerprint_matches(
    job: Job,
    config: WorkflowConfig,
    proof: dict[str, Any],
    *,
    require_source_media: bool,
) -> bool:
    """v2 proof 的指纹（可选含源媒体内容 checksum）是否与当前 Job 匹配。

    ``require_source_media`` 是两个调用点**刻意**的差异，不是重复：
    ``status`` 侧（``burned_output_relink_recovery_available``）不哈希源媒体，免得
    每次 status 都读一遍大文件；真正改写登记的 ``relink_burned_output`` 必须哈希并
    比对，否则源媒体被同 size+mtime 的内容替换时会放过一次应有的重烧。

    指纹本身的判定复用 ``_burn_fingerprint_satisfied``（含内容 checksum 回退），
    不在这里重造。回退路径自己已经哈希并比对过内容 checksum，因此那条分支不再为
    ``require_source_media`` 重复算一次——两条路径合起来至多哈希源媒体一次。

    ``burned_video`` 的 checksum 比对**不在**这里：两个调用点对它的时机与报错文案
    不同（relink 要先确认候选文件存在、再把候选与登记值一起比，报「候选 checksum
    不匹配」），放进来只会逼出一个带标签的返回值。
    """

    stored_fingerprint = _stored_burn_fingerprint(job)
    if stored_fingerprint is None or proof["fingerprint"] != stored_fingerprint:
        return False
    if _burn_fingerprint(job, config) != stored_fingerprint:
        # 走回退：它已经比对过源媒体内容 checksum，对两个调用点都够。
        return _burn_fingerprint_satisfied(job, config)
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
    media_identity = _burn_media_identity(job, config)
    fingerprint = (
        None
        if media_identity is None
        else _burn_fingerprint_for_media_identity(job, config, media_identity)
    )
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
    document: dict[str, Any] = {
        "version": 2,
        "fingerprint": fingerprint,
        "burned_video_checksum": burned_video.checksum,
        "source_media_checksum": source_media_checksum,
    }
    # 烧录时的本地媒体身份是「指纹不匹配时的内容 checksum 回退」的授权依据：没有它就
    # 无法证明指纹差异只来自文件元数据，手改字体/编码选项会被误判成「已完成」。
    if _valid_burn_media_identity(media_identity):
        document[_BURN_PROOF_MEDIA_IDENTITY] = media_identity
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
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
    # 源媒体不在原路径时指纹根本算不出来（``_burn_fingerprint`` 返回 None），下面的
    # 比对会一路落到「指纹过期」——那是错误归因：指纹没过期，是拿不到源媒体。先把
    # 这一类分出来并点名缺失的路径。方向不变（本来就拒绝），只是说对了原因。
    missing_media = missing_burn_source_media(job, config)
    if missing_media is not None:
        raise UserInputError(
            f"burned video cannot be relinked while its source media is missing: {missing_media}"
        )
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


#: ``relink-source-media`` 的 Job 侧前置条件消息表。文案逐字沿用命令原有的报错，
#: 收敛成表只是为了让「命令」与「导航谓词」共用同一份判定。
_SOURCE_MEDIA_RELINK_BLOCKER_MESSAGES = {
    "goal": "job has no persisted burned-video goal",
    "remote": (
        "source media relink does not apply to a remote media job; its media is a "
        "download registered inside the Job, not a path on your filesystem"
    ),
    "proof": "source media relink requires a checksum-valid v2 burn proof",
}


def source_media_relink_blocker(job: Job, config: WorkflowConfig) -> str | None:
    """``relink-source-media`` 在这个 Job 上的**阻塞原因**；会走到候选校验时返回 None。

    判定的是 Job 侧（与候选文件无关）的那三条前置条件，标签供调用方派生各自的出口：

    - ``"goal"``：没有持久化的烧录目标
    - ``"remote"``：媒体是远程 Job 内登记的下载产物，"搬家" 不适用
    - ``"proof"``：没有 checksum 有效的 v2 burn proof，没有可比对的基线

    形态照 ``media_qa_failure`` / ``media_qa_passed``：命令本体消费标签取报错文案，
    ``recovery`` 消费布尔包装决定要不要把这条命令放进推荐位。**两侧必须同口径**——
    只把 ``_burn_proof`` 暴露出去会逼 ``recovery`` 在调用点重造另外两条前置条件，
    于是命令收紧时导航不会跟着动，下一次「推荐一条在该状态下必然失败的命令」就从
    那里长出来（本仓库此类缺陷已经出现过六次）。
    """

    if not config.burn:
        return "goal"
    if _burn_media_is_remote(_burn_media_value(job, config)):
        return "remote"
    if _burn_proof(job) is None:
        return "proof"
    return None


def source_media_relink_available(job: Job, config: WorkflowConfig) -> bool:
    """``relink-source-media`` 在这个 Job 上够不够格（不关心阻塞原因）。"""

    return source_media_relink_blocker(job, config) is None


def relink_source_media(job_file: str | Path, media: str | Path) -> Path:
    """把持久化的烧录源媒体路径指到一份**内容完全相同**的现存文件（源媒体搬家）。

    为什么需要它：源媒体永久换位置（路径变了、内容没变）之后，状态机本来就处理得了
    这件事——``_burn_fingerprint_satisfied`` 的内容 checksum 回退在「差异只来自媒体
    身份、内容 checksum 仍等于 proof 登记那份」时照旧判指纹成立，于是 Job 零重烧回到
    完成。缺的只是一个受支持的方式去改那个路径：此前唯一的办法是手改 ``job.json``。
    ``recovery`` 的源媒体缺失分支已经把这条命令放进推荐位，但**只在它会成功时**——
    资格判定见 ``source_media_relink_blocker`` / ``source_media_relink_available``，
    本函数与那条导航共用同一份实现。

    与 ``relink_burned_output`` 的同与不同：

    - **同**：fail closed，只认内容 checksum。新文件的内容 checksum 必须等于 v2 proof
      登记的 ``source_media_checksum``；没有 checksum 有效的 v2 proof 就没有可比对的
      基线，证明不了是同一份媒体，一律拒绝（legacy fingerprint 不给这条出口）。
      任一校验不过时 Job 一个字节都不改。
    - **不同**：这里只写 ``config.workflow.burn_media`` 一个字段。不重烧、不碰 artifact
      登记、不改 stage，因此**不发 artifact 事件**——借用 ``artifact_invalidated`` /
      ``artifact_created`` 会谎报产物变动。改动落在 ``job.json``（恢复依据本身），
      JSONL 模式下由 CLI 的 ``cli_completed`` 带出新路径。

    刻意不要求源媒体「当前确实缺失」：指到另一份内容相同的副本同样安全（烧录结果本来
    就一致），多一道状态判定只会多一条没有必要的拒绝路径。

    存量 v2 proof（没有可选的 ``source_media_identity``）同样放行：授权依据是 v2 的
    **必需**字段 ``source_media_checksum``，匹配即证明是同一份媒体，写入是正确的。
    零重烧是回退判定的性质而不是本命令的承诺——缺身份字段时回退拿不到授权，Job 落到
    ``burn`` 重烧一次（新 proof 随即补上身份字段），那仍然是用户能真正推进的出口。

    远程 Job 不支持：媒体是 Job 内登记的下载产物（``source_video``），它在指纹里的身份
    本就是内容 checksum，不含路径；"搬家" 这个概念不适用，而把 ``burn_media`` 指到一个
    本地路径还会把媒体身份从「内容 checksum」口径换成「``{path, size, mtime_ns}``」
    口径，指纹必然漂移、反而逼出一次重烧。下载产物自身缺失时 ``continue`` 能重新获取。
    """

    requested_job_file = Path(job_file).expanduser().resolve()
    job = load_job(requested_job_file)
    if requested_job_file != get_job_file(job).resolve():
        raise UserInputError("job file path does not match the Job's persisted identity")
    try:
        config = require_workflow_config(job)
    except ValueError as error:
        raise UserInputError("job has no persisted burned-video workflow goal") from error
    blocker = source_media_relink_blocker(job, config)
    if blocker is not None:
        raise UserInputError(_SOURCE_MEDIA_RELINK_BLOCKER_MESSAGES[blocker])
    # 谓词放行即保证这三者成立；proof 在这里只是再取一次登记值，不重判。
    proof = _burn_proof(job)
    assert proof is not None

    media_path = Path(media).expanduser()
    if media_path.is_dir():
        raise UserInputError(f"source media candidate is not a file: {media_path}")
    if not media_path.is_file():
        raise UserInputError(f"source media candidate does not exist: {media_path}")
    media_path = media_path.resolve()
    if file_checksum(media_path) != proof["source_media_checksum"]:
        raise UserInputError(
            "source media candidate checksum does not match the burn proof's source media"
        )
    if media_path == Path(_burn_media_value(job, config)).expanduser().resolve():
        return media_path

    set_workflow_config(job, replace(config, burn_media=str(media_path)))
    save_job(job)
    return media_path


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
    polished = translation_polish_satisfied(job) or agent_translate_and_polish_core_satisfied(job)
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
    if not _burn_fingerprint_satisfied(job, config):
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


def agent_full_translation_waiting(job: Job) -> bool:
    """整轮 Agent 翻译是否真的停在一次仍挂得住的等待上。

    两条件：``TRANSLATE`` 停在 ``AWAITING_INPUT``，且 ``translation_batches``
    checksum 有效。manifest 失效时刻意判否——没有 manifest 就没有任何响应能回填这次
    等待，报等待会让 ``status`` 说等回填、``xvt apply`` 被 manifest 校验拒绝、
    ``continue`` 只发一次 waiting，三方互指没有出口（见
    ``docs/RECOVERY.md`` 的「端到端推进计划的判定顺序」第 3 条）。

    单点实现：``plan_advance_steps`` 的两处分支（满足度之前的早退与满足度之内的既有
    分支）与 ``recovery`` 原子 Job 的 ``qa_blocked`` 让路都以这里为准，新增调用点不要
    就地重造这两个条件。
    """

    return (
        job.stages[StageName.TRANSLATE].status is StageStatus.AWAITING_INPUT
        and valid_artifact_path(job, "translation_batches") is not None
    )


# 被一次新的整轮翻译请求取代的在途请求/报告。整轮 manifest 登记更晚即「整轮更新」。
_SUPERSEDED_BY_FULL_TRANSLATION: tuple[str, ...] = (
    "cue_retranslation_batches",
    "translation_polish_batches",
    "translation_semantic_qa",
    "translation_semantic_qa_reviewed",
    "translation_semantic_qa_final",
)


def _full_translation_request_is_newest(job: Job) -> bool:
    """整轮 ``translation_batches`` 是否是最后登记的那份在途请求。

    ``add_artifact`` 先按 kind 去重再追加，因此 ``job.artifacts`` 的相对次序就是各
    kind 最后一次登记的先后（与 ``pipeline.retranslate._newer_full_translation_request``
    同一个判据，方向相反）。用它把「整轮等待」与润色 / 语义 / 局部重译的等待分开：
    这三类的 manifest 与报告在译文被改写时不会被失效闭包带走，残留文件照样 checksum
    有效，只看「stage 在等输入 + 整轮 manifest 有效」会把它们的等待盖成整轮等待。
    """

    kinds = [artifact.kind for artifact in job.artifacts]
    try:
        index = kinds.index("translation_batches")
    except ValueError:
        return False
    return all(
        kind not in kinds or kinds.index(kind) < index for kind in _SUPERSEDED_BY_FULL_TRANSLATION
    )


def plan_advance_steps(job: Job, config: WorkflowConfig) -> tuple[str, ...]:
    """根据当前 Job 与持久化目标，列出仍需执行的推进步骤。

    返回值为有序步骤名：
    - ``await_cue_retranslation``：局部重译已进入 Agent 等待（可能叠加在一个端到端目标
      已经满足的 Job 上），调用方应停止并提示回填，不得按已完成处理
    - ``await_agent_translation``：整轮翻译已进入 Agent 等待（同样可能叠加在一个译文目标
      已经满足的 Job 上），调用方应停止并提示回填
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
    # 同一条理由对整轮 Agent 翻译等待完全成立，所以必须对称处理：下面
    # await_agent_translation 的全部 return 分支都埋在 `not _translation_satisfied`
    # 里，旧译文仍有效时它们根本不会被求值——用户照 status 的提示放弃重译、改跑一轮
    # 完整翻译（`xvt translate JOB --target-language LANG`）之后，status 报 complete、
    # continue 报完成、`xvt apply` 又说「not awaiting Agent input」，导航闭环断掉。
    # 满足度只算一次：_translation_satisfied 会哈希四到五个产物，重复调用会在没有
    # 只读记忆化作用域的 continue 路径上翻倍。
    translation_satisfied = goals.needs_translation and _translation_satisfied(job, config)
    if (
        translation_satisfied
        and agent_full_translation_waiting(job)
        and _full_translation_request_is_newest(job)
    ):
        return ("await_agent_translation",)

    steps: list[str] = []
    if goals.needs_translation and not translation_satisfied:
        draft_path = valid_artifact_path(job, "translation_draft_cues")
        if config.translation_quality == "polished":
            polished_ready = translation_polish_satisfied(
                job
            ) or agent_translate_and_polish_core_satisfied(job)
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
            if agent_translate_and_polish_progress_ready(job):
                steps.append("polish")
            elif agent_translate_and_polish_waiting(job):
                return ("await_agent_translation",)
            elif draft_path is None and agent_translate_and_polish_started(job):
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
        elif agent_full_translation_waiting(job):
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
        ) or agent_translate_and_polish_core_satisfied(job)
        if not polished_ready:
            if agent_translate_and_polish_progress_ready(job):
                complete_agent_polished_outputs(path, event_sink=event_sink)
            elif agent_translate_and_polish_waiting(job):
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
                    agent_translate_and_polish_started(job)
                    and not agent_translate_and_polish_waiting(job)
                    and not agent_translate_and_polish_progress_ready(job)
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

    retained_translation = valid_artifact_path(job, "input_translation_subtitles")
    translation_subtitles = retained_translation or config.translation_subtitles
    if translation_subtitles:
        # 与 status 共用同一份只读谓词、同一句诊断：这份已存译文套不回当前源 cue 时
        # apply_subtitle_translation 必然在映射处失败，而且失败发生在 set_stage 之前、
        # Job 一个字节都不改。提前带着那句诊断失败，用户看到的「任务失败」与随后的
        # 「恢复建议」逐字同源，不会一句说英文细节、一句说「源字幕有效可以继续」。
        blocker = pending_translation_import_blocker(job, config)
        if blocker is not None:
            # 异常类型刻意保持 ValueError：今天这条路径就是 map_subtitle_translations
            # 的 ValueError 一路冒到 CLI（退出码 2），提前失败只是把同一句话说得更清楚，
            # 不顺手改调用方看得见的异常契约。
            raise ValueError(blocker)
        apply_subtitle_translation(
            path,
            translation_subtitles,
            target_language=target_language,
            event_sink=event_sink,
            # 走 Job 内早先存下的副本时，没有人为当前这套源 cue 主张过「这份译文属于
            # 它」（`rebuild-source` 之后源时间轴可能已经变了），等长不足为凭，必须
            # 由时间轴重叠证实。用户此刻经 `--translation-subtitles` 交出的文件是那个
            # 主张本身，保持默认宽松。
            require_timeline_overlap=retained_translation is not None,
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

    # 必须排在下面那条被 goals.needs_translation 守卫的等待分支之前：局部重译等待要的是
    # 自己的 message 与 artifact 集合（``retranslating=True``），落进下面那条分支会被报成
    # "translation requires Agent input" 并给出整轮等待的产物清单。
    #
    # 本条与上面 ``waiting_step`` 里 ``or first_step == "await_cue_retranslation"`` 的
    # 放宽，原先都记着另一条理由：「局部重译可以发生在 workflow.target_language 为 None 的
    # Job 上（run 时没给目标语言，后来用原子 translate 补翻）」。**那条理由今天不成立**
    # （2026-10-20 实测）：``run`` 一定会落 ``config.workflow``，而
    # ``run_translation`` / ``prepare_agent_translation`` 都以
    # "target language must match the persisted workflow goal" 拒绝目标语言与快照不符的
    # 调用（退出码 2），所以这种 Job 永远拿不到 ``translated_cues``，
    # ``retranslate-cues`` 自己也以 "job has no translated_cues artifact" 退出。
    # needs_translation 就是 ``bool(config.target_language)``，因此当前可达的 Job 上
    # 两者同真同假。保留这条分支与那处放宽是为了上面那个 message/artifact 的差异，
    # 不是为了那个不可达的场景。
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
