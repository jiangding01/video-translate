# XVT Video Translate

XVT 是一个本地优先的视频转写、双语字幕翻译与烧录 CLI。它以稳定的时间轴为核心，支持本地媒体、已有字幕和 YouTube URL，输出 `SRT`、`ASS`，并可选烧录为新视频。

## 能力概览

- 本地视频、音频、YouTube URL 输入
- MLX Whisper 或 faster-whisper 转写，保留词级时间戳
- 导入 SRT、VTT、ASS、SSA 源字幕，默认保留原时间轴
- YouTube 人工字幕优先，允许显式回退自动字幕或 Whisper
- OpenAI-compatible Chat Completions 翻译，或 Agent 翻译批次协议
- 翻译断点续跑、配置指纹、429/5xx 退避和请求超时
- 原文、译文、双语 SRT，及独立中英样式的双语 ASS
- 字幕 QA、媒体 QA、FFmpeg 硬件或软件烧录
- `job.json`、checksum 校验、恢复和 JSONL 结构化事件
- 单命令端到端工作流，以及基于持久化目标的 `xvt continue`

时间轴只由 ASR 或源字幕决定。翻译阶段只能替换文本，不能改动 cue ID 或时间码。

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- FFmpeg，烧录时需包含 libass，建议同时具备 VideoToolbox 或 libx264
- yt-dlp，仅 YouTube 输入需要
- macOS Apple Silicon 使用 MLX Whisper 时安装 `asr-mlx` extra

安装开发环境：

```bash
uv sync --extra dev --extra asr-mlx
```

作为本地 CLI 安装：

```bash
uv tool install '.[asr-mlx]'
xvt doctor
```

检查运行时依赖：

```bash
uv run --no-sync xvt doctor
uv run --no-sync xvt doctor --json
```

`doctor` 会检查 FFmpeg、ffprobe、yt-dlp、libass、默认字幕字体的一帧实渲染、VideoToolbox、libx264 和已安装的 ASR 引擎。

## 快速开始

### 1. 单命令完成翻译与烧录

本地视频可以直接串联转写、翻译、双语字幕和烧录：

```bash
uv run --no-sync xvt run "/path/to/video.mp4" \
  --output "/absolute/path/to/output" \
  --language en \
  --target-language zh-CN \
  --provider auto \
  --burn \
  --burn-output "/path/to/bilingual.mp4"
```

已有源字幕时增加 `--subtitles source.srt`，会跳过 ASR；同时已有译文字幕时再增加 `--translation-subtitles translated.srt`，会继续跳过翻译 API。`--provider auto` 在兼容 API 配置完整时自动翻译，否则生成 Agent 批次并将工作流置为 `awaiting_input`，不会误报全部完成。

YouTube 全流程示例：

```bash
uv run --no-sync xvt run "https://www.youtube.com/watch?v=VIDEO_ID" \
  --output "/absolute/path/to/output" \
  --youtube-subtitles \
  --subtitle-language en \
  --allow-auto-subtitles \
  --target-language zh-CN \
  --burn
```

请求烧录时，远程视频会在前置阶段下载一次；ASR 和后续烧录复用同一文件。

普通运行无需额外参数即可在终端看到阶段进度。阶段切换会立即显示，下载和烧录会显示百分比、速度或剩余时间，翻译会显示当前批次。进度写入 stderr，最终 Job 和产物路径仍写入 stdout，现有脚本可以继续读取路径。

### 2. 只创建源字幕作业

```bash
uv run --no-sync xvt run "/path/to/video.mp4" \
  --output "/absolute/path/to/output" \
  --engine mlx \
  --language en \
  --events-jsonl
```

完成后，任务目录会包含 `job.json`、`artifacts/source-cues.json` 和 `artifacts/source.srt`。

已有源字幕时可跳过 ASR，并保留其时间轴：

```bash
uv run --no-sync xvt run "/path/to/video.mp4" \
  --output "/absolute/path/to/output" \
  --subtitles "/path/to/source.srt" \
  --language en
```

只有确认字幕存在整体偏移或漂移时，才使用 `--align-subtitles`。该模式会额外运行 Whisper 并写入 `artifacts/alignment.json`。

### 3. 单独翻译字幕

配置任意兼容 Chat Completions 的服务：

```bash
export XVT_TRANSLATION_BASE_URL="https://provider.example.com/v1"
export XVT_TRANSLATION_MODEL="model-name"
export XVT_TRANSLATION_API_KEY="secret"

uv run --no-sync xvt translate "/path/to/job.json" \
  --target-language zh-CN
```

DeepSeek 示例：

```bash
export XVT_TRANSLATION_BASE_URL="https://api.deepseek.com"
export XVT_TRANSLATION_MODEL="deepseek-v4-flash"
export XVT_TRANSLATION_API_KEY="secret"
```

`--provider auto` 仅在 Base URL、模型和 API Key 都存在时调用 API。配置不完整时会安全降级为 Agent 模式，生成 `artifacts/translation-batches.json`。显式传入 `--provider openai-compatible` 时，缺失配置会直接失败。

API Key 只从环境变量读取，不会写入命令行、事件或 `job.json`。翻译按批次保存到 `artifacts/translation-progress.json`，只有输入、模型、端点和提示词版本完全一致时才复用进度。429 会遵循 `Retry-After`，网络错误和 5xx 会退避重试。

不使用 API 时，可走 Agent 协议：

```bash
uv run --no-sync xvt prepare-translation "/path/to/job.json" \
  --source-language en \
  --target-language zh-CN

uv run --no-sync xvt apply-translation "/path/to/job.json" \
  "/path/to/translation-response.json" \
  --target-language zh-CN
```

响应格式见 [翻译协议](skills/video-translate/references/translation-protocol.md)。

如果已有机翻字幕，可直接导入：

```bash
uv run --no-sync xvt import-translation "/path/to/job.json" \
  "/path/to/machine-translated.srt" \
  --target-language zh-CN
```

源字幕时间轴始终优先。数量不一致的译文只有在时间覆盖完整时才能合并。

### 4. 只使用 YouTube 字幕

```bash
uv run --no-sync xvt run "https://www.youtube.com/watch?v=VIDEO_ID" \
  --output "/absolute/path/to/output" \
  --youtube-subtitles \
  --subtitle-language en \
  --allow-auto-subtitles
```

XVT 优先选择人工字幕。只有传入 `--allow-auto-subtitles` 才会使用自动字幕。需要登录态或代理时，必须由用户显式传入 `--cookies-from-browser` 或 `--proxy`。

### 5. 单独烧录双语字幕

```bash
uv run --no-sync xvt burn "/path/to/job.json" \
  --media "/path/to/video.mp4" \
  --output "/path/to/bilingual.mp4"
```

默认优先 VideoToolbox，仅在硬件编码器不可用或繁忙时回退 libx264。使用 `--software` 可直接指定软件编码。远程作业未传 `--media` 时会下载视频流；本地音频作业则必须通过 `--media` 提供视频。

## 输出与质量控制

典型产物位于 `jobs/<job-id>/artifacts/`：

| 文件 | 用途 |
| --- | --- |
| `source.srt` | 原文字幕 |
| `translation-batches.json` | Agent 翻译批次 |
| `translation-progress.json` | API 翻译断点进度 |
| `translated.srt` | 仅译文字幕 |
| `bilingual.srt` | 中英双语字幕 |
| `bilingual.ass` | 带独立中英样式的字幕 |
| `qa.json`、`qa-translated.json` | 字幕 QA 报告 |
| `burned.mp4` | 烧录视频 |

双语输出会折叠每种语言内部的源字幕换行，确保每个 cue 固定为“译文一行 + 原文一行”。对 16:9 横版英文，64 个显示单位以内通过，65 到 72 个显示单位仅警告，超过 72 才需复核；窄画幅会自动收紧上限。中文维持更严格的单行限制。

## 恢复与诊断

```bash
uv run --no-sync xvt status "/path/to/job.json"
uv run --no-sync xvt verify "/path/to/job.json" --json
uv run --no-sync xvt continue "/path/to/job.json" --events-jsonl
uv run --no-sync xvt resume "/path/to/job.json" --events-jsonl
```

`continue` 根据 `run` 时持久化的目标补齐翻译和烧录：API 中断会复用批次进度，Agent 等待会停在 `apply-translation`，烧录失败只重跑烧录，指纹有效的成品不会重复生成。`resume` 仅负责从规范化音频、词级转写或源 cue 恢复转写主链路。`verify` 会重新计算 artifact 的 SHA-256，`status` 会根据当前 Job 给出下一条建议命令。

任务创建后会立即显示 `job.json` 路径。后续失败时，普通模式会显示失败阶段、任务路径，以及保守可靠的 `xvt status JOB` 检查命令；已有 Job 的原子命令同样保留任务路径。用户按 Ctrl-C 时，当前运行阶段会记录为失败，CLI 返回退出码 130 并显示同一套状态检查入口。在统一恢复计划完成前，不会根据未校验的 artifact 猜测续跑命令。任务创建前的参数或输入错误会明确提示修正后重新执行。

`run`、`continue`、`translate`、`prepare-translation`、`apply-translation`、`import-translation` 和 `burn` 支持 `--events-jsonl`。该模式保留机器接口：stdout 只输出逐行 JSON 事件，不混入普通进度文本；端到端链路还会输出 `workflow_planned`、`workflow_waiting`、`workflow_completed`、`download_progress` 和 `burn_progress`。完成或失败时以 `cli_completed`、`workflow_waiting` 或 `cli_failed` 收口。命令事件会脱敏代理账号、密码、Cookie、鉴权请求头和常见签名查询参数。

## 开发

```bash
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pytest -q
```

更多设计说明见 [架构说明](docs/ARCHITECTURE.md)，已完成和待完成工作见 [路线图](ROADMAP.md)，协作约定见 [贡献指南](CONTRIBUTING.md)。

## License

[MIT](LICENSE)
