"""Workflow 配置默认值的单一事实来源。

本模块只包含纯常量，不导入任何其他 xvt 模块，避免循环导入。
CLI 参数默认值、`WorkflowOptions` 字段默认值以及 pipeline 读取
持久化 config 时的手写兜底，都应引用这里的常量，防止默认值散落漂移。
"""

from __future__ import annotations

# 默认转写引擎（MLX Whisper）。
DEFAULT_ENGINE = "mlx"

# 默认 ASR 分段预设。
DEFAULT_SEGMENTATION_PRESET = "balanced"

# 默认翻译提供方选择模式（凭据完整走兼容 API，否则进入 Agent 模式）。
DEFAULT_PROVIDER = "auto"

# 默认翻译批次大小（每批 cue 数）。
DEFAULT_BATCH_SIZE = 40

# 默认翻译请求最大重试次数。
DEFAULT_MAX_RETRIES = 2

# 默认翻译请求超时时间（秒）。
DEFAULT_TIMEOUT_SECONDS: float = 60

# 默认读取 API Key 的环境变量名。
DEFAULT_API_KEY_ENV = "XVT_TRANSLATION_API_KEY"

# 默认翻译风格。
DEFAULT_TRANSLATION_STYLE = "faithful"

# 默认源字幕对齐的最低置信度阈值。
DEFAULT_MIN_ALIGNMENT_CONFIDENCE = 0.55

# 默认是否启用硬件编码烧录。
DEFAULT_HARDWARE_ENCODING = True
