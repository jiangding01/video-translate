# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的分类方式。以下记录基于首次公开版本前已验证的功能状态。

## [Unreleased]

### Changed

- 端到端推进改为 Goals → plan → execute：`workflow_goals` / `plan_advance_steps` 从 `WorkflowConfig` 与当前 artifact 推导剩余步骤（`translate` / `import_translation` / `await_agent_translation` / `burn`），`_advance_workflow` 只按计划执行；`planned_run_steps` 统一 `run` 首跑的 `WORKFLOW_PLANNED` 步骤列表。行为与原先 imperative 分支一致（仍以 `target_language` / `burn` 为权威目标），便于测试与后续 status 复用。

- 端到端 `job.config["workflow"]` 收敛为类型化 `WorkflowConfig`（`src/xvt/workflow_config.py`）：`from_dict`/`to_dict` 校验已知字段类型与默认值；未知顶层字段进入 `extras` 并在写回时保留，避免指纹更新抹掉新版本扩展；`requested_outputs` 保留未知输出名以维持前向兼容。`WorkflowOptions.persisted_config` 返回结构体，落盘边界仍序列化为既有 JSON 形状。`workflow`、`recovery`、`pipeline` 与 CLI 提示统一经 `get_workflow_config` / `require_workflow_config` / `resolve_target_language` / `clear_burn_fingerprint` 读写，消除对 dict 袋的散落 `config.get` 与目标语言双处解析。纯内部收敛，旧 Job 可继续加载；已知字段类型错误在边界显式失败。

- `pipeline.py`（2385 行单体）拆分为 `src/xvt/pipeline/` 包，纯搬移、零行为变化：`common`（共享记账与失效 helper）、`review`（QA 确认通道）、`transcription`（转写与恢复主链）、`translation`（翻译主链与统一收尾）、`retranslate`（局部重译）、`resegment`（语义重分段）、`burnin`（预览与烧录），`__init__` 仅保留 re-export 门面与 `__all__` 公共 API 清单；依赖方向无环（`common ← review ← {transcription, translation}`，`retranslate ← translation`，`resegment`/`burnin` 仅依赖 `common`）。测试同簇拆分为 `test_pipeline_{transcription,translation,retranslate,resegment,burnin}.py`，全部 mock patch 目标随调用点迁移到对应子模块命名空间；三处源码扫描守卫测试（artifact kind 覆盖、config 默认值、事件裸字符串/信封）改为递归扫描以覆盖包内子模块。

- artifact 依赖图与失效闭包收敛到单一来源（`src/xvt/artifacts.py`）：新增 `ArtifactKind` 枚举登记全部产物种类、`DERIVES_FROM` 依赖边表和 `downstream_of` 传递闭包函数；`pipeline.py` 中 `_invalidate_rendered_artifacts`、`_invalidate_preview_artifacts` 和 `resegment_translation` 的三处失效集合改由闭包推导，不再各自维护硬编码集合。新增测试将三处历史集合钉为基线并校验依赖图无环，行为与现状完全一致。
- workflow 配置默认值收敛到单一来源（`src/xvt/config.py`）：新增纯常量模块集中定义 `DEFAULT_ENGINE`、`DEFAULT_SEGMENTATION_PRESET`、`DEFAULT_PROVIDER`、`DEFAULT_BATCH_SIZE`、`DEFAULT_MAX_RETRIES`、`DEFAULT_TIMEOUT_SECONDS`、`DEFAULT_API_KEY_ENV`、`DEFAULT_TRANSLATION_STYLE`、`DEFAULT_MIN_ALIGNMENT_CONFIDENCE`、`DEFAULT_HARDWARE_ENCODING`；`cli.py` 各子命令参数默认值、`WorkflowOptions` 字段默认值以及 `pipeline.py`/`workflow.py` 读取持久化 config 的手写兜底改引常量。纯内部收敛，无行为或数值变化。
- 集中定义错误模型与退出码（`src/xvt/errors.py`）：新增 `ExitCode` 枚举和 `XvtError` 领域异常层次（`UserInputError`、`DependencyError`、`InternalError`）；顶层异常映射优先识别领域异常并沿用其退出码与 category，旧的按 Python 异常类型兜底映射保持不变。
- **Breaking**：拆分冲突的退出码语义。`verify` 校验未通过由 `4` 改为 `5`（VERIFY_FAILED），`batch`/`batch-continue` 存在失败条目由 `4` 改为 `6`（PARTIAL_FAILURE）；`doctor` 未通过仍为 `3`，中断仍为 `130`。依赖脚本请同步更新退出码判断。

### Added

- 翻译进度可预估：`translation_batch_started` / `translation_batch_retrying` / `translation_batch_completed` 事件新增可选字段 `batch_total`（批次总数，v1 兼容性变更不升协议版本）；CLI 人类可读输出的翻译批次行改为「批次 N/总数」，重试行同时显示事件里已有的 `reason` 字段（已知原因在渲染层映射为中文，契约字符串保持英文，未知原因回退原文）；旧事件流缺少 `batch_total` 时回退为仅显示批次 ID。
- `xvt continue --acknowledge-subtitle-review`：人工确认 `needs_review` 的字幕 QA 报告后继续翻译与烧录（导入字幕折叠后超宽等场景）。确认绑定字幕 checksum 与具体 review 问题集合、字幕变化或新问题自动失效、error 级永不放行；放行以 `subtitle_review_acknowledged` 事件与 config 记录留痕，原始 QA 报告保持不变。

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
- 外部命令以独立进程组执行：超时、取消或中断时清理全部子孙进程，不再遗留 ffmpeg/yt-dlp 孤儿进程；提供静默超时机制（默认不启用，由保证持续输出的调用方显式设置——yt-dlp 提取阶段等合法静默场景不受误杀），并新增协作式取消入口（`CommandCancelled`）。
- CLI 改为命令级信号作用域：进入 `main()` 复位取消状态、退出恢复原信号处理器（同进程多次调用互不污染）；第二次 Ctrl-C/SIGTERM 不再产生裸 traceback，只保持取消状态直至清理完成。
- CLI 现在处理 SIGTERM：与 Ctrl-C 一样清理进程组、持久化中断的 stage 并生成恢复计划，退出码为 143；翻译批次循环和批处理条目在迭代边界响应取消。
- Job 与 batch 全生命周期独占执行：flock 锁自资源首次持久化点持有至终态——既有 Job 子命令与 `batch-continue` 在 CLI 入口、在途 `run` 自 Job 创建起、`batch` 自 batch.json 写出前；第二个进程以退出码 7 立即失败并提示持有者 PID；进程崩溃时内核自动释放锁。含真实跨进程冲突测试。

- stage 跳过决策收敛到 `stages.py` 计划表：入口在业务逻辑开始前一次性标记跳过项（先计划后执行），取代散落在各分支的逐个记账；中途失败时计划内的跳过状态已持久化。
- run 首跑与 `continue` 续跑统一为同一推进函数：转写完成后的翻译、Agent 等待与烧录决策只有一份实现，首跑烧录同样走指纹感知路径；`workflow_completed` 与等待事件的 artifacts 统一为 checksum 有效的用户产物。
- 翻译收尾统一为单一 `_finalize_translation`：局部重译、Agent/API 响应回填和导入译文字幕三条路径共用同一段「写译文 cue → 失效下游 → 译文 QA → 渲染」逻辑，消除三份重复骨架，无行为变化。

### Fixed

- 再加固事件脱敏（P7h）：敏感头命中后一律吞到行尾（不再解析引号/转义，堵住转义引号 Digest 泄漏）；URL userinfo 放宽为 @ 前一切（覆盖无用户名、无冒号形态）；事件 data 中命中敏感 key（Cookie/Authorization/api_key/password/token 等）的字典值整体清空，覆盖嵌套 Header 字典。
- 加固事件自由文本脱敏（P7g）：Cookie 等敏感头的值改为吞到闭引号或行尾（覆盖多 Cookie 对、Digest 后续字段、字典形态 Header），并新增任意协议 URL userinfo 脱敏（socks5:// 等）；非 UTF-8 的 provider 响应体纳入 `ProviderResponseError` 边界；batch 条目取 Job 锁失败时不再于无锁状态下重读恢复计划。
- 修复事件自由文本脱敏缺口：外部命令的原始 stderr/stdout 进入事件错误消息时，`Authorization`/`Proxy-Authorization`/`Cookie`/`Set-Cookie`/`X-Api-Key` 头与 `Bearer` 令牌现在会在事件边界统一脱敏（此前只脱敏 URL，凭据可能原样进入 JSONL）。
- Job 锁提前到首次持久化之前：`create_job` 在写出 `job.json` 前即持有目录锁，消除「文件已出现、锁尚未取得」的并发窗口；batch 对已有 Job 的恢复分类与 artifact 校验同样改为先取 Job 锁再读取，避免按旧状态误判条目完成。
- provider 响应解析边界统一包装为 `ProviderResponseError`：HTTP 200 非 JSON 响应体、模型输出 JSON 语法错误、cue ID 缺失/重复/多余、译文为空等结构性错误在纠错重试耗尽后按依赖故障退出码 3 分类，不再被误报为用户输入错误退出码 2。
- 重分段后旧 `subtitle_qa` 报告一并失效：它派生自 source cue 且不会被立即重建，此前以 checksum 有效姿态残留，污染 `status`/`verify` 与恢复复用清单。
- 修复 P2 引入的翻译重试回归：结构性无效的 provider 响应（缺 `items`、报文不完整、内容非文本）改抛专用 `ProviderResponseError`，重新进入携带纠正提示词的纠错重试；重试耗尽后按依赖故障分类（退出码 3）。`TranslationProviderError` 一并归入 `DependencyError` 层次。
- 修复媒体 QA 失败但 `burned_video` checksum 有效时，恢复计划、批处理或续跑可能错误宣告任务完成的问题。
- 修复仅生成源字幕时 `workflow_completed` 和 `cli_completed` 没有返回 `source.srt` 的问题。
- 损坏或字段不完整的当前版本 Job 现在返回可控的无效输入错误，不再泄漏 Python traceback。
- 修复转写失败或缺少词级时间戳时可能误报完成进度的问题。
- 修复预览未继承工作流烧录媒体、字体和编码设置，以及自定义输出可能覆盖 Job、源媒体或正式 artifact 的问题；预览中断现在提供独立重试提示。
- 修复 `resegment-translation` 失效集合遗留 `cue_retranslation_batches`/`cue_retranslation_response`（P6a 为保持行为而排除）的问题：这两类 artifact 在重分段后引用的是废弃 cue id，虽然 `apply-cue-retranslation` 会因 `translated_cues` 已失效而拒绝执行、无正确性漏洞，但会以 checksum 有效的姿态污染 `status`、`verify` 和恢复计划的可复用产物清单；现按依赖图闭包一并失效（P7e）。
- 修复 P7c 终审发现的 v1 事件信封失配：`cli.py` 的 `cli_failed` 顶层裸 `category`/`message` 改为嵌套 `error_event_fields` 信封；`pipeline.py` 中 `remote_subtitles_selected`、`translation_resegmented`、`translation_fallback`、`translation_progress_reset`、`translation_batch_started`、`translation_batch_retrying`（两处）、`translation_batch_completed`、`translation_provider_selected`、`preview_completed` 共 10 处补齐缺失的 `job_file`；`batch.py` 的 `_mark_unfinished_item` 补齐 `batch_item_failed`/`batch_item_waiting` 缺失的 `index`/`total`。`docs/EVENTS.md` 同步对账，新增载荷结构契约测试（`tests/test_events.py`）防止回归。

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
