# AGENTS.md

本文件适用于项目根目录及全部子目录。先阅读本文件，再按受影响面选读：进行中的工作与下一步看 [HANDOFF.md](HANDOFF.md)；工作流、恢复或回填改动看 [架构说明](docs/ARCHITECTURE.md) 与 [恢复与回填契约](docs/RECOVERY.md)；用户可见行为看 [README.md](README.md) 与 `xvt --help`；范围与优先级看 [路线图](ROADMAP.md)。HANDOFF 的历史条目在 [HANDOFF 历史档案](docs/HANDOFF_ARCHIVE.md)，**那份只用 `rg` / `grep` 按需检索，不要整份读入上下文**。

## 项目定位

XVT 是一个本地优先的视频转写、字幕翻译和双语字幕烧录 CLI。它支持本地视频、音频、YouTube URL、已有源字幕和已有译文字幕，并输出 SRT、ASS 或烧录后的视频。

当前开发重点是 CLI，包含已有的目录批处理能力。桌面端和 Web 服务不在当前实现范围内，除非用户明确调整优先级。

## 不可破坏的约束

1. 时间轴只由 ASR 或用户提供的源字幕确定。翻译阶段不得修改 cue ID、起止时间、顺序或数量。
2. 用户提供源字幕时，默认保留原时间轴。只有用户明确要求，才运行 Whisper 对齐。
3. 翻译模型只负责文本。媒体获取、分段、时间轴、QA、渲染和烧录必须由确定性代码完成。
4. 字幕 QA 或媒体 QA 失败时，任务必须失败或等待人工处理，不能继续烧录或宣告完成。已知画幅的译文行宽以实际 ASS 字体、字号和描边测量为准；测量异常回退保守规则，确认绑定布局，不跨画幅或字体复用。同一条 `run` / `continue` 里烧录腿复用渲染腿已测的那份布局 QA 时，必须绑定该报告 `layout` 块所记的画幅与 ASS 样式（见 [恢复与回填契约](docs/RECOVERY.md#烧录腿复用渲染腿的布局-qa-与-ass)），且放行判定仍排在复用判定之前——复用只省掉结论必然相同的重算，不得省掉任何一次判定。人工译文 `apply --check` 必须只读，正式 apply 在写入前重复预检。
5. API Key 只从环境变量读取。Cookie 内容、代理凭据、签名 URL、本地媒体和生成产物不得提交到仓库，也不得原样写入事件。翻译提供方的 Base URL 与模型可以固化进 Job，但只在首个批次成功后写回，且 `resolve_provider` 在确认一条 Base URL 真的会被用来发请求时拒绝带用户名密码或 query 串的地址（`InvalidURL` 消息不带 scheme，脱敏规则够不着它；配置不齐全时 `auto` 仍按契约降级，那条 Base URL 既不发请求也不落盘）；API Key 永不落盘。错误文本的脱敏在**边界**集中做，不靠调用方自觉：事件走 `create_event`，`job.json` 走 `pipeline.common.set_stage`（它在写 `record.error` 之前统一调用 `redact_text`，两条出口给出同一份文本）。新增持久化出口时要么接入既有边界，要么在新边界上补一道，不要在调用点逐处补 `redact_text`。
6. 外部命令必须以参数数组执行，不拼接 shell 字符串。`src/xvt/` 下只有 `process.py` 可以 import `subprocess`，全仓任何位置都不得出现 `shell=True`、`os.system` 或 `os.popen`。这条由 `tests/test_subprocess_boundary.py` 的 AST 守卫强制，不再只靠自觉。
7. Job、batch manifest 和 artifact 是恢复依据。新增阶段或产物时，要同步持久化状态、checksum 和 JSONL 事件。烧录成品重新关联只能由 checksum 有效的 v2 proof 授权，proof 必须绑定 fingerprint、成品 checksum 和实际源媒体内容 checksum；legacy fingerprint 不得绕过重烧。
8. 每个 Job 当前只支持一个目标语言。不要在现有 schema 上隐式加入多目标语言。
9. 产物有效性只认内容 checksum。size、mtime 等文件元数据只能用于**证伪**（与登记值不符即失效），不得用于**证实**有效；`cp -p`、`rsync -a` 和备份还原都会保留 mtime。

## 代码结构

| 路径 | 职责 |
| --- | --- |
| `src/xvt/cli.py` | 参数定义、命令路由、普通输出和 JSONL 终态 |
| `src/xvt/batch.py` | 目录输入快照、串行 Job 编排、`batch.json` 原子持久化和批量恢复；中断兜底与恢复分类见 [恢复与回填契约](docs/RECOVERY.md#批处理恢复分类与中断兜底) |
| `src/xvt/workflow.py` | 端到端目标编排（Goals → `plan_advance_steps` → 执行）、`continue`、Agent 等待和烧录派生指纹；译文满足度含 SRT 与译文 QA 报告，缺渲染或 QA 报告时走 `complete_translated_outputs`；`plan_advance_steps` 在函数体最前判定局部重译等待（`await_cue_retranslation`），紧接着判定整轮 Agent 翻译等待（`agent_full_translation_waiting` + 公有单点 `full_translation_request_is_newest`，只让 checksum 有效的取代 kind 参与登记次序比较，`recovery` 原子段的语义等待让路复用同一份组合），两者都必须排在 `_translation_satisfied` **之前**，因为它们会叠加在一个译文目标已满足的 Job 上；新增等待步骤必须就这道闸门表态（写一条闸门之前的早退，或登记进 `_SATISFIED_GATED_AWAIT_STEPS` 并说明理由，`AwaitStepsCoverageTest` 用 AST 强制）；完整判定顺序见 [恢复与回填契约](docs/RECOVERY.md#端到端推进计划的判定顺序)；`_advance_workflow` 在发 `workflow_completed` 前有 fail-closed 终检；媒体 QA 放行判定（`media_qa_failure` / `media_qa_passed`）、烧录派生指纹是否成立（`_burn_fingerprint_satisfied`，内含「快路径按 `{path, size, mtime_ns}` 比对、失配后再以 v2 proof 登记的源媒体身份与内容 checksum 回退」两段判定）与 v2 proof 指纹比对（`_burn_proof_fingerprint_matches`，指纹部分复用前者）各只有一份实现，烧录满足度、relink 资格、`relink_burned_output` 与 `recovery` 的原因文案一律复用，不得就地重造 |
| `src/xvt/workflow_config.py` | 持久化 `job.config["workflow"]` 的类型化模型与读写边界 |
| `src/xvt/config.py` | Workflow 默认值常量的单一事实来源（纯常量、零 xvt 依赖，CLI 默认值与持久化 config 的手写兜底都引用这里） |
| `src/xvt/checksums.py` | 文件内容 SHA-256 的唯一实现（零依赖叶子，供 recovery / candidate_qa / pipeline.common 共用） |
| `src/xvt/recovery.py` | checksum 校验、`RecoveryPlan` / `advance_steps`、与 planner 对齐的 status 恢复建议 |
| `src/xvt/artifacts.py` | artifact kind 枚举、派生依赖图与失效闭包 |
| `src/xvt/pipeline/` | stage 推进与 artifact 登记的业务主链包：`context`（翻译上下文候选包的落盘与登记，只依赖 `common`；不改 stage、不新增事件类型、不接 `plan_advance_steps`）、`common`（共享记账，并承载翻译策略回退的**唯一**实现 `resolved_translation_policy`：快照优先、顶层 `translation_policy` 兜底，翻译侧与译文 QA 侧必须同口径，否则术语死锁；以及「Job 内已存译文还能不能套回当前 `source_cues`」的**唯一**只读谓词 `pending_translation_import_blocker`，由 `status` / `continue` / `rebuild-source` 收尾三处消费，重叠度量复用 `translate.map_subtitle_translations`，不得就地重造）、`review`（QA 与人工确认）、`revision`（人工译文修订）、`transcription`（转写与恢复，2026-10-20 按长函数治理约定（F13）拆成子包，`__init__` 仅 re-export：`transcription/shared`（三簇共用的候选 QA 登记、源选择记账、源字幕 QA 与 SRT 渲染，零兄弟依赖的叶子，分段 preset 查询的唯一实现 `_segmentation_config` 也在这里）、`transcription/local`（本地输入腿：`_process_adaptive_media` 现代主路径、音频规范化与 Whisper 调用等 ASR 机制、纯字幕输入 Job）、`transcription/remote`（远程获取腿：探测、字幕与媒体下载、获取后转写）、`transcription/resume`（`resume` 与 `rebuild-source`）、`transcription/dispatch`（`run_transcription` 的输入类型分派）；`remote` 的 `_process_acquired_media` 与 `resume` 的 `resume_job` 对齐分支是仅缺 `source_strategy` 旧 Job 可达的冻结 legacy 路径，不与现代路径同步）、`translation`（翻译与收尾，并承载 provider 批次循环与重试的唯一实现 `run_provider_batches` / `translate_batch_with_retries`，以及导入译文的放弃入口 `drop_imported_translation`）、`polish`（可恢复润色）、`semantic`（语义校对与一次修复；API 与 Agent 两条链路共用两个模块私有 helper，不得就地重造：终态收口的**唯一**实现 `_conclude_semantic_review`（仍有语义 issue 或阻塞性候选 QA issue 就停 `awaiting_input` 并补人工输入模板，否则收口候选译文，三处调用只差 `issues` 来源与 `mode`），以及 issue 按类别分拣的**唯一**实现 `_partition_issues`（可定向修复 / 必须保留的 `source_suspect`））、`semantic_state`（语义链路只读纯函数：`semantic_review_target` 及其批次重算与指纹、报告与 issue 的解析/校验/序列化、人工修订范围、repair checkpoint 读取；零副作用叶子，包内只依赖 `common`，供 review / revision / semantic 共用，不写盘不登记 artifact 不改 stage）、`retranslate`（局部重译）、`resegment`（重分段）、`burnin`（预览与烧录，并承载「工作流烧录腿能否复用渲染腿已产出的布局 QA 与 ASS」的**唯一**判定 `_reusable_rendered_layout`：绑定报告自己的 `layout` 块所记的画幅、ASS 样式与 `wrap` 换行规则版本、就地重判媒体时长，任一不成立照旧现场重测重渲；原子 `xvt burn` / `xvt preview` 不走这条）；`__init__` 仅 re-export |
| `src/xvt/models.py` | Job、stage、artifact、cue 和 transcript 数据模型 |
| `src/xvt/stages.py` | Stage 计划表：给定输入组合声明转写阶段跳过哪些 stage（先计划、后执行的单一事实来源） |
| `src/xvt/job_store.py` | Job 创建、加载和原子写入 |
| `src/xvt/download.py` | yt-dlp 检查、字幕下载、音频或视频下载 |
| `src/xvt/media.py` | ffprobe 媒体检查 |
| `src/xvt/transcribe.py` | MLX Whisper 和 faster-whisper 适配 |
| `src/xvt/segment.py` | 词级时间戳到字幕 cue 的分段；禁則処理（行首行尾禁则）的**唯一**谓词 `forbidden_break`——cue 边界有四个产出点（主循环的句末/停顿/超时切、`_soft_cut`、`_partition_words` 的宽度切，以及展示侧译文超宽换行 `wrap_target_lines`），四处都走它，新增切分出口必须接入 |
| `src/xvt/subtitles.py` | 字幕解析、规范化和 SRT 输出 |
| `src/xvt/align.py` | 源字幕与 ASR 的顺序模糊对齐 |
| `src/xvt/translate.py` | 翻译批次、响应校验和译文回填 |
| `src/xvt/translation_provider.py` | OpenAI-compatible Chat Completions 调用 |
| `src/xvt/translation_context.py` | 零依赖叶子：源 cue 的确定性翻译上下文候选抽取（专名、术语、主题线索）。主判据「只出现大写形态且至少一次非句首」（规则 D）在真实英文字幕上实测精度与召回各 6/6，基线由 `tests/test_translation_context.py` 逐词钉住；被实测否决的两条替代判据（频次门槛、只收形态可辨的）在模块 docstring 里写了代价，不要捡回来。只产出**候选**，永不自动成为强制术语——`--glossary` 的 `TranslationPolicy.terms` 仍是唯一进确定性 QA 的强约束 |
| `src/xvt/textwidth.py` | 零依赖叶子：显示宽度与显示单位上限等六个纯宽度函数，`qa.py` 原名 re-export |
| `src/xvt/qa.py` | 时间轴、文本完整性和显示宽度检查 |
| `src/xvt/candidate_qa.py` | 候选译文 QA 判定，以及 QA message 中行宽/上限的**唯一**解析实现（`overflow_limits`，零依赖叶子，供 recovery / pipeline.revision 共用）；其它模块一律复用它，不得就地重造这条正则 |
| `src/xvt/source_selection.py` | 源字幕候选的确定性 QA 与选择决策（`SourceStrategy` / `SourceCandidateKind` / `SourceSelectionAction`）。`plan_source_selection` 的 `platform_subtitles_requested` **只应在远程 Job 上传 `False`**——本地文件输入的 Job 永远没请求过平台字幕，照此传会把它误记成 `platform_subtitles_not_requested`，等于用一个谎话换另一个；调用方以「有没有 `remote_info` artifact」为准（`transcription.shared.platform_manual_subtitle_languages` 返回 `None` 即本地 Job） |
| `src/xvt/render.py` | 双语 ASS 样式和平台字体选择 |
| `src/xvt/burn.py` | FFmpeg 烧录、编码选择和进度解析 |
| `src/xvt/paths.py` | **用户可指定路径的写盘出口**共用的绝对路径解析与覆盖保护（源媒体、job 文件、Job 记着的用户原始输入、已登记 artifact）。烧录 `--output`、预览 `--output` 与 `build-translation-context --export-glossary` 都走这一道；**新增这类出口必须接入**，否则一条手滑的路径就能盖掉已登记 artifact 并让 Job 的 checksum 失效（`--glossary` 一类只读输入不在其内） |
| `src/xvt/events.py` | JSONL 事件和通用 URL 脱敏 |
| `src/xvt/process.py` | 无 shell 外部进程、流式输出和命令参数脱敏 |
| `src/xvt/errors.py` | `ExitCode` 枚举与 XVT 异常族（退出码语义的单一事实来源） |
| `src/xvt/locking.py` | Job 目录与 batch 文件的 `flock` 独占执行锁（第二个进程立即以退出码 7 失败，锁序固定先 batch 后 job） |
| `src/xvt/cancellation.py` | 进程级协作式取消：把 SIGINT/SIGTERM 统一为全局取消事件，供 `process.run_command` 与长循环的迭代检查点使用 |
| `src/xvt/doctor.py` | `xvt doctor` 的本机运行时依赖探测（ffmpeg/ffprobe/yt-dlp、ffmpeg 滤镜与编码能力、ASR 后端、字幕字体、输出目录可写） |
| `tests/` | 与源码模块对应的单元和流程测试；另有三类跨文件守卫：`test_pipeline_imports.py`（包内依赖方向）、`test_cli_command_tables.py`（CLI 命令表硬关系）、`test_subprocess_boundary.py`（不可破坏约束 6） |

`workflow.py` 只负责编排，不应复制 `pipeline` 包的媒体或字幕算法；跨模块复用的判定一律把 pipeline 侧的私有名公有化并经 `pipeline/__init__` re-export，由 `tests/test_workflow.py::WorkflowReusesPipelineHelpersTest` 以对象身份钉住（当前为 `source_selection_matches` / `artifact_checksum_for` / `error_category`）。原子命令仍需可单独使用，端到端命令通过组合这些能力完成任务。`pipeline` 包内依赖方向保持无环：`common ← review ← {transcription.shared, translation, burnin}`，`common ← semantic_state ← {review, revision}`，`retranslate` 依赖 `translation`，`semantic` 依赖 `translation` 与 `{review, revision, semantic_state}`，`resegment` 与 `context` 只依赖 `common`，`burnin` 依赖 `common` 与 `review`（烧录/预览与 workflow 共用同一 QA 放行谓词），`polish` 依赖 `translation`，`revision` 依赖 `common` 与 `semantic_state`；`transcription` 子包内部同样无环：`transcription.shared ← transcription.local ← transcription.remote ← transcription.resume`，`transcription.dispatch ← {transcription.local, transcription.remote}`；新增子模块时不得引入反向依赖，公共入口一律经 `__init__` re-export。**包内已无函数级 import 环**：所有包内依赖都是模块顶层 import，`tests/test_pipeline_imports.py` 同时守卫顶层依赖边白名单与「函数体内不得出现 `from xvt.pipeline.*`」（它按 `rglob` 递归扫描包目录，子包成员在白名单里写成 `transcription.local` 这类点号名，新增子包不会被静默漏扫；**包根不是合法依赖目标**——包内模块不得写 `from xvt.pipeline import X`、`import xvt.pipeline` 或相对 import，包根 `__init__` import 了全部子模块，反向依赖它就是加载期循环的来源，这三类写法一律被报成 `<package root>` 而白名单里没有这一项），需要共享只读状态时把它下移到叶子模块，而不是用惰性 import 规避循环。

## CLI 语义

默认用户路径是 **`run` → `status` → `continue`**（批处理为 `batch` / `batch-continue`）。`status` 输出 `recovery_plan` 与 `advance_steps`，与 `continue` 共用 `plan_advance_steps` 判定，是中断后的导航入口。

- `xvt run` 创建新 Job。没有 `--target-language` 时停在源字幕；提供目标语言后继续翻译；同时提供 `--burn` 时继续烧录。停在 `awaiting_input` 时退出码默认为 0（Agent 协议的正常步骤），只有显式 `--fail-on-waiting` 才返回 10；该旗标不发 `cli_failed`、不改事件流，只改退出码。
- `xvt batch` 固定本地目录输入清单并为每项创建独立 Job；`xvt batch-continue` 只按 `batch.json` 和各 Job 的恢复计划处理未完成项。等待项绝不由批处理自动推进：已登记烧录成品缺失**且具备重新关联资格**（checksum 有效的 v2 proof 等条件全部匹配）时绝不自动重烧，一律停在 `awaiting_input` 等用户显式 `relink-burned-output` 或 `continue`；不具备资格的（例如只有 legacy fingerprint 的旧 Job）按 `retryable` 分类，由批处理重烧。`--fail-on-waiting` 下有等待条目返回 10，有失败条目仍返回 6（**6 优先于 10**）。恢复分类、`degraded` 标注与各目标产物的去向详见 [恢复与回填契约](docs/RECOVERY.md#批处理恢复分类与中断兜底)。
- `xvt status JOB` 展示 stage、`advance_steps` 与下一步命令（优先看这里，不要猜 resume/continue）。产物有效性判定与 `continue` 使用**同一套全量 checksum 校验**，只是同一次判定内对同一文件只哈希一次。它对缺失烧录成品只做静态资格判断，不额外计算源媒体内容 checksum。记忆化边界与元数据证伪/证实的界线详见 [恢复与回填契约](docs/RECOVERY.md#产物有效性校验与-checksum-记忆化)。
- `xvt continue JOB` 读取 `job.config.workflow`（类型化为 `WorkflowConfig`），补齐未完成的翻译和烧录。它不用于没有持久化工作流目标的旧 Job。等待态退出码同 `run`（默认 0，`--fail-on-waiting` 时 10）。
- `xvt resume JOB` 只恢复转写 checkpoint，例如规范化音频、词级转写或源 cue（advanced）。
- `xvt rebuild-source JOB --strategy asr` 只在用户明确要求时以 ASR 重建媒体 Job 的源时间轴（advanced）。它优先复用 checksum 有效的 words/audio checkpoint，失效旧 source cue 及全部下游产物，并保留 Job 内有效的输入译文副本——**保留不等于还能用**：这份副本能否套回新的源时间轴，由 `status` / `continue` / 本命令收尾共用的只读谓词 `pipeline.common.pending_translation_import_blocker` 当场判定；套不回去时本命令收尾就给出 `import-translation` 出口，`status` 归 `manual_fix`，`continue` 带同一句诊断失败，三侧都不再推荐那条必然失败的 `continue`（判定条件与两条出口的取舍见 [恢复与回填契约](docs/RECOVERY.md#已存译文套不回重建后的源时间轴)）；纯字幕 Job 不支持。
- `xvt drop-imported-translation JOB` 是**显式放弃导入译文**的唯一入口（advanced）：同时清掉 `input_translation_subtitles` 的 artifact 登记与 `config.workflow.translation_subtitles`（缺一处就会被 `_ensure_translation` 的 `retained_translation or config.translation_subtitles` 捡回来），按依赖图失效 `downstream_of(INPUT_TRANSLATION_SUBTITLES)` 的全部下游并复位相关 stage（不带闭包的话译文仍 checksum 有效，`continue` 会直接判已完成，产物还是来自被放弃的译文），保留两份文件不删字节，并以 `artifact_invalidated` + `translation_import_dropped` 留痕。两处来源都没有时以退出码 2 拒绝。它取代的不是能力而是**旁路**：原子 `translate` 也能绕过去，但两处来源原样留着，下一次 `rebuild-source` 原地复发同一个死局。原子 Job（无 `config.workflow`）同样支持，放弃之后源文修订与重分段的「正在复用导入译文」闸门随之解除（详见 [恢复与回填契约](docs/RECOVERY.md#已存译文套不回重建后的源时间轴)）。
- `xvt build-translation-context JOB [--export-glossary PATH]` 从源 cue 确定性抽取术语候选包（advanced，零模型调用）。产出 `translation_context` artifact，可选导出可审核的 glossary 骨架。**只产出候选，不产出事实**：不进翻译 prompt、不进任何指纹、不改 stage，候选只有经用户审核后由 `--glossary` 传回才生效。只对「句中普通名词小写」的拉丁语言有效，CJK / 德语 / 源语言未知一律降级（产物记 `extractor_degraded`，退出码仍为 0）。它进 `downstream_of(SOURCE_CUES)`，所以重分段、源文修订与 `rebuild-source` 会一并失效它。
- `xvt prepare-source-review JOB --cue ID` / `apply-source-review-fixes JOB REVIEW` 显式修订源 cue 文本，保留 ID、时间轴、words 和 provenance，立即重跑源字幕 QA，并失效旧翻译及全部下游；导入译文工作流不支持。
- `xvt translate JOB` 是独立翻译入口（advanced）。`--provider auto` 在 Base URL、模型和 API Key 完整时调用兼容 API，否则进入 Agent 模式。
- Agent 模式生成 `translation-batches.json` 并进入 `awaiting_input`。必须先回填再运行 `continue`。
- `xvt apply JOB RESPONSE` 是统一回填入口：人工修订模板按 `task` 字段路由，人工修订 SRT 按固定文件名识别，Agent 响应按 Job 当前等待步骤路由。**路由不出来时报错而不猜测**，改用对应的显式 `apply-*` 命令（它们保留为稳定入口，语义零改动）。**新增等待步骤必须先登记进 `workflow.AWAIT_STEPS`，再在 `cli._STEP_ROUTES` 与 `cli._UNROUTABLE_AWAIT_STEPS` 两张表之一里表态**，守卫测试强制：`tests/test_workflow.py::AwaitStepsCoverageTest` 用 AST 比对 `plan_advance_steps` 实际产出的 `await_*` 与 `AWAIT_STEPS`，双向都不许有差集（漏登记会让 `recovery` 的让路判定与 `xvt apply` 的路由一起静默失效）。`--continue` 透传、源文修订拒绝该组合的理由与各场景推荐命令详见 [恢复与回填契约](docs/RECOVERY.md#xvt-apply-统一回填路由)。
- `--translation-quality polished` 在润色候选后必须完成 Q6 语义校对；API 最多修复一次并复查，Agent 使用 `apply-semantic-review` 回填合并响应。仍有 review 问题时运行 `continue --acknowledge-translation-review`；确认不能绕过确定性字幕 QA。
- `xvt polish-translation JOB` 为已有端到端 Job 显式补跑润色；API 模式逐批 checkpoint，Agent 模式必须先运行 `apply-polish-translation`，再由 `continue` 进入语义校对。
- `xvt verify JOB` 重新计算 artifact checksum。恢复逻辑只能复用校验通过的文件。
- `xvt burn JOB` 是独立烧录入口（advanced）。本地纯音频 Job 必须通过 `--media` 提供视频。该原子命令可覆盖持久化 workflow 之外的媒体、字体和编码参数，因此不签发 workflow v2 proof；只有 `run` / `continue` 按持久化目标完成的烧录才登记 checksum 保护的 `version=2` proof，详见 [恢复与回填契约](docs/RECOVERY.md#烧录-proof-与成品重新关联资格)。
- `xvt relink-source-media JOB NEW_MEDIA_PATH` 是**源媒体**永久搬家（路径变了、内容没变）后重指持久化路径的唯一入口（advanced）。只写 `config.workflow.burn_media` 一个字段：不重烧、不碰 artifact 登记、不改 stage，因此不发 artifact 事件。fail closed，只认内容 checksum——新文件内容 checksum 必须等于 checksum 有效的 v2 proof 登记的 `source_media_checksum`，没有 v2 proof 一律拒绝（与 `relink-burned-output` 同口径）。远程 Job 不支持。写入后烧录派生指纹的内容 checksum 回退即成立，Job 零重烧回到 `complete`。恢复导航已接入它：源媒体不在登记路径且 Job 够格（`workflow.source_media_relink_available`，与命令本体共用一份判定）时 `status` 的 `primary_command` 就是这条命令，不够格（例如只有 legacy fingerprint）时维持 `None`——它没有 v2 proof 必然失败，无条件推荐就是又一次「推荐一条必然失败的命令」。详见 [恢复与回填契约](docs/RECOVERY.md#源媒体搬家后的路径重指)。
- `xvt relink-burned-output JOB EXISTING_VIDEO` 是已移动烧录成品的唯一重新关联入口（advanced）。**只认 checksum 有效的 v2 proof**，全部校验项匹配才放行，重复 `burned_video` 一律 fail closed；不具备资格的 Job 按 `retryable` 由 `continue` 重烧。完整匹配条件详见 [恢复与回填契约](docs/RECOVERY.md#烧录-proof-与成品重新关联资格)。

不要把 `resume` 和 `continue` 合并。前者恢复转写状态，后者完成用户最初请求的端到端目标。

## 修改流程

1. 开始编码前，用简体中文说明方案、涉及文件和验证方式（出方案推荐用 `/think` skill），**并对方案本身做一遍 review 再交付**：最脆弱的假设是什么、有没有漏掉的协议面与快照影响、改动面是否被低估、验证方式能不能真的证伪。发现问题当场调整并重新过一遍，确认方案没问题才等用户批准、才开始实现——**与第 10 条的代码 review 同一口径**。方案阶段漏判的代价会在实现阶段放大：2026-11-01 的 E1 方案漏了「往 artifact 加字段会改内容 checksum」这一层，直到实现时协议快照 7 条红才发现，返工了一轮设计。
2. 单组修改超过 10 个文件时，拆成可独立验证的小任务。
3. **分支与 PR：默认单分支串行**（实现 → review → 提交 → 开 PR → 合 `main` → 再开下一项）。不要多条互不依赖的功能分支同时对着 `main` 开 PR。确需拆分时，后继分支必须基于**前置功能分支**切开（叠放），不要各自从 `main` 并行发散。
4. 查找代码符号时优先使用 LSP。当前环境没有 LSP 工具时，可以降级使用 `rg`，并在进度说明中注明。
5. 使用 `apply_patch` 修改文本文件。不要覆盖用户已有的无关改动。
6. 保持实现简单。不要为尚未确认的 GUI、队列、多租户、数据库或插件体系预留抽象层。
7. 行为变化必须补测试。涉及 FFmpeg、字体、yt-dlp、翻译提供方、恢复逻辑或安全边界时，至少覆盖一个失败或边界场景。
8. **协议快照不得被重构改写**：`tests/test_provider_protocol_snapshot.py` 冻结 provider 编排的事件序列、artifact 内容 checksum、manifest 键序与 fingerprint，golden 在 `tests/fixtures/provider-protocol-snapshot.json`。重构期间它出现 diff，一律回退改动而不是更新期望值；只有刻意的协议变更才允许重新生成 golden，且必须同步 Changelog 与相关文档。
9. CLI 参数、默认行为、artifact、事件或安全边界变化后，同步检查 README、架构、恢复契约、Changelog、Roadmap、Skill 和 HANDOFF。**同步 HANDOFF 不等于往里堆**：它 ≤ 200 行且 ≤ 32KB，单条日期条目 ≤ 2KB，超出就把最旧的条目滚进 [HANDOFF 历史档案](docs/HANDOFF_ARCHIVE.md)（按日期倒序，插在对应日期分组最前面）。实证过程、否决记录、复现数据属于 `docs/REVIEW_FINDINGS_*.md` 或对应专项方案，HANDOFF 条目只留结论、影响和链接。这条由 `tests/test_handoff_budget.py` 强制，不是自觉——2026-10 期间它曾在 4 天内从 61KB 涨到 240KB。
10. **提交前硬门槛（不可跳过）**：
    1. **用 `/check` skill 做 review**，不是随手对照 diff 看一遍——它有固定检查面（范围漂移、同类站点普查、过度设计、硬停项、回归测试在旧代码上是否真会红），能逼出自查漏掉的东西。review 范围与重点要在调用时写清（正确性、边界、与既有语义一致性、安全边界、文档是否漏改）。
    2. 高/中优先级问题必须修复，修完**重新跑 `/check`**，循环到没有阻断性问题；未修干净不得 `git commit`。同一任务最多三轮，第三轮仍冒出新问题就回头质疑方案本身而不是继续打补丁。
    3. 跑通约定验证命令（至少 ruff + 相关/全量 pytest）；通过后才提交。
    4. 向用户简要说明 review 结论（无问题 / 修了什么）后再推送或开 PR。

## 验证命令

```bash
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pytest -q
uv run --no-sync xvt --help
```

集成测试（`tests/test_e2e.py`、`tests/test_burn.py`）要求 ffmpeg **带 libass 的 `ass` 滤镜**——烧录与预览靠 `-vf ass=filename=...` 送字幕。homebrew 把 ffmpeg 拆成了两个 formula，精简的 `ffmpeg` 不带 `--enable-libass`，**本地与 CI 都要装 `ffmpeg-full`**；缺 libass 时这些测试会跳过并说明原因（`pytest -rs` 可见），不会伪装成通过。

**本地与 CI 还都需要系统能解析到字幕字体**，否则行宽 QA 会**静默降级到保守规则**：`render.font_for_language` 在非 darwin / 非 win32 上给 zh / ja / ko 要 `Noto Sans CJK SC` / `JP` / `KR`、给其余语言要 `DejaVu Sans`（darwin 上要 Hiragino / Helvetica，系统自带），缺字体时 libass 报 `failed to find any fallback with glyph` → `measure_bilingual_line_widths` 抛 `ValueError` → `qa.check_subtitles` 回退保守显示单位上限并只记一条 `layout_measurement_unavailable` warning。**这不会让任何测试变红**（涉及行宽的用例全部打桩了测量函数），所以它靠一步独立的能力断言兜住：

```bash
uv run --no-sync python .github/scripts/verify_layout_measurement.py
```

它对 zh / ja / en 各真的调一次 `measure_bilingual_line_widths` 并断言报告自己的 `layout["measurement"] == "measured"`，**不是** `fc-list | grep cjk` 这类代理指标（「字体包在盘上」≠「fontconfig 解析得到那个家族」≠「libass 量得出包围盒」）。CI 两个平台都跑这一步，Linux 侧装 `fonts-noto-cjk`；该步**必须排在 `uv sync` 之后**（脚本 import xvt），不能像 ffmpeg 那两步一样提到最前。

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
- 失败路径不会误报 `cli_completed` 或 `workflow_completed`。`_advance_workflow` 在发 `workflow_completed` **之前**断言 `plan_advance_steps(job, config)` 为空，不为空按内部不一致抛 `InternalError`（fail closed，不得降级成警告）。
- 「完成」必须对应一个真的完成了的 Job。断言 `status == "completed"` 的测试，其打桩只允许替掉真实的网络、模型与 FFmpeg 调用，**必须照真实运行落盘产物、登记 artifact 并推进 stage**；返回假路径、不碰 Job 的替身会让这条断言永远成立而什么都没验到（2026-10-17 的 F6 / O5 就是这么漏了两道媒体 QA 判定）。
- 测试、Ruff lint 和 Ruff formatter 全部通过。
- 对应文档已经同步，HANDOFF 中的进展和下一步仍然准确。
