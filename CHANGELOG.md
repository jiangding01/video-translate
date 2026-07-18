# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的分类方式。以下记录基于首次公开版本前已验证的功能状态。

## [Unreleased]

### Added

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

### Fixed

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
