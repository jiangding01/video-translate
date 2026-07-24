# AGENTS.md

本文件适用于项目根目录及全部子目录。进入项目后先阅读本文件，再阅读 [HANDOFF.md](HANDOFF.md)、[README.md](README.md)、[架构说明](docs/ARCHITECTURE.md) 和 [路线图](ROADMAP.md)。

## 项目定位

XVT 是一个本地优先的视频转写、字幕翻译和双语字幕烧录 CLI。它支持本地视频、音频、YouTube URL、已有源字幕和已有译文字幕，并输出 SRT、ASS 或烧录后的视频。

当前开发重点是 CLI。桌面端、Web 服务和批处理不在当前实现范围内，除非用户明确调整优先级。

## 不可破坏的约束

1. 时间轴只由 ASR 或用户提供的源字幕确定。翻译阶段不得修改 cue ID、起止时间、顺序或数量。
2. 用户提供源字幕时，默认保留原时间轴。只有用户明确要求，才运行 Whisper 对齐。
3. 翻译模型只负责文本。媒体获取、分段、时间轴、QA、渲染和烧录必须由确定性代码完成。
4. 字幕 QA 或媒体 QA 失败时，任务必须失败或等待人工处理，不能继续烧录或宣告完成。
5. API Key 只从环境变量读取。Cookie 内容、代理凭据、签名 URL、本地媒体和生成产物不得提交到仓库，也不得原样写入事件。
6. 外部命令必须以参数数组执行，不拼接 shell 字符串。
7. Job、batch manifest 和 artifact 是恢复依据。新增阶段或产物时，要同步持久化状态、checksum 和 JSONL 事件。
8. 每个 Job 当前只支持一个目标语言。不要在现有 schema 上隐式加入多目标语言。

## 代码结构

| 路径 | 职责 |
| --- | --- |
| `src/xvt/cli.py` | 参数定义、命令路由、普通输出和 JSONL 终态 |
| `src/xvt/batch.py` | 目录输入快照、串行 Job 编排、`batch.json` 原子持久化和批量恢复 |
| `src/xvt/workflow.py` | 端到端目标编排、`continue`、Agent 等待和烧录派生指纹 |
| `src/xvt/workflow_config.py` | 持久化 `job.config["workflow"]` 的类型化模型与读写边界 |
| `src/xvt/pipeline/` | stage 推进与 artifact 登记的业务主链包：`common`（共享记账）、`review`（QA 确认通道）、`transcription`（转写与恢复）、`translation`（翻译与收尾）、`retranslate`（局部重译）、`resegment`（重分段）、`burnin`（预览与烧录）；`__init__` 仅 re-export |
| `src/xvt/models.py` | Job、stage、artifact、cue 和 transcript 数据模型 |
| `src/xvt/job_store.py` | Job 创建、加载和原子写入 |
| `src/xvt/download.py` | yt-dlp 检查、字幕下载、音频或视频下载 |
| `src/xvt/media.py` | ffprobe 媒体检查 |
| `src/xvt/transcribe.py` | MLX Whisper 和 faster-whisper 适配 |
| `src/xvt/segment.py` | 词级时间戳到字幕 cue 的分段 |
| `src/xvt/subtitles.py` | 字幕解析、规范化和 SRT 输出 |
| `src/xvt/align.py` | 源字幕与 ASR 的顺序模糊对齐 |
| `src/xvt/translate.py` | 翻译批次、响应校验和译文回填 |
| `src/xvt/translation_provider.py` | OpenAI-compatible Chat Completions 调用 |
| `src/xvt/qa.py` | 时间轴、文本完整性和显示宽度检查 |
| `src/xvt/render.py` | 双语 ASS 样式和平台字体选择 |
| `src/xvt/burn.py` | FFmpeg 烧录、编码选择和进度解析 |
| `src/xvt/events.py` | JSONL 事件和通用 URL 脱敏 |
| `src/xvt/process.py` | 无 shell 外部进程、流式输出和命令参数脱敏 |
| `tests/` | 与源码模块对应的单元和流程测试 |

`workflow.py` 只负责编排，不应复制 `pipeline` 包的媒体或字幕算法。原子命令仍需可单独使用，端到端命令通过组合这些能力完成任务。`pipeline` 包内依赖方向保持无环：`common ← review ← {transcription, translation}`，`retranslate` 依赖 `translation`，`resegment` 与 `burnin` 只依赖 `common`；新增子模块时不得引入反向依赖，公共入口一律经 `__init__` re-export。

## CLI 语义

默认用户路径是 **`run` → `status` → `continue`**（批处理为 `batch` / `batch-continue`）。`status` 输出 `recovery_plan` 与 `advance_steps`，是中断后的导航入口。

- `xvt run` 创建新 Job。没有 `--target-language` 时停在源字幕；提供目标语言后继续翻译；同时提供 `--burn` 时继续烧录。
- `xvt batch` 固定本地目录输入清单并为每项创建独立 Job；`xvt batch-continue` 只按 `batch.json` 和各 Job 的恢复计划处理未完成项。
- `xvt status JOB` 展示 stage 与下一步命令（优先看这里，不要猜 resume/continue）。
- `xvt continue JOB` 读取 `job.config.workflow`，补齐未完成的翻译和烧录。它不用于没有持久化工作流目标的旧 Job。
- `xvt resume JOB` 只恢复转写 checkpoint，例如规范化音频、词级转写或源 cue（advanced）。
- `xvt translate JOB` 是独立翻译入口（advanced）。`--provider auto` 在 Base URL、模型和 API Key 完整时调用兼容 API，否则进入 Agent 模式。
- Agent 模式生成 `translation-batches.json` 并进入 `awaiting_input`。必须先运行 `apply-translation` 或 `import-translation`，再运行 `continue`。
- `xvt verify JOB` 重新计算 artifact checksum。恢复逻辑只能复用校验通过的文件。
- `xvt burn JOB` 是独立烧录入口（advanced）。本地纯音频 Job 必须通过 `--media` 提供视频。烧录成功后登记 `burn_fingerprint` artifact。

不要把 `resume` 和 `continue` 合并。前者恢复转写状态，后者完成用户最初请求的端到端目标。

## 修改流程

1. 开始编码前，用简体中文说明方案、涉及文件和验证方式，等待用户批准。
2. 单组修改超过 6 个文件时，拆成可独立验证的小任务。
3. 查找代码符号时优先使用 LSP。当前环境没有 LSP 工具时，可以降级使用 `rg`，并在进度说明中注明。
4. 使用 `apply_patch` 修改文本文件。不要覆盖用户已有的无关改动。
5. 保持实现简单。不要为尚未确认的 GUI、队列、多租户、数据库或插件体系预留抽象层。
6. 行为变化必须补测试。涉及 FFmpeg、字体、yt-dlp、翻译提供方、恢复逻辑或安全边界时，至少覆盖一个失败或边界场景。
7. CLI 参数、默认行为、artifact、事件或安全边界变化后，同步检查 README、架构、Changelog、Roadmap、Skill 和 HANDOFF。

## 验证命令

```bash
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pytest -q
uv run --no-sync xvt --help
```

涉及本机运行时依赖时，再执行：

```bash
uv run --no-sync xvt doctor
```

涉及打包时执行：

```bash
uv build
```

真实 YouTube、翻译 API 和整段烧录验收需要用户授权，并使用公开素材和用户自己的凭据。不要把这类测试加入默认测试套件。

## 完成标准

- 请求的行为已经实现，原子命令和端到端命令语义一致。
- cue ID 和时间轴没有被翻译或渲染阶段改动。
- Job、stage、artifact、checksum 和事件能够解释任务当前状态。
- 失败路径不会误报 `cli_completed` 或 `workflow_completed`。
- 测试、Ruff lint 和 Ruff formatter 全部通过。
- 对应文档已经同步，HANDOFF 中的进展和下一步仍然准确。
