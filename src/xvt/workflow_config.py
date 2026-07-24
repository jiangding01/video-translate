"""端到端工作流配置的类型化模型与读写边界。

``job.config["workflow"]`` 在磁盘上仍是普通 JSON 对象；本模块是唯一的
结构化入口。读取时校验类型与默认值，写出时保持与既有 Job 字段兼容。
未知字段在 ``from_dict`` 中被忽略，以兼容未来前向扩展。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from xvt.config import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BATCH_SIZE,
    DEFAULT_HARDWARE_ENCODING,
    DEFAULT_MAX_RETRIES,
    DEFAULT_PROVIDER,
    DEFAULT_SEGMENTATION_PRESET,
    DEFAULT_TIMEOUT_SECONDS,
)
from xvt.models import Job
from xvt.translate import TranslationPolicy, translation_policy_from_dict

# 已知字段集合：from_dict 之外的键进入 extras，to_dict 原样写回，避免
# 新版本 CLI 写入的扩展字段被旧版本在更新指纹时抹掉。
_KNOWN_FIELDS = frozenset(
    {
        "target_language",
        "requested_outputs",
        "translation_subtitles",
        "provider",
        "base_url",
        "model",
        "api_key_env",
        "batch_size",
        "max_retries",
        "timeout_seconds",
        "translation_policy",
        "segmentation_preset",
        "burn",
        "burn_media",
        "burn_output",
        "hardware_encoding",
        "target_font",
        "source_font",
        "burn_fingerprint",
    }
)


@dataclass(frozen=True, slots=True)
class WorkflowConfig:
    """持久化在 ``job.config["workflow"]`` 中的端到端目标与运行参数。"""

    target_language: str | None = None
    requested_outputs: tuple[str, ...] = ()
    translation_subtitles: str | None = None
    provider: str = DEFAULT_PROVIDER
    base_url: str | None = None
    model: str | None = None
    api_key_env: str = DEFAULT_API_KEY_ENV
    batch_size: int = DEFAULT_BATCH_SIZE
    max_retries: int = DEFAULT_MAX_RETRIES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    translation_policy: TranslationPolicy | None = None
    segmentation_preset: str = DEFAULT_SEGMENTATION_PRESET
    burn: bool = False
    burn_media: str | None = None
    burn_output: str | None = None
    hardware_encoding: bool = DEFAULT_HARDWARE_ENCODING
    target_font: str | None = None
    source_font: str | None = None
    burn_fingerprint: str | None = None
    # 未知顶层字段；不参与相等比较，只负责 round-trip 保真。
    extras: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def to_dict(self) -> dict[str, Any]:
        # burn_fingerprint 仅在烧录成功后写入，缺省时省略以保持与历史 Job 形状接近。
        data: dict[str, Any] = {
            "target_language": self.target_language,
            "requested_outputs": list(self.requested_outputs),
            "translation_subtitles": self.translation_subtitles,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "batch_size": self.batch_size,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "translation_policy": (
                self.translation_policy.to_dict() if self.translation_policy is not None else None
            ),
            "segmentation_preset": self.segmentation_preset,
            "burn": self.burn,
            "burn_media": self.burn_media,
            "burn_output": self.burn_output,
            "hardware_encoding": self.hardware_encoding,
            "target_font": self.target_font,
            "source_font": self.source_font,
        }
        if self.burn_fingerprint is not None:
            data["burn_fingerprint"] = self.burn_fingerprint
        # 后写 extras，避免未知字段覆盖已知字段的权威值。
        for key, value in self.extras.items():
            if key not in _KNOWN_FIELDS:
                data[key] = value
        return data

    def with_burn_fingerprint(self, fingerprint: str | None) -> WorkflowConfig:
        return replace(self, burn_fingerprint=fingerprint)

    @classmethod
    def from_dict(cls, data: object) -> WorkflowConfig:
        if not isinstance(data, dict):
            raise ValueError("workflow config must be an object")

        target_language = _optional_str(data.get("target_language"), "target_language")
        requested_outputs = _requested_outputs(data.get("requested_outputs", []))
        translation_subtitles = _optional_str(
            data.get("translation_subtitles"), "translation_subtitles"
        )
        provider = data.get("provider", DEFAULT_PROVIDER)
        if not isinstance(provider, str) or not provider:
            raise ValueError("workflow config provider must be a non-empty string")
        base_url = _optional_str(data.get("base_url"), "base_url")
        model = _optional_str(data.get("model"), "model")
        api_key_env = data.get("api_key_env", DEFAULT_API_KEY_ENV)
        if not isinstance(api_key_env, str) or not api_key_env:
            raise ValueError("workflow config api_key_env must be a non-empty string")
        batch_size = _positive_int(data.get("batch_size", DEFAULT_BATCH_SIZE), "batch_size")
        max_retries = _non_negative_int(data.get("max_retries", DEFAULT_MAX_RETRIES), "max_retries")
        timeout_seconds = _positive_float(
            data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "timeout_seconds"
        )
        policy_raw = data.get("translation_policy")
        translation_policy = (
            None if policy_raw is None else translation_policy_from_dict(policy_raw)
        )
        segmentation_preset = data.get("segmentation_preset", DEFAULT_SEGMENTATION_PRESET)
        if not isinstance(segmentation_preset, str) or not segmentation_preset:
            raise ValueError("workflow config segmentation_preset must be a non-empty string")
        burn = _bool(data.get("burn", False), "burn")
        burn_media = _optional_str(data.get("burn_media"), "burn_media")
        burn_output = _optional_str(data.get("burn_output"), "burn_output")
        hardware_encoding = _bool(
            data.get("hardware_encoding", DEFAULT_HARDWARE_ENCODING), "hardware_encoding"
        )
        target_font = _optional_str(data.get("target_font"), "target_font")
        source_font = _optional_str(data.get("source_font"), "source_font")
        burn_fingerprint = _optional_str(data.get("burn_fingerprint"), "burn_fingerprint")
        extras = {key: value for key, value in data.items() if key not in _KNOWN_FIELDS}

        return cls(
            target_language=target_language,
            requested_outputs=requested_outputs,
            translation_subtitles=translation_subtitles,
            provider=provider,
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
            batch_size=batch_size,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            translation_policy=translation_policy,
            segmentation_preset=segmentation_preset,
            burn=burn,
            burn_media=burn_media,
            burn_output=burn_output,
            hardware_encoding=hardware_encoding,
            target_font=target_font,
            source_font=source_font,
            burn_fingerprint=burn_fingerprint,
            extras=extras,
        )


def get_workflow_config(job: Job) -> WorkflowConfig | None:
    """读取 Job 上的 workflow 配置；不存在时返回 None。"""

    raw = job.config.get("workflow")
    if raw is None:
        return None
    return WorkflowConfig.from_dict(raw)


def require_workflow_config(job: Job) -> WorkflowConfig:
    """读取端到端 workflow 配置；缺失时提示改用原子 resume。"""

    config = get_workflow_config(job)
    if config is None:
        raise ValueError("job has no persisted workflow goal; use the atomic resume command")
    return config


def set_workflow_config(job: Job, config: WorkflowConfig) -> None:
    """将类型化配置写回 ``job.config["workflow"]``（调用方负责 save_job）。"""

    job.config["workflow"] = config.to_dict()


def clear_burn_fingerprint(job: Job) -> bool:
    """清除烧录派生指纹（artifact 登记 + 历史 workflow 键）。

    - 新路径：移除 ``burn_fingerprint`` artifact 登记（文件可残留，不参与恢复）。
    - 旧路径：只删除 ``workflow.burn_fingerprint`` 键，不整表重写 workflow。
    """

    changed = False
    kept = [artifact for artifact in job.artifacts if artifact.kind != "burn_fingerprint"]
    if len(kept) != len(job.artifacts):
        job.artifacts = kept
        changed = True
    raw = job.config.get("workflow")
    if isinstance(raw, dict) and "burn_fingerprint" in raw:
        raw.pop("burn_fingerprint", None)
        changed = True
    return changed


def resolve_target_language(job: Job) -> str | None:
    """解析目标语言：优先 workflow 配置，回退原子翻译写入的顶层字段。"""

    config = get_workflow_config(job)
    if config is not None and config.target_language:
        return config.target_language
    value = job.config.get("target_language")
    return value if isinstance(value, str) and value else None


def _optional_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"workflow config {field} must be a string or null")
    return value or None


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"workflow config {field} must be a boolean")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"workflow config {field} must be a positive integer")
    return value


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"workflow config {field} must be a non-negative integer")
    return value


def _positive_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        raise ValueError(f"workflow config {field} must be a positive number")
    return float(value)


def _requested_outputs(value: object) -> tuple[str, ...]:
    """解析 requested_outputs。

    未知输出名保留而非拒绝：当前推进逻辑仍以 target_language / burn 为准，
    白名单会破坏「新版本写入、旧版本读取」的前向兼容。
    """

    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("workflow config requested_outputs must be an array")
    outputs: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("workflow config requested_outputs items must be non-empty strings")
        outputs.append(item)
    return tuple(outputs)
