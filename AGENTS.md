# AGENTS.md

本文件适用于项目根目录及全部子目录。先阅读本文件，再按受影响面选读：进行中的工作与下一步看 [HANDOFF.md](HANDOFF.md)；工作流、恢复或回填改动看 [架构说明](docs/ARCHITECTURE.md) 与 [恢复与回填契约](docs/RECOVERY.md)；用户可见行为看 [README.md](README.md) 与 `xvt --help`；范围与优先级看 [路线图](ROADMAP.md)。

## 项目定位

XVT 是一个本地优先的视频转写、字幕翻译和双语字幕烧录 CLI。它支持本地视频、音频、YouTube URL、已有源字幕和已有译文字幕，并输出 SRT、ASS 或烧录后的视频。

当前开发重点是 CLI，包含已有的目录批处理能力。桌面端和 Web 服务不在当前实现范围内，除非用户明确调整优先级。

## 不可破坏的约束

1. 时间轴只由 ASR 或用户提供的源字幕确定。翻译阶段不得修改 cue ID、起止时间、顺序或数量。
2. 用户提供源字幕时，默认保留原时间轴。只有用户明确要求，才运行 Whisper 对齐。
3. 翻译模型只负责文本。媒体获取、分段、时间轴、QA、渲染和烧录必须由确定性代码完成。
4. 字幕 QA 或媒体 QA 失败时，任务必须失败或等待人工处理，不能继续烧录或宣告完成。已知画幅的译文行宽以实际 ASS 字体、字号和描边测量为准；测量异常回退保守规则，确认绑定布局，不跨画幅或字体复用。人工译文 `apply --check` 必须只读，正式 apply 在写入前重复预检。
5. API Key 只从环境变量读取。Cookie 内容、代理凭据、签名 URL、本地媒体和生成产物不得提交到仓库，也不得原样写入事件。翻译提供方的 Base URL 与模型可以固化进 Job，但只在首个批次成功后写回；API Key 永不落盘，任何会持久化的错误文本都必须先脱敏。
6. 外部命令必须以参数数组执行，不拼接 shell 字符串。
7. Job、batch manifest 和 artifact 是恢复依据。新增阶段或产物时，要同步持久化状态、checksum 和 JSONL 事件。烧录成品重新关联只能由 checksum 有效的 v2 proof 授权，proof 必须绑定 fingerprint、成品 checksum 和实际源媒体内容 checksum；legacy fingerprint 不得绕过重烧。
8. 每个 Job 当前只支持一个目标语言。不要在现有 schema 上隐式加入多目标语言。
9. 产物有效性只认内容 checksum。size、mtime 等文件元数据只能用于**证伪**（与登记值不符即失效），不得用于**证实**有效；`cp -p`、`rsync -a` 和备份还原都会保留 mtime。

## 代码结构

| 路径 | 职责 |
| --- | --- |
| `src/xvt/cli.py` | 参数定义、命令路由、普通输出和 JSONL 终态 |
| `src/xvt/batch.py` | 目录输入快照、串行 Job 编排、`batch.json` 原子持久化和批量恢复；中断兜底与恢复分类见 [恢复与回填契约](docs/RECOVERY.md#批处理恢复分类与中断兜底) |
| `src/xvt/workflow.py` | 端到端目标编排（Goals → `plan_advance_steps` → 执行）、`continue`、Agent 等待和烧录派生指纹；译文满足度含 SRT 与译文 QA 报告，缺渲染或 QA 报告时走 `complete_translated_outputs`；`plan_advance_steps` 在函数体最前判定局部重译等待（`await_cue_retranslation`），因为它会叠加在一个译文目标已满足的 Job 上；完整判定顺序见 [恢复与回填契约](docs/RECOVERY.md#端到端推进计划的判定顺序) |
| `src/xvt/workflow_config.py` | 持久化 `job.config["workflow"]` 的类型化模型与读写边界 |
| `src/xvt/checksums.py` | 文件内容 SHA-256 的唯一实现（零依赖叶子，供 recovery / candidate_qa / pipeline.common 共用） |
| `src/xvt/recovery.py` | checksum 校验、`RecoveryPlan` / `advance_steps`、与 planner 对齐的 status 恢复建议 |
| `src/xvt/artifacts.py` | artifact kind 枚举、派生依赖图与失效闭包 |
| `src/xvt/pipeline/` | stage 推进与 artifact 登记的业务主链包：`common`（共享记账）、`review`（QA 与人工确认）、`revision`（人工译文修订）、`transcription`（转写与恢复）、`translation`（翻译与收尾）、`polish`（可恢复润色）、`semantic`（语义校对与一次修复）、`retranslate`（局部重译）、`resegment`（重分段）、`burnin`（预览与烧录）；`__init__` 仅 re-export |
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
| `src/xvt/candidate_qa.py` | 候选译文 QA 判定，以及 QA message 中行宽/上限的**唯一**解析实现（`overflow_limits`，零依赖叶子，供 recovery / pipeline.revision 共用）；其它模块一律复用它，不得就地重造这条正则 |
| `src/xvt/render.py` | 双语 ASS 样式和平台字体选择 |
| `src/xvt/burn.py` | FFmpeg 烧录、编码选择和进度解析 |
| `src/xvt/paths.py` | 烧录/预览输出绝对路径与覆盖保护（源媒体、job 文件、已登记 artifact） |
| `src/xvt/events.py` | JSONL 事件和通用 URL 脱敏 |
| `src/xvt/process.py` | 无 shell 外部进程、流式输出和命令参数脱敏 |
| `tests/` | 与源码模块对应的单元和流程测试 |

`workflow.py` 只负责编排，不应复制 `pipeline` 包的媒体或字幕算法。原子命令仍需可单独使用，端到端命令通过组合这些能力完成任务。`pipeline` 包内依赖方向保持无环：`common ← review ← {transcription, translation, burnin}`，`semantic` 与 `retranslate` 依赖 `translation`，`resegment` 只依赖 `common`，`burnin` 依赖 `common` 与 `review`（烧录/预览与 workflow 共用同一 QA 放行谓词），`polish` 依赖 `translation`，`revision` 只依赖 `common`；新增子模块时不得引入反向依赖，公共入口一律经 `__init__` re-export。

## CLI 语义

默认用户路径是 **`run` → `status` → `continue`**（批处理为 `batch` / `batch-continue`）。`status` 输出 `recovery_plan` 与 `advance_steps`，与 `continue` 共用 `plan_advance_steps` 判定，是中断后的导航入口。

- `xvt run` 创建新 Job。没有 `--target-language` 时停在源字幕；提供目标语言后继续翻译；同时提供 `--burn` 时继续烧录。停在 `awaiting_input` 时退出码默认为 0（Agent 协议的正常步骤），只有显式 `--fail-on-waiting` 才返回 10；该旗标不发 `cli_failed`、不改事件流，只改退出码。
- `xvt batch` 固定本地目录输入清单并为每项创建独立 Job；`xvt batch-continue` 只按 `batch.json` 和各 Job 的恢复计划处理未完成项。等待项绝不由批处理自动推进，已登记烧录成品缺失时绝不自动重烧，一律停在 `awaiting_input` 等用户显式 `relink-burned-output` 或 `continue`。`--fail-on-waiting` 下有等待条目返回 10，有失败条目仍返回 6（**6 优先于 10**）。恢复分类、`degraded` 标注与各目标产物的去向详见 [恢复与回填契约](docs/RECOVERY.md#批处理恢复分类与中断兜底)。
- `xvt status JOB` 展示 stage、`advance_steps` 与下一步命令（优先看这里，不要猜 resume/continue）。产物有效性判定与 `continue` 使用**同一套全量 checksum 校验**，只是同一次判定内对同一文件只哈希一次。它对缺失烧录成品只做静态资格判断，不额外计算源媒体内容 checksum。记忆化边界与元数据证伪/证实的界线详见 [恢复与回填契约](docs/RECOVERY.md#产物有效性校验与-checksum-记忆化)。
- `xvt continue JOB` 读取 `job.config.workflow`（类型化为 `WorkflowConfig`），补齐未完成的翻译和烧录。它不用于没有持久化工作流目标的旧 Job。等待态退出码同 `run`（默认 0，`--fail-on-waiting` 时 10）。
- `xvt resume JOB` 只恢复转写 checkpoint，例如规范化音频、词级转写或源 cue（advanced）。
- `xvt rebuild-source JOB --strategy asr` 只在用户明确要求时以 ASR 重建媒体 Job 的源时间轴（advanced）。它优先复用 checksum 有效的 words/audio checkpoint，失效旧 source cue 及全部下游产物，并保留 Job 内有效的输入译文副本供后续重新映射；纯字幕 Job 不支持。
- `xvt prepare-source-review JOB --cue ID` / `apply-source-review-fixes JOB REVIEW` 显式修订源 cue 文本，保留 ID、时间轴、words 和 provenance，立即重跑源字幕 QA，并失效旧翻译及全部下游；导入译文工作流不支持。
- `xvt translate JOB` 是独立翻译入口（advanced）。`--provider auto` 在 Base URL、模型和 API Key 完整时调用兼容 API，否则进入 Agent 模式。
- Agent 模式生成 `translation-batches.json` 并进入 `awaiting_input`。必须先回填再运行 `continue`。
- `xvt apply JOB RESPONSE` 是统一回填入口：人工修订模板按 `task` 字段路由，人工修订 SRT 按固定文件名识别，Agent 响应按 Job 当前等待步骤路由。**路由不出来时报错而不猜测**，改用对应的显式 `apply-*` 命令（它们保留为稳定入口，语义零改动）。**新增等待步骤必须在 `cli._STEP_ROUTES` 与 `cli._UNROUTABLE_AWAIT_STEPS` 两张表之一里表态**，守卫测试强制。`--continue` 透传、源文修订拒绝该组合的理由与各场景推荐命令详见 [恢复与回填契约](docs/RECOVERY.md#xvt-apply-统一回填路由)。
- `--translation-quality polished` 在润色候选后必须完成 Q6 语义校对；API 最多修复一次并复查，Agent 使用 `apply-semantic-review` 回填合并响应。仍有 review 问题时运行 `continue --acknowledge-translation-review`；确认不能绕过确定性字幕 QA。
- `xvt polish-translation JOB` 为已有端到端 Job 显式补跑润色；API 模式逐批 checkpoint，Agent 模式必须先运行 `apply-polish-translation`，再由 `continue` 进入语义校对。
- `xvt verify JOB` 重新计算 artifact checksum。恢复逻辑只能复用校验通过的文件。
- `xvt burn JOB` 是独立烧录入口（advanced）。本地纯音频 Job 必须通过 `--media` 提供视频。该原子命令可覆盖持久化 workflow 之外的媒体、字体和编码参数，因此不签发 workflow v2 proof；只有 `run` / `continue` 按持久化目标完成的烧录才登记 checksum 保护的 `version=2` proof，详见 [恢复与回填契约](docs/RECOVERY.md#烧录-proof-与成品重新关联资格)。
- `xvt relink-burned-output JOB EXISTING_VIDEO` 是已移动烧录成品的唯一重新关联入口（advanced）。**只认 checksum 有效的 v2 proof**，全部校验项匹配才放行，重复 `burned_video` 一律 fail closed；不具备资格的 Job 按 `retryable` 由 `continue` 重烧。完整匹配条件详见 [恢复与回填契约](docs/RECOVERY.md#烧录-proof-与成品重新关联资格)。

不要把 `resume` 和 `continue` 合并。前者恢复转写状态，后者完成用户最初请求的端到端目标。

## 修改流程

1. 开始编码前，用简体中文说明方案、涉及文件和验证方式，等待用户批准。
2. 单组修改超过 6 个文件时，拆成可独立验证的小任务。
3. **分支与 PR：默认单分支串行**（实现 → review → 提交 → 开 PR → 合 `main` → 再开下一项）。不要多条互不依赖的功能分支同时对着 `main` 开 PR。确需拆分时，后继分支必须基于**前置功能分支**切开（叠放），不要各自从 `main` 并行发散。
4. 查找代码符号时优先使用 LSP。当前环境没有 LSP 工具时，可以降级使用 `rg`，并在进度说明中注明。
5. 使用 `apply_patch` 修改文本文件。不要覆盖用户已有的无关改动。
6. 保持实现简单。不要为尚未确认的 GUI、队列、多租户、数据库或插件体系预留抽象层。
7. 行为变化必须补测试。涉及 FFmpeg、字体、yt-dlp、翻译提供方、恢复逻辑或安全边界时，至少覆盖一个失败或边界场景。
8. CLI 参数、默认行为、artifact、事件或安全边界变化后，同步检查 README、架构、恢复契约、Changelog、Roadmap、Skill 和 HANDOFF。
9. **提交前硬门槛（不可跳过）**：
   1. 对照 diff 做一遍代码 review（正确性、边界、与既有语义一致性、安全边界、文档是否漏改）。
   2. 有问题先修再测；未修干净不得 `git commit`。
   3. 跑通约定验证命令（至少 ruff + 相关/全量 pytest）；通过后才提交。
   4. 向用户简要说明 review 结论（无问题 / 修了什么）后再推送或开 PR。

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

发版流程：升 `pyproject.toml` 与 `src/xvt/__init__.py` 版本号 → CHANGELOG 落版本节 → `uv build` → 合并 `main` 后打 annotated tag `vX.Y.Z` 并推送。历史版本 tag 已补齐（`v0.1.0`、`v0.1.1`）。

真实 YouTube、翻译 API 和整段烧录验收需要用户授权，并使用公开素材和用户自己的凭据。不要把这类测试加入默认测试套件。

## 完成标准

- 请求的行为已经实现，原子命令和端到端命令语义一致。
- cue ID 和时间轴没有被翻译或渲染阶段改动。
- Job、stage、artifact、checksum 和事件能够解释任务当前状态。
- 失败路径不会误报 `cli_completed` 或 `workflow_completed`。
- 测试、Ruff lint 和 Ruff formatter 全部通过。
- 对应文档已经同步，HANDOFF 中的进展和下一步仍然准确。
