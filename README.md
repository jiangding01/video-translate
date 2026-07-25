# XVT Video Translate

[![CI](https://github.com/jiangding01/video-translate/actions/workflows/ci.yml/badge.svg)](https://github.com/jiangding01/video-translate/actions/workflows/ci.yml)

XVT 是一个本地优先的视频转写、双语字幕翻译与烧录 CLI。它以稳定的时间轴为核心，支持本地媒体、已有字幕和 YouTube URL，输出 `SRT`、`ASS`，并可选烧录为新视频。

## 能力概览

- 本地视频、音频、YouTube URL 输入
- MLX Whisper 或 faster-whisper 转写，保留词级时间戳；超宽 ASR 片段会优先按语义边界切分；faster-whisper 显示真实完成比例，MLX 显示诚实的运行时长
- 导入 SRT、VTT、ASS、SSA 源字幕，默认保留原时间轴
- YouTube 平台字幕仅在显式请求时下载；新 Job 以候选 QA 和 `--source-strategy auto|subtitles|asr` 选择最终时间轴
- OpenAI-compatible Chat Completions 翻译，或 Agent 翻译批次协议；确定性语义组避免在 provider 批次边界切开句意
- 翻译断点续跑、配置指纹、429/5xx 退避和请求超时
- 已有端到端 Job 可显式补跑可恢复译文润色，支持 API checkpoint 与 Agent 回填
- 原文、译文、双语 SRT，及独立中英样式的双语 ASS
- 按 cue 或时间范围生成短片段预览，复用正式 ASS 样式和烧录参数
- `conservative`、`balanced`、`compact` 三种 ASR 字幕分段预设，默认保持既有平衡行为
- 字幕 QA、媒体 QA、FFmpeg 硬件或软件烧录
- `job.json`、checksum 校验、恢复和 JSONL 结构化事件
- 单命令端到端工作流，以及基于持久化目标的 `xvt continue`
- 目录批处理、独立 Job、失败隔离和可恢复的 `batch.json` 汇总报告

时间轴只由 ASR 或源字幕决定。翻译阶段只能替换文本，不能改动 cue ID 或时间码。

## 下一阶段规划

在开始可视化校对界面之前，项目已完成 Q1–Q6 字幕质量闭环：翻译传输可靠性、源字幕自适应选择、既有 Job 的 ASR 源重建、语义组翻译、可恢复润色，以及语义校对与一次定向修复。下一步是使用公开素材完成 polished API/Agent E2E，再决定是否调整默认质量。完整设计见 [字幕质量与自适应源字幕技术方案](docs/SUBTITLE_QUALITY_PLAN.md)。

该文档同时记录已完成和待实现阶段；当前可用命令和行为仍以本 README 其他章节及 `xvt --help` 为准。

## 默认命令路径

日常优先记住三条命令；中断后先看 `status`，再执行它提示的下一步：

```text
xvt run INPUT --output DIR [--target-language LANG] [--translation-quality standard|polished] [--burn]
xvt status JOB.json
xvt continue JOB.json
```

Agent 等待输入时：

```text
xvt apply-translation JOB.json RESPONSE --target-language LANG
xvt continue JOB.json
```

目录批处理：

```text
xvt batch DIR --output DIR ...
xvt batch-continue BATCH.json
```

`resume`、`translate`、`burn`、`preview`、`resegment-translation` 等是**原子/修复**入口（`xvt --help` 中标为 `[advanced]`）。端到端 Job 不要用 `resume` 代替 `continue`：前者只恢复转写 checkpoint，后者完成 `job.config.workflow` 里的目标。

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
  --glossary "/path/to/glossary.json" \
  --translation-style natural \
  --segmentation-preset balanced \
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

普通运行无需额外参数即可在终端看到阶段进度。阶段切换会立即显示，下载、faster-whisper 转写和烧录会显示百分比、速度或剩余时间，MLX Whisper 会显示已运行时长，翻译会显示当前批次。进度写入 stderr，最终 Job 和产物路径仍写入 stdout，现有脚本可以继续读取路径。

### 2. 只创建源字幕作业

```bash
uv run --no-sync xvt run "/path/to/video.mp4" \
  --output "/absolute/path/to/output" \
  --engine mlx \
  --language en \
  --events-jsonl
```

完成后，任务目录会包含 `job.json`、`artifacts/source-cues.json` 和 `artifacts/source.srt`。

`--segmentation-preset conservative` 使用更长的停顿和时长阈值，适合更完整的句子；默认 `balanced` 保持既有分段；`compact` 使用更短的阈值，适合需要更频繁换行的短视频。预设仅作用于 ASR 分段：直接导入源字幕会保留原时间轴；`--align-subtitles` 跳过分段预设，并仅在用户明确请求时按 ASR 对齐修正时间轴。

已有源字幕时可跳过 ASR，并保留其时间轴：

```bash
uv run --no-sync xvt run "/path/to/video.mp4" \
  --output "/absolute/path/to/output" \
  --subtitles "/path/to/source.srt" \
  --language en
```

只有确认字幕存在整体偏移或漂移时，才使用 `--align-subtitles`。该模式会额外运行 Whisper 并写入 `artifacts/alignment.json`。

### 3. 纯字幕文件工作流

源字幕可以直接作为输入创建 Job，无需提供视频或音频：

```bash
uv run --no-sync xvt run "/path/to/source.srt" \
  --output "/absolute/path/to/output" \
  --language en \
  --target-language zh-CN \
  --provider auto
```

支持 SRT、VTT、ASS 和 SSA。该模式保留用户时间轴，输出 `source.srt`、`translated.srt` 和 `bilingual.srt`，并照常支持 Agent 翻译、`continue`、`verify` 和术语表 QA。由于没有媒体尺寸，不生成 ASS，也不支持 `--align-subtitles`、烧录、预览或 `resegment-translation`。

### 4. 目录批处理

目录中的本地视频、音频和字幕文件可以按稳定顺序串行处理，每个输入创建独立 Job：

```bash
uv run --no-sync xvt batch "/path/to/input-directory" \
  --output "/absolute/path/to/output" \
  --target-language zh-CN \
  --provider auto \
  --burn
```

默认只扫描目录第一层；使用 `--recursive` 才递归扫描。批处理支持常见视频、音频及 SRT、VTT、ASS、SSA，忽略隐藏文件和输出目录。使用 `--burn` 时目录只能包含视频；音频和纯字幕输入应拆成不烧录的独立批次。单项失败不会阻断后续文件，执行状态会原子写入 `batches/<batch-id>/batch.json`，其中包含每个输入的 Job 文件、产物、错误和恢复计划。

中断、部分失败或 Agent 等待后，继续原批次：

```bash
uv run --no-sync xvt batch-continue "/path/to/batch.json"
```

`batch-continue` 会校验已有 Job，只跳过目标和 checksum 均有效的已完成项；可重试项调用原有 `continue` 工作流，需要人工修复或 Agent 回填的项保留恢复指令。批处理当前串行执行，不监控目录变化，也不提供队列或后台并发。

### 5. 单独翻译字幕

配置任意兼容 Chat Completions 的服务：

```bash
export XVT_TRANSLATION_BASE_URL="https://provider.example.com/v1"
export XVT_TRANSLATION_MODEL="model-name"
export XVT_TRANSLATION_API_KEY="secret"

uv run --no-sync xvt translate "/path/to/job.json" \
  --target-language zh-CN \
  --glossary "/path/to/glossary.json" \
  --translation-style natural
```

DeepSeek 示例：

```bash
export XVT_TRANSLATION_BASE_URL="https://api.deepseek.com"
export XVT_TRANSLATION_MODEL="deepseek-v4-flash"
export XVT_TRANSLATION_API_KEY="secret"
```

`--provider auto` 仅在 Base URL、模型和 API Key 都存在时调用 API。配置不完整时会安全降级为 Agent 模式，生成 `artifacts/translation-batches.json`。显式传入 `--provider openai-compatible` 时，缺失配置会直接失败。

API Key 只从环境变量读取，不会写入命令行、事件或 `job.json`。翻译按批次保存到 `artifacts/translation-progress.json`，只有输入、模型、端点和提示词版本完全一致时才复用进度。429 会遵循 `Retry-After`，网络错误和 5xx 会退避重试。

`translate` 和 `prepare-translation` 支持结构化术语表及 `faithful`（默认）、`natural`、`concise` 三种风格。Q4 不改变默认风格：新 Job 仍使用 `faithful`，除非显式传入 `--translation-style`。术语表是 JSON 文件；命中当前批次源 cue 的术语才会写入 API 请求或 Agent 批次，避免无关提示膨胀：

```json
{
  "terms": [
    {"source": "OpenAI", "target": "OpenAI"},
    {"source": "Whisper", "target": "Whisper"}
  ]
}
```

`translate`、`prepare-translation` 和端到端 `run` 都支持该策略。端到端 Job 会保存解析后的策略快照而不是术语表路径，因此 `continue` 即使在原术语表文件移动、修改或删除后，仍使用首次运行时的相同术语和风格。策略也会进入 API 翻译指纹；修改风格或术语表后不会复用旧的批次进度。译文 QA 会确定性检查命中源 cue 的术语是否包含要求的目标词；违规会以 `glossary_violation` 写入 `qa-translated.json`，阻断渲染和烧录。

Q4 在翻译前按已有 cue 确定性建立语义组：句末标点、相邻 cue 间隔达到 650ms、20 个 cue 或 30 秒会形成边界；达到上限时优先在句末、软标点、连接词前或最大停顿处分界。provider 批次仍最多容纳 40 个 cue，但只以完整语义组装箱，不会从组中切开。批次中的 `semantic_groups` 仅记录各组的 cue ID，时间轴、cue 数量和顺序始终不变。

新 Job 可显式选择翻译质量，默认仍为 `standard`：

```bash
uv run --no-sync xvt run INPUT --output OUTPUT \
  --target-language zh-CN \
  --translation-quality polished
```

`run`、`batch`、`translate` 和 `prepare-translation` 都支持 `standard|polished`。API 模式的 `polished` 先保存完整翻译草稿，再逐批润色；Agent 模式生成带 `task=translate_and_polish` 和 fingerprint 的单个 `translation-batches.json`。润色候选随后必须经过 Q6 语义校对；API 对可修复问题按完整语义组最多修复一次并复查，Agent 生成 `task=semantic_review_and_repair` manifest，使用 `apply-semantic-review JOB RESPONSE` 回填。旧 Job 或旧 batch 缺质量字段时继续按 `standard`。`--translation-subtitles` 不能与新 Job 的 `polished` 同时使用，应先导入译文再执行 `polish-translation`。

已有端到端 Job 具备 checksum 有效的 `translated_cues` 时，可显式补跑 Q5a 润色：

```bash
uv run --no-sync xvt polish-translation "/path/to/job.json"
```

命令先保存不可变草稿，再将工作流质量目标设为 `polished` 并失效旧译文下游。API 模式逐批写入 `translation-polish-progress.json`，中断后执行 `xvt continue JOB` 只续跑未完成批次；配置不完整的 `auto` 模式生成 `translation-polish-batches.json`，完成 Agent 响应后执行：

```bash
uv run --no-sync xvt apply-polish-translation "/path/to/job.json" RESPONSE.json
uv run --no-sync xvt continue "/path/to/job.json"
```

润色响应必须回传当前 `translation-polish-batches.json` 的 `fingerprint`、精确覆盖全部请求 ID，并且只能替换译文文本；旧响应不能用于新的 draft。`continue` 随后进入语义校对。如果语义报告仍有问题，`status` 会要求检查报告并执行：

```bash
uv run --no-sync xvt continue "/path/to/job.json" --acknowledge-translation-review
```

确认绑定当前候选、完整语义处理 artifact 链和报告 checksum；内容变化后自动失效。初次报告包含可修复问题时不能提前确认，修复完成后仍会保留未改动源文对应的 `source_suspect`。确定性 QA 失败仍会阻断渲染与烧录。Q5a 的显式补跑仍只支持带工作流配置的端到端 Job；原子 `translate --translation-quality polished` 通过重跑同一命令恢复 API draft、润色和语义校对 checkpoint。

`translation-draft-cues.json` 是恢复润色的必要检查点。它缺失或 checksum 失效时，`status` 会要求人工恢复原始译文草稿，不会把已润色的最终译文再次复制为 draft。

对 QA 报告中定位出的单个或多个 cue，可使用 `retranslate-cues`。它只发送指定 cue，带相邻源 cue 作为只读上下文，复用 Job 已保存的术语策略；回填后保留其他译文与全部时间轴，重新执行完整译文 QA，并使旧 ASS、烧录视频和媒体 QA 失效：

```bash
uv run --no-sync xvt retranslate-cues "/path/to/job.json" \
  --cue 18 --cue 24 \
  --target-language zh-CN
```

Agent 模式会生成 `cue-retranslation-batches.json`，完成后将只包含这些 cue ID 的响应回填：

```bash
uv run --no-sync xvt apply-cue-retranslation "/path/to/job.json" \
  "/path/to/cue-retranslation-response.json" \
  --cue 18 --cue 24 \
  --target-language zh-CN
```

若端到端 Job 请求烧录，QA 通过并生成字幕后再执行 `xvt continue JOB`。局部修复不支持改变目标语言，也不接受重复或不存在的 cue ID。

不使用 API 时，可走 Agent 协议：

```bash
uv run --no-sync xvt prepare-translation "/path/to/job.json" \
  --source-language en \
  --target-language zh-CN \
  --glossary "/path/to/glossary.json" \
  --translation-style natural

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

### 6. 只使用 YouTube 字幕

```bash
uv run --no-sync xvt run "https://www.youtube.com/watch?v=VIDEO_ID" \
  --output "/absolute/path/to/output" \
  --youtube-subtitles \
  --subtitle-language en \
  --allow-auto-subtitles
```

XVT 仅在传入 `--youtube-subtitles` 时请求平台候选字幕，且只有传入 `--allow-auto-subtitles` 才会请求自动字幕。新 Job 默认 `--source-strategy auto`：人工候选 QA 通过直接采用，失败时先尝试对齐再回退 ASR；自动候选在翻译或烧录任务中最终使用 ASR，在只生成源字幕且 QA 通过时可直接采用。`subtitles` 强制候选为时间轴，`asr` 强制媒体转写（不能用于纯字幕输入，也不能与 `--align-subtitles` 同用）。候选 QA 与最终选择会写入 `source-candidate-qa.json`、`source-selection.json`。需要登录态或代理时，必须由用户显式传入 `--cookies-from-browser` 或 `--proxy`。

### 7. 重建既有 Job 的源字幕

当媒体 Job 的源字幕 QA 出现 error 时，可显式用 ASR 重建时间轴：

```bash
uv run --no-sync xvt rebuild-source "/path/to/job.json" \
  --strategy asr \
  --cookies-from-browser "firefox" \
  --proxy "http://127.0.0.1:7890"
```

这是 advanced 原子命令，`continue` 不会自动执行。它优先复用校验通过的词级转写、规范化音频和本地或已下载媒体；只有远程 Job 缺少这些可复用内容时，才按显式 Cookie/代理重新获取。命令持久化 ASR 策略与修复意图并关闭对齐，失效旧候选 QA、source cue、选择、重分段及全部下游产物，但保留 Job 内 checksum 有效的输入译文副本，以便原始外部译文文件已移动时仍可按新时间轴重新映射。修复中断后 `status` 会继续给出显式重试命令；成功后端到端 Job 运行 `xvt continue JOB`，旧原子 Job 运行 `xvt translate JOB --target-language ...`。`status` 仅在非 ASR 源字幕 QA 为 error 且可重建时首次建议此命令；ASR 自身 QA error、`needs_review` 和译文 QA 不会重复建议重建。

### 8. 生成短片段预览

在烧录整段视频前，可以围绕指定 cue 快速检查字体、位置、换行和同步：

```bash
uv run --no-sync xvt preview "/path/to/job.json" \
  --cue 18 \
  --duration 15
```

也可以显式指定起点和输出路径：

```bash
uv run --no-sync xvt preview "/path/to/job.json" \
  --start 90 \
  --duration 15 \
  --output "/absolute/path/to/preview.mp4"
```

不传 `--cue` 或 `--start` 时，默认从第一条有效译文 cue 附近生成 15 秒预览。预览要求译文 QA 已通过，使用与正式烧录相同的字体、分辨率自适应码率和硬件回退逻辑，但不会修改正式 `burn`、`media_qa` stage，也不会覆盖 `bilingual.ass` 或 `burned.mp4`。本地纯音频 Job 以及没有有效 `source_video` 的远程 Job 必须通过 `--media` 提供视频。

### 9. 单独烧录双语字幕

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
| `translation-draft-cues.json` | 润色前的不可变译文草稿 |
| `translation-polish-batches.json` | Agent 润色批次 |
| `translation-polish-progress.json` | API/Agent 润色恢复进度 |
| `translation-polished-cues.json` | 完成润色且待统一收尾的 cue |
| `translated.srt` | 仅译文字幕 |
| `bilingual.srt` | 中英双语字幕 |
| `bilingual.ass` | 带独立中英样式的字幕 |
| `preview.ass`、`preview.mp4` | 短片段预览字幕和视频 |
| `qa.json`、`qa-translated.json` | 字幕 QA 报告 |
| `burned.mp4` | 烧录视频 |

双语输出会折叠每种语言内部的源字幕换行，确保每个 cue 固定为“译文一行 + 原文一行”。对 16:9 横版英文，64 个显示单位以内通过，65 到 72 个显示单位仅警告，超过 72 才需复核；窄画幅会自动收紧上限。中文维持更严格的单行限制。

若译文 QA 因超过硬显示阈值而失败，可对具有词级时间戳的 ASR cue 执行重切：

```bash
uv run --no-sync xvt resegment-translation "/path/to/job.json"
```

该命令只按语义优先边界重切超宽 cue，不会按字符拆旧译文；它会重建 `source-cues.json` 和 `source.srt`，使旧翻译批次、译文、QA、ASS 与烧录产物失效，并写入 `translation-resegmentation.json` 审计报告。端到端 Job 随后执行 `xvt continue JOB`，原子 Job 则按提示重新执行 `xvt translate`。用户导入的源字幕没有 ASR 词级时间戳，命令会拒绝改写其时间轴。

## 恢复与诊断

```bash
uv run --no-sync xvt status "/path/to/job.json"
uv run --no-sync xvt verify "/path/to/job.json" --json
uv run --no-sync xvt continue "/path/to/job.json" --events-jsonl
uv run --no-sync xvt resume "/path/to/job.json" --events-jsonl
uv run --no-sync xvt batch-continue "/path/to/batch.json" --events-jsonl
```

`continue` 根据 `run` 时持久化的目标补齐未完成步骤：远程检查或下载失败时会在同一个 Job 中重新获取素材，API 翻译、润色或语义校对中断会复用各自批次进度，Agent 等待会停在 `apply-translation`、`apply-polish-translation` 或 `apply-semantic-review`，烧录失败只重跑烧录。烧录成品只有在 checksum、派生指纹和媒体 QA 全部有效时才会复用；媒体 QA 失败不会被 `status` 或批处理误判为完成。`resume` 仅负责从规范化音频、词级转写或源 cue 恢复转写主链路。`verify` 会重新计算 artifact 的 SHA-256。`status` 使用同一套结构化恢复计划，区分可重试、等待输入、需要人工修复、不可自动恢复和已完成，并只把 checksum 有效的 artifact 视为可复用产物；`--json` 会额外输出 `recovery_plan`。

任务创建后会立即显示 `job.json` 路径。后续失败时，普通模式会显示失败阶段、任务路径和结构化恢复建议；已有 Job 的原子命令同样保留任务路径。用户按 Ctrl-C（SIGINT）或向进程发送 SIGTERM 时，外部命令的整个进程组会被清理，当前运行阶段记录为失败，CLI 分别返回退出码 130 和 143 并重新计算恢复计划。字幕 QA 失败只展示报告和人工修复提示，不会错误建议继续烧录。 对 `needs_review` 级的报告（例如导入字幕折叠后超宽），确认可接受后可用 `xvt continue JOB --acknowledge-subtitle-review` 显式放行：确认记录绑定当前字幕内容与具体问题集合，字幕变化或出现新的 review 问题后自动失效；error 级问题（时间轴无效、缺失译文、术语违规等）永远不能通过该通道放行。当前 schema 的 Job 缺少必填字段、stage 或正确字段类型时会返回无效输入错误，不输出 Python traceback。远程端到端任务即使尚未形成媒体 checkpoint，也可以用 `xvt continue JOB` 重试检查和下载；重新提供 `--cookies-from-browser` 或 `--proxy` 时，这些参数同时用于远程检查与素材下载。任务创建前的参数或输入错误仍需修正后重新执行。

`run`、`continue`、`batch`、`batch-continue`、`translate`、`prepare-translation`、`apply-translation`、`polish-translation`、`apply-polish-translation`、`apply-semantic-review`、`acknowledge-translation-review`、`import-translation`、`resegment-translation`、`retranslate-cues`、`apply-cue-retranslation`、`preview` 和 `burn` 支持 `--events-jsonl`。该模式保留机器接口：stdout 只输出逐行 JSON 事件，不混入普通进度文本；端到端链路还会输出 `workflow_planned`、`workflow_waiting`、`workflow_completed`、`download_progress`、`transcription_progress` 和 `burn_progress`，其中完成事件会列出 checksum 有效的用户产物，仅生成源字幕时也包含 `source.srt`。批处理会输出 `batch_started`、逐项终态和带汇总计数的 `batch_finished`，预览会输出 `preview_started`、`preview_progress` 和 `preview_completed`。`transcription_progress` 的 `determinate=true` 表示可用真实百分比（当前为 faster-whisper），MLX 使用 `determinate=false` 和 `elapsed_seconds`，只有词级时间戳验证成功后才发送 100%。完成或失败时以结构化终态事件收口；批处理存在失败项或等待输入项时不会发出 `cli_completed`。命令事件和批处理错误报告会脱敏代理账号、密码、Cookie、鉴权请求头和常见签名查询参数。

## 退出码

CLI 使用集中定义的退出码表达终态语义（见 `src/xvt/errors.py`）：

| 退出码 | 含义 |
| --- | --- |
| 0 | 命令成功完成 |
| 2 | 用户输入或配置错误，需修正后重试 |
| 3 | 外部依赖或子进程故障（`doctor` 未通过、翻译提供方故障等） |
| 4 | 内部工作流不变量被破坏，属于程序侧问题 |
| 5 | `verify` 校验未通过，artifact checksum 不一致 |
| 6 | 批处理整体完成但存在失败条目 |
| 7 | 目标 Job 或 batch 正被另一个 XVT 进程独占执行 |
| 130 | 被用户中断（SIGINT / Ctrl-C） |
| 143 | 被 SIGTERM 终止，中断状态已持久化 |

## 开发

```bash
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pytest -q
```

更多设计说明见 [架构说明](docs/ARCHITECTURE.md)，已完成和待完成工作见 [路线图](ROADMAP.md)，协作约定见 [贡献指南](CONTRIBUTING.md)。

## License

[MIT](LICENSE)
