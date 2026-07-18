# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的分类方式。以下记录基于首次公开版本前已验证的功能状态。

## [Unreleased]

### Changed

- artifact 依赖图与失效闭包收敛到单一来源（`src/xvt/artifacts.py`）：新增 `ArtifactKind` 枚举登记全部产物种类、`DERIVES_FROM` 依赖边表和 `downstream_of` 传递闭包函数；`pipeline.py` 中 `_invalidate_rendered_artifacts`、`_invalidate_preview_artifacts` 和 `resegment_translation` 的三处失效集合改由闭包推导，不再各自维护硬编码集合。新增测试将三处历史集合钉为基线并校验依赖图无环，行为与现状完全一致。
- workflow 配置默认值收敛到单一来源（`src/xvt/config.py`）：新增纯常量模块集中定义 `DEFAULT_ENGINE`、`DEFAULT_SEGMENTATION_PRESET`、`DEFAULT_PROVIDER`、`DEFAULT_BATCH_SIZE`、`DEFAULT_MAX_RETRIES`、`DEFAULT_TIMEOUT_SECONDS`、`DEFAULT_API_KEY_ENV`、`DEFAULT_TRANSLATION_STYLE`、`DEFAULT_MIN_ALIGNMENT_CONFIDENCE`、`DEFAULT_HARDWARE_ENCODING`；`cli.py` 各子命令参数默认值、`WorkflowOptions` 字段默认值以及 `pipeline.py`/`workflow.py` 读取持久化 config 的手写兜底改引常量。纯内部收敛，无行为或数值变化。
- 集中定义错误模型与退出码（`src/xvt/errors.py`）：新增 `ExitCode` 枚举和 `XvtError` 领域异常层次（`UserInputError`、`DependencyError`、`InternalError`）；顶层异常映射优先识别领域异常并沿用其退出码与 category，旧的按 Python 异常类型兜底映射保持不变。
- **Breaking**：拆分冲突的退出码语义。`verify` 校验未通过由 `4` 改为 `5`（VERIFY_FAILED），`batch`/`batch-continue` 存在失败条目由 `4` 改为 `6`（PARTIAL_FAILURE）；`doctor` 未通过仍为 `3`，中断仍为 `130`。依赖脚本请同步更新退出码判断。

### Added

- Job schema 前向兼容与迁移链骨架：`Artifact` 和 stage record 解析改为按已知字段过滤构造，未知字段静默忽略，避免旧版本 CLI 读取新版本 `job.json` 时因新增字段崩溃；新增模块级 `_MIGRATIONS` 迁移链骨架（当前为空），供未来结构性 schema 变更登记逐版本迁移函数。
- JSONL 事件协议 v1 地基：新增 `EVENT_PROTOCOL_VERSION` 与 `EventType` 枚举作为全部事件类型的单一事实来源，事件信封新增 `v` 版本字段，消费端改引枚举成员；新增 `job_event_fields` / `error_event_fields` 信封辅助函数，并以 `docs/EVENTS.md` 记录协议版本、兼容性承诺和全部事件字段契约。
- 生产端 v1 事件迁移全量完成（P3b/P3c）：`pipeline.py`、`workflow.py`、`batch.py`、`process.py`、`download.py`、`transcribe.py`、`burn.py` 的全部 `create_event` 调用改引 `EventType` 枚举成员，`command_failed` 统一接入 `error_event_fields` 错误信封；`create_event` 签名收紧为仅接受 `EventType`，删除裸字符串兼容分支；新增测试扫描源码防止裸字符串调用回归。
- `transcription_progress` 结构化事件：faster-whisper 使用词级分段提供真实百分比和 ETA，MLX Whisper 提供运行时长心跳。
- 超宽 ASR 片段会在翻译前按句末、软标点、连接词或介词等语义优先边界切分，并保持词级时间轴连续。
- `xvt resegment-translation`：对超过硬显示阈值的 ASR 译文 cue 按语义边界重切、重建源字幕并使翻译及下游产物失效，随后按新 cue ID 重新翻译。
- `translate` 和 `prepare-translation` 支持 JSON 术语表及 `faithful`、`natural`、`concise` 翻译风格；策略会持久化到 Job 并进入 API 翻译指纹。
- `xvt run` 支持同一套翻译策略；端到端 Job 保存策略快照，`xvt continue` 可在原术语表文件不可用时保持相同翻译约束。
- 译文 QA 对术语表执行确定性检查；违规以 `glossary_violation` 定位到 cue，并阻断渲染和烧录。
- `retranslate-cues` 和 `apply-cue-retranslation` 支持仅修复指定 cue；保留其他译文和时间轴，重跑 QA 并使旧渲染、烧录产物失效。
- `xvt preview` 支持按 cue 或起始时间生成短片段双语预览，复用正式 ASS 样式和烧录参数，并独立登记 checksum artifact。
- `xvt run --segmentation-preset` 支持 `conservative`、`balanced` 和 `compact` 三种 ASR 分段预设；选择会持久化到 Job 和端到端工作流，`resume` 可保持一致。
- `xvt run source.srt` 支持 SRT、VTT、ASS、SSA 纯字幕 Job：保留用户时间轴，无需媒体即可翻译、QA、`continue` 和导出原文、译文、双语 SRT。
- `xvt batch` 和 `xvt batch-continue`：固定目录输入清单，串行创建独立 Job，隔离单项失败，并通过原子 `batch.json` 汇总报告恢复未完成任务。
- 外部命令以独立进程组执行：超时、取消或中断时清理全部子孙进程，不再遗留 ffmpeg/yt-dlp 孤儿进程；所有外部命令默认携带 120 秒静默超时兜底，并新增协作式取消入口（`CommandCancelled`）。
- CLI 现在处理 SIGTERM：与 Ctrl-C 一样清理进程组、持久化中断的 stage 并生成恢复计划，退出码为 143；翻译批次循环和批处理条目在迭代边界响应取消。
- Job 与 batch 独占执行：改写既有 Job 的子命令和 `batch-continue` 在入口取 flock 目录锁，第二个进程以退出码 7 立即失败并提示持有者 PID；进程崩溃时内核自动释放锁。

- stage 跳过决策收敛到 `stages.py` 计划表：入口在业务逻辑开始前一次性标记跳过项（先计划后执行），取代散落在各分支的逐个记账；中途失败时计划内的跳过状态已持久化。
- run 首跑与 `continue` 续跑统一为同一推进函数：转写完成后的翻译、Agent 等待与烧录决策只有一份实现，首跑烧录同样走指纹感知路径；`workflow_completed` 与等待事件的 artifacts 统一为 checksum 有效的用户产物。
- 翻译收尾统一为单一 `_finalize_translation`：局部重译、Agent/API 响应回填和导入译文字幕三条路径共用同一段「写译文 cue → 失效下游 → 译文 QA → 渲染」逻辑，消除三份重复骨架，无行为变化。

### Fixed

- 修复媒体 QA 失败但 `burned_video` checksum 有效时，恢复计划、批处理或续跑可能错误宣告任务完成的问题。
- 修复仅生成源字幕时 `workflow_completed` 和 `cli_completed` 没有返回 `source.srt` 的问题。
- 损坏或字段不完整的当前版本 Job 现在返回可控的无效输入错误，不再泄漏 Python traceback。
- 修复转写失败或缺少词级时间戳时可能误报完成进度的问题。
- 修复预览未继承工作流烧录媒体、字体和编码设置，以及自定义输出可能覆盖 Job、源媒体或正式 artifact 的问题；预览中断现在提供独立重试提示。

## [0.1.0] - 2026-07-15

### Added

- 本地视频、音频和 YouTube URL 的转写与字幕导入流程。
- MLX Whisper 与 faster-whisper 转写适配器，支持词级时间戳和基于停顿、标点、长度的分段。
- SRT、VTT、ASS、SSA 解析，已有字幕时间轴保留和可选 Whisper 对齐。
- YouTube 人工字幕优先选择，可显式回退自动字幕或 Whisper。
- OpenAI-compatible Chat Completions 翻译与 Agent 翻译协议。
- 翻译断点续跑、输入与配置指纹、请求超时、429/5xx 退避与错误分类。
- 原文、译文、双语 SRT，独立中英样式 ASS，以及 FFmpeg 烧录。
- 字幕 QA、媒体 QA、`job.json` 持久化、checksum 校验、恢复和 JSONL 事件。
- 统一 `xvt run` 端到端编排与 `xvt continue` 目标恢复，覆盖 API 中断、Agent 等待和烧录重试。
- 远程检查或下载失败后，可在同一个 Job 中重新进入素材获取阶段，并复用 checksum 有效的远程产物。
- yt-dlp 下载与 FFmpeg 烧录的结构化实时进度事件。

### Changed

- 双语输出会折叠每种语言内部的源字幕换行，固定为译文和原文各一行。
- 英文分段默认上限调整为 64 个显示单位。横版英文 65 到 72 个显示单位仅警告，超过 72 才需要复核，窄画幅自动收紧。
- macOS 默认中文字幕字体使用可被 libass 读取的 `Hiragino Sans GB`。
- 远程烧录工作流前置下载完整视频，ASR 和烧录复用同一文件。

### Fixed

- 修复远程字幕作业中不必要的音频下载和 ASR。
- 修复 VideoToolbox 对不兼容质量参数的烧录失败，保留精确的软件编码回退。
- 修复字幕内部换行被 ASS 原样保留，导致英文额外换行的问题。
- 修复外部命令事件可能记录代理凭据、Cookie、鉴权头或签名 URL 的问题。
- 修复远程元数据检查未使用显式 Cookie 来源和代理的问题。
