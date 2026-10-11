# 架构改进与工程加固技术方案（R 系列）

本方案来自 2026-08-02 的一次项目全量审查（基线 `ab28aff`，482 测试 + 18 子测试全部通过，Ruff lint/format 全绿）。它把审查发现与设计改进整理为可独立交付的分阶段任务，按风险从低到高分为 R1（工程加固）、R2（CLI 与交互收敛）、R3（架构演进）三组。各项之间除显式标注外没有依赖，可以穿插进正常功能迭代。

审查确认的核心结论：项目的关键取舍（时间轴单一来源、翻译只碰文本、checksum 恢复、本地优先、显式凭据）全部保留，本方案不触碰任何一条不可破坏约束。要解决的是**复杂度增长模式**问题：当前每个质量特性 × 每种 provider 模式 × 各自的恢复语义三个维度相乘，本方案的目标是把它变成加法。

## 目标

1. 堵住发布工程盲区（打包验证、多 Python 版本、发布 tag）。
2. 把 pipeline 依赖规约从文档纪律变成可机械核查的守卫测试。
3. 收敛 CLI 回填命令面和巨型分发函数，降低后续新增命令（尤其是 GUI 配套命令）的边际成本。
4. 用统一的 provider 传输抽象和统一的 checkpoint 机制，消除"每个质量特性手写一遍缓存系统"的重复。
5. 全程不破坏现有 Job 兼容性；每一项都可独立验证、独立合并。

## 非目标

- 不改变时间轴、cue ID、翻译职责边界等不可破坏约束。
- 不新增 GUI、队列、并发批处理或远程执行能力。
- 不在本方案内切换默认翻译质量（Agent 主链真实 E2E 后仍保持 standard + faithful，多样本对照后再评估）。
- R3 各项在真实 polished E2E 验收完成前不启动实施，避免与质量闭环验收互相干扰。

## 审查发现汇总（证据基线 `ab28aff`）

| 编号 | 发现 | 证据 |
| --- | --- | --- |
| F1 | CI 只测 Python 3.11，无 `uv build` 打包验证 | `.github/workflows/ci.yml`；pyproject 声明 3.11+ |
| F2 | CHANGELOG 有 0.1.0/0.1.1 版本节和 dist 产物，但仓库无任何 git tag | `git tag` 为空 |
| F3 | AGENTS.md 的 pipeline 依赖规约缺 `polish → translation` 与 `revision → common` 两条边 | `AGENTS.md:51`；`pipeline/polish.py:22` 实际 import `translation` |
| F4 | 跨模块引用 `_` 前缀私有符号：`_translate_batch_with_retries`、`_complete_qa_stage`、`_set_stage`、`_artifact_checksum_for` 等实际是包内共享 API | `pipeline/` 内 8 个文件涉及 |
| F5 | `cli.py` 的 `_run_command` 约 650 行（1012–1665），承载全部子命令执行逻辑 | `src/xvt/cli.py:1012` |
| F6 | 6 个 `apply-*` + 2 个 `prepare-*` 命令，而 Agent manifest 已含 `task` 字段和 fingerprint，路由信息冗余地要求用户提供 | `pipeline/translation.py:695`（`task=translate_and_polish`）等 |
| F7 | 每个 LLM 子步骤各自实现 checkpoint/fingerprint/恢复判定，`semantic.py` 1276 行中管道工程占大头 | `pipeline/semantic.py` 的 `_load/_write_*_checkpoint`、`_repair_chain_satisfied` 等 |
| F8 | API 与 Agent 双路径在 polish/semantic/retranslate 各实现两遍编排 | Q5/Q6 各方案文档与实现 |
| F9 | `_MIGRATIONS` 迁移机制已建成但为空、从未使用；stage 枚举冻结导致 `translate` 内部六个真实阶段只能靠 artifact 存在性反推 | `models.py:93` |
| F10 | `job.config` 双轨：workflow 子配置已类型化，`source_strategy`、旧 `target_language` 仍是顶层裸键 | `pipeline/transcription.py:313` 直接读 `job.config["source_strategy"]` |
| F11 | 测试用 `unittest.mock.patch` 打模块级函数补丁，patch 目标绑定模块内部结构，纯搬移重构时曾多次失效 | `tests/test_pipeline_translation.py` 等；`translation_provider` 的 `opener` 参数已是 DI 先例 |
| F12 | 人工修订要求用户手编 review JSON 的 `items[].text`，而 SRT 才是字幕行业通用可编辑格式 | Q6.1a/b 流程 |
| F13 | `transcription.py` 等存在 150–250 行长函数（`_process_adaptive_media` 299–551 等） | `pipeline/transcription.py:299` |
| F14 | 真实 Job 复查：用户把导出到 Job 外的烧录成品移走后，`status` 误报"烧录尚未通过媒体 QA"（该 Job `media_qa=succeeded`），并推荐会触发完整重新编码的 `continue`；`verify` 的诊断（`artifact file is missing`）才是准确的 | `recovery.py:627` 将成品缺失与媒体 QA 未过合并为同一文案；2026-07-16/18 两个真实 Job 复现 |
| F15 | 字幕 QA `needs_review` 经 `--acknowledge-subtitle-review` 确认并完成烧录后，`translate` stage 永久保留 `failed` 和过期错误文本（"run --acknowledge-subtitle-review"，实际已确认）；在含 PR #66 修复的 `ab28aff` 上对完整 Job 副本重跑 `continue` 仍不收口。PR #66 只覆盖语义复核确认路径，字幕 QA 确认路径是同类问题的未扫兄弟实例 | 2026-08-01 polished 真实 Job（`20260801T104038Z-5376f3bb`）复现；`recovery` 判定 complete 而 stage 行显示 failed，两轨矛盾与 F14 同族 |

## 实施进度（2026-08-10 更新）

- **已完成**：R1 全部四项（PR #68，堆叠于 #67）；R2.4 文案拆分与 R2.5 收口已在真实 Job 上实证（PR #67），R2.4 两阶段随后以 v2 proof Job、CLI 和批处理集成测试完成。R1.3 落地时发现 semantic ↔ review/revision 之间存在函数级惰性 import 打破加载期循环，守卫测试只约束模块顶层依赖方向，该循环留待 R3.1 传输层重构一并消解。R2.1 `_run_command` 拆分已完成：26 个命令分支提取为独立 `_cmd_<name>` 函数 + `_COMMAND_DISPATCH` 分发表，`_run_command` 缩减至 4 行查表逻辑。R2.2 统一 `apply` 路由已完成：人工修订模板按 task 字段路由、Agent 响应（无 task 字段）按 Job 等待步骤路由的两级路由，仅支持有 `config.workflow` 目标的端到端 Job，原 `apply-*` 命令全部保留。
  R2.3 译文修订 SRT 通道已完成：`review/translation-review-<fp12>.srt` 与同名 JSON 同时生成（旧 Job 首次 `ensure_translation_review_input` 时按同一 fingerprint 补齐），`apply-translation-review-fixes` 按后缀分派到 SRT / JSON 解析器，两者汇聚成同一个 `changes` 映射后共用 fingerprint 绑定、只改 `target_text`、review-only 复查。SRT 无 fingerprint 字段，改由文件名承载校验；cue ID 按 `(start_ms, end_ms)` 精确匹配还原，因此改时间轴、增删 cue 都在解析边界失败，无需额外校验。统一入口 `xvt apply` 按固定文件名识别修订 SRT，不再把它误引导到 `import-translation`。补成功路径测试时发现 `_srt_review_changes` 的完整性断言把 cue ID 集合与时间轴键集合相比，SRT 通道的成功路径原本永远抛"removed a cue"——修正为与 `by_timing.values()` 比较。

- **已完成**：2026-10-11 polished Agent 主链真实 E2E（平台人工字幕到整段烧录），默认保持 standard + faithful；见 [验收记录](POLISHED_AGENT_E2E_ACCEPTANCE.md)。Q6.2 语义收敛验证之后再启动 R3.1。R2.3 源文修订 SRT 通道已完成：与译文通道共用文件名 fingerprint、时间轴精确匹配、注释 ID 校验和统一 `xvt apply` 路由契约，JSON/SRT 回填汇聚到同一 `changes` 语义。

## R1：工程加固（低风险，可立即实施）

### R1.1 CI 补打包验证与 Python 版本矩阵（F1）——已完成（PR #68）

- `.github/workflows/ci.yml` 的矩阵增加 `python-version: ["3.11", "3.13"]`（与 os 矩阵正交）。
- 新增 `uv build` step（放在测试之后），确保 wheel/sdist 始终可构建。
- 验收：CI 四个组合全绿；本地 `uv build` 与 CI 产物一致。

### R1.2 发布 tag 与发版流程（F2）——已完成（v0.1.0 / v0.1.1 已推送，AGENTS 已记录流程）

- 按 CHANGELOG 日期定位 0.1.0（2026-07-15）与 0.1.1（2026-07-24）对应 commit，补打 annotated tag `v0.1.0`、`v0.1.1` 并推送。
- AGENTS.md 验证命令一节追加发版约定：升版本号 → 更新 CHANGELOG 版本节 → `uv build` → `git tag vX.Y.Z`。
- 验收：`git tag` 列出两个历史版本；AGENTS.md 记录流程。

### R1.3 依赖规约补齐与 import 方向守卫测试（F3）——已完成（PR #68）

- AGENTS.md:51 的规约补上 `polish → translation`、`revision → common` 两条边。
- 新增 `tests/test_pipeline_imports.py`：解析 `pipeline/` 各模块的 `from xvt.pipeline.X import` 语句，断言实际依赖边集合与声明的白名单完全一致（多一条或少一条都失败）。守卫测试把"无环 + 方向正确"从文档纪律变成失败的构建。
- 验收：守卫测试在当前代码上通过；人为加一条反向 import 时测试失败。

### R1.4 包内共享符号公有化（F4）——已完成（PR #68，16 个符号，无别名）

- 把确实被跨模块引用的 `_` 前缀符号去掉下划线（`set_stage`、`complete_qa_stage`、`translate_batch_with_retries`、`artifact_checksum_for`、`write_json`、`load_cues`、`checksum` 等）。包内符号无外部消费者，一次性完成定义处、所有 import 处与调用处改名，不保留旧名别名。
- 纯模块内私有的保持现状。这是纯重命名，注意测试中 `mock.patch` 字符串目标需同步（正是 F11 描述的脆弱点，本项顺带减少一批此类目标）。
- 验收：`grep -rn 'import _' src/xvt/pipeline/` 无跨模块私有引用；全量测试通过。

## R2：CLI 与交互收敛（中风险，建议在 GUI 之前完成）

### R2.1 `_run_command` 分发拆解（F5）——已完成

- 把 `cli.py` `_run_command` 内每个 `if arguments.command == ...` 分支抽成独立的 `_cmd_<name>(arguments) -> int` 函数，顶层换成 `dict[str, Callable]` 分发表。
- 纯搬移，不改任何行为和输出。遵循既有"纯搬移四步陷阱"经验：先确认 `tests/test_cli.py` 中 patch 目标是否随符号迁移、多行文本块完整搬运、守卫断言范围不缩水。
- 最终以单 PR 一步到位完成：26 个 `_cmd_<name>` 函数 + `_COMMAND_DISPATCH` 分发表，`_run_command` 从 622 行缩减至 4 行。485 测试全部通过。
- 验收：`_run_command` 只剩查表与公共前后置逻辑；`xvt --help` 与各命令行为逐字节不变。

### R2.2 统一 `xvt apply JOB RESPONSE` 自动路由（F6）——已完成

- 新增 `apply` 子命令：优先读取 RESPONSE 文件中的 `task` 字段路由（`source_review_fixes`、`translation_review_fixes`、`semantic_review_only`、`semantic_review_and_repair`、`translate_and_polish`）；缺 `task` 时回退到按 Job 的 `plan_advance_steps` 首步路由（`await_agent_translation`、`await_agent_polish`、`await_agent_semantic_review`、`await_translation_review`）。两条路都不成立时报结构化错误，不猜测。
- `apply-translation` 需要的 `--target-language` 由 `resolve_target_language(job)` 从 Job 中解析，用户无需重复提供。
- 现有 `apply-*` 命令全部保留为稳定入口，行为不变；`status` 的 `before:` 改为推荐 `xvt apply`，原命令降为 `alternative:`。
- 各回填实现的校验逻辑零改动——本项只加一层查表路由，不碰协议语义。response 内容非法但 JSON 可解析时，stage / artifact 事件序列与原命令一致；response 文件不存在或不可解析时，`apply` 在触碰 Job 之前即失败，只发出 `cli_failed`，stage 保持 `awaiting`（不像原命令会把 stage 打成 `failed`）——这是刻意保留的更优行为，不是与原命令的等价性缺口。终态事件的 `command` 字段按 EVENTS.md 契约报用户实际执行的 `apply`，成功（`cli_completed`）与失败（`cli_failed`）两条路径报同一个名字。
- manifest 侧的三个 task key（`semantic_review_only`、`semantic_review_and_repair`、`translate_and_polish`）是为前向兼容保留的路由分支：Agent 回填响应的协议契约（`translation-protocol.md`）本身不含 `task` 字段，真实 Agent 响应不会命中这三个 key，而是统一走 Job 等待步骤回退路由；只有人工修订模板（`source_review_fixes`、`translation_review_fixes`）会真正按 `task` 字段路由。
- 不纳入统一路由：`apply-cue-retranslation`（需额外 `--cue` 参数）、`import-translation`（输入是字幕文件而非 JSON response）。原子 Job（无持久化 `config.workflow` 目标）同样不受统一路由支持，只能用显式 `apply-*` 命令。
- 验收：12 个新增 CLI 测试覆盖四类 task 路由、步骤回退路由、终态事件命令名一致性、不可读文件的用户输入错误归类、真实 Job 零 mock 端到端路由、路由表与实现常量同源的守卫，以及未知 task、原子 Job 无工作流目标、非等待态三类错误；497 测试全部通过。
- 已决策：`await_translation_review` 与源文修订等待态的 `status` 提示保留显式命令（不换成统一入口），因为这两条提示嵌入了真实模板文件路径，比通用 RESPONSE 占位符更有引导价值；`xvt apply` 对这两类模板文件同样可用（携带 `task` 字段，按 task 路由），只是不作为 `status` 的首选提示。

### R2.3 人工修订 SRT 编辑通道（F12）——译文与源文通道均已完成

- `await_translation_review` 时在 `review/` 目录同时生成一份带 cue ID 注释的 review SRT（如 `translation-review-<fingerprint>.srt`）；用户用任意字幕工具编辑后，`apply-translation-review-fixes` 接受 SRT 输入：解析、按 cue ID diff 出改动项、转换为现有修订记录格式，其后校验与回填流程完全复用现有实现（fingerprint 绑定、只改 `target_text`、review-only 复查）。
- JSON 模板通道原样保留给 Agent 与脚本消费；SRT 是人工通道的加法。
- SRT 不带 `revision_fingerprint` 字段，改由文件名承载（`translation-review-<fp12>.srt`），等价于 JSON 通道的 fingerprint 校验；回填时 cue ID 按 `(start_ms, end_ms)` 精确匹配还原，`#<id>` 注释行若存在必须与匹配结果一致。时间轴是不可破坏的单一来源，因此改时间轴、增删 cue 都在解析边界自然失败，无需另加校验。
- 两条通道各自解析后汇聚成同一个 `changes: dict[int, str]`，fingerprint 绑定、只改 `target_text`、review-only 复查完全共用，这是相同 checksum 的成因。
- 旧 Job 只有 JSON 模板时，`ensure_translation_review_input` 按同一 fingerprint 补生成 SRT；`recovery.py` 的 `input_paths` 暴露 SRT 路径，使其可经 `status` 发现。
- 统一入口 `xvt apply` 按生成时的固定文件名（`translation-review-<fp12>.srt`）确定性识别修订 SRT 并路由到修订通道；其他 SRT 仍按原逻辑归为用户输入错误并引导 `import-translation`，两者不混淆。
- 该命名是跨模块契约（模板写入点、校验回填点、CLI 形状正则、测试夹具四处依赖同一个值），故收敛到 `revision.py` 的 `TRANSLATION_REVIEW_STEM_PREFIX` / `REVISION_FINGERPRINT_LENGTH` / `translation_review_stem()` 单一来源，各处一律派生，并加同源守卫测试防改名后静默失配。
- 源文修订（`prepare-source-review`）已按相同契约增加 SRT 通道：JSON/SRT 同 fingerprint，按 source cue 时间轴还原 ID，只允许改 `source_text`，统一入口按 `source-review-<fp12>.srt` 路由。
- 验收：SRT 通道与 JSON 通道对同一组编辑产生相同的修订记录 checksum；改动时间轴或增删 cue 的 SRT 在解析边界被拒绝。译文通道两项均已通过（含 6 个拒绝场景 subtest：时间轴平移、删 cue、加 cue、注释与时间轴不符、空译文、过期 fingerprint）；501 测试全部通过。

### R2.4 烧录成品缺失的恢复语义与文案修正（F14）——两阶段已完成

- `_burn_recovery_reason`（`recovery.py`）拆分三种情形并分别给出准确文案：媒体 QA 未通过（现文案保留）、烧录派生指纹失效（现文案保留）、**成品文件缺失但媒体 QA 与指纹均有效**（说明成品已不在登记路径；第二阶段再按 proof 等静态资格决定提示重新关联还是 `continue` 重烧）。
- 第一阶段提供显式 `relink-burned-output JOB EXISTING_VIDEO`：仅在唯一旧成品记录、媒体 QA pass、当前译文有效且 checksum 有效的 v2 burn proof 同时成立时，校验当前 fingerprint、原成品 checksum、实际源媒体内容 checksum 和候选文件 checksum，再原子更新 artifact 路径；legacy fingerprint、重复成品记录或任一校验失败均拒绝且不改 Job。
- 第二阶段已把重新关联接入 `status` 恢复导航：普通 `status` 仅作静态资格判断，不为提示额外 hash 源媒体内容；符合资格的缺失成品归类 `awaiting_input`，优先给出 `relink-burned-output`，并保留显式 `continue` 重烧作为备选。最终 `relink` 命令仍重新计算并校验源媒体内容 checksum。
- `batch-continue` 对上述 `awaiting_input` 项保持阻断，不自动触发整段重烧；legacy proof/fingerprint、媒体 QA、proof 或 fingerprint 无效的缺失成品均不提示 relink，而是归类 `retryable` 并沿正常 `continue` 重烧恢复。
- 验收：第一阶段已覆盖成功后的 `verify` / `status`、checksum 不匹配、媒体 QA/proof/源媒体绑定无效、legacy proof、重复成品记录和失败原子性；第二阶段覆盖 status 的优先 relink/显式重烧导航、静态导航不 hash 源媒体，以及 batch-continue 对可 relink 项停住而对无资格项按 retryable 重烧。

### R2.5 字幕 QA 确认后的 translate stage 收口（F15）——已完成（PR #67，真实 Job 实证收口）

- 与 PR #66 的语义复核收口对称：字幕 QA `needs_review` 经确认且译文候选与渲染产物有效（或补齐成功）后，`translate` stage 同样显式收口为 `succeeded`，清除过期的 acknowledge 提示错误。旧 Job 在下一次 `continue` 时收口。
- 回归测试必须先在未修复代码上失败：构造"polished 链全绿 → 字幕 QA needs_review → 确认 → 烧录完成"的 Job，断言 `translate` 终态为 `succeeded` 且无残留错误文本。
- 顺带按 sibling 排查：搜索所有把 stage 置为 `failed` 并期待后续人工确认解除的路径，确认每条都有对应收口逻辑（语义复核已由 PR #66 覆盖，字幕 QA 为本项，若有第三处一并列出）。
- 验收：2026-08-01 真实 Job 副本重跑 `continue` 后 stage 行不再显示 `failed`；`recovery: complete` 与 stage 行不再矛盾。

## R3：架构演进（较高投入，polished Agent E2E 与默认质量决策后启动）

R3 各项相互关联，建议按 R3.1 → R3.2 顺序实施；R3.3、R3.4 独立。

### R3.1 统一 provider 传输抽象：Agent 作为传输而非平行编排（F8）

- 定义统一接口：`translate_batch(batch, policy) -> BatchResult | Pending(manifest)`。API provider 返回结果；Agent provider 返回 `Pending`，其中携带现有 manifest 内容。
- polish / semantic review / semantic repair / retranslate 的编排代码收敛为单份：遇到 `Pending` 即持久化 manifest 并进入 `awaiting_input`，回填时 response 经同一接口注入后继续同一段编排。
- 现有 artifact 文件名、manifest 格式、fingerprint 规则、事件序列全部保持不变——本项重组的是代码归属，不是磁盘协议。旧 Job 与已等待的 Agent manifest 完全兼容。
- 顺带收敛翻译逻辑的文件边界：`translate.py`（分组与批次协议）、`translation_provider.py`（传输）、`pipeline/translation|polish|semantic.py`（编排）职责单一化，消除 F4 之外残余的交叉引用。
- 测试策略：provider 以参数注入（延续 `opener` 的 DI 先例），新编排测试传 fake provider 对象，逐步替换 `mock.patch` 模块函数的用法（F11）；迁移期间两种方式并存。
- 验收：polish/semantic/retranslate 的 API 与 Agent 行为测试全部通过且编排实现只有一份；对既有等待中 Job 的回填回归测试通过。

### R3.2 内容寻址 step 缓存统一 checkpoint（F7）

- 抽出统一机制：每个 LLM 步骤声明输入（源 cue checksum、模型、端点、prompt 版本、策略、上游产物 checksum）与输出 artifact，框架计算输入哈希作为缓存键，命中即复用、未命中执行后落盘。现有各特性的 fingerprint 正是这些缓存键，本项把"算键、查缓存、失效"从各特性代码抽到一处。
- 直接收益：`continue` 与首跑收敛为同一条执行路径——重跑 pipeline，命中的步骤瞬间跳过；`plan_advance_steps` 与 `_advance_workflow` 的人工对齐义务消失；上游变化后下游"自然失效"（键不匹配），`downstream_of` 主动失效闭包退化为清理手段而非正确性依赖。
- 实施策略：新机制先以现有 progress/checkpoint 文件为存储后端逐特性接入（translation → polish → semantic），每步保持磁盘格式不变、只换读写代码；全部接入后再评估是否统一存储布局。禁止一次性大爆炸迁移。
- 验收：每接入一个特性，其中断恢复测试组（含 fingerprint 失效场景）不改断言通过；`status` 输出保持一致。

### R3.3 schema 演进：启用迁移链，stage 数据化（F9）

- 现状成本：`translate` stage 内部实际有草稿、润色、语义校对、修复、复查、人工修订六个阶段，状态只能由 `semantic_review_waiting` / `_repair_chain_satisfied` 等函数从 artifact 存在性反推，`status` 展示层靠 detail 补丁。
- 方案：`SCHEMA_VERSION` 升至 2，`_MIGRATIONS[1]` 写第一个纯函数迁移：把冻结 stage 枚举扩展为 Job 内的 steps 数据列表（每步含名称、状态、detail），`translate` 的六个子阶段获得显式命名状态。旧 Job 读取时自动沿迁移链升级，`from_dict` 机制已就绪。
- 本项影响面最大（models、recovery、status、事件展示、全部相关测试），是否实施待 R3.1/R3.2 落地后重新评估——若前两项完成后状态反推代码已明显缩减，本项可降级或取消。
- 验收（若实施）：v1 Job 样本经迁移后 `status` / `continue` / `verify` 行为不变；迁移函数有独立单测。

### R3.4 config 单轨化（F10）

- 把 `source_strategy`、`source_rebuild_requested` 等顶层裸键迁入 `WorkflowConfig`（或为原子 Job 建立对应的类型化读写边界），`resolve_target_language` 的回退逻辑收敛到唯一入口。
- 旧 Job 顶层键继续可读（读取边界做一次性归一），写入只走新路径。
- 验收：`grep -rn 'job.config\["' src/xvt/ | grep -v workflow_config` 仅剩边界模块；相关恢复测试通过。

## 长函数治理约定（F13，持续性而非专项）

不安排专项重构。约定：后续任何 PR 触碰 `_process_adaptive_media`、`_acquire_remote_job`、`run_translation`、`apply_agent_semantic_review` 等 150 行以上函数时，顺手按阶段抽出子函数；`transcription.py` 若突破 2000 行，按"本地转写 / 远程获取 / resume 与 rebuild"三簇拆分模块（复用纯搬移四步经验）。

## 交付顺序建议

1. **立即**：R1 全部（四项合计改动量小，可一到两个 PR）。
2. **GUI 之前**：R2.1、R2.2、R2.3 译文/源文修订 SRT 通道与 R2.4 两阶段恢复安全均已完成；polished Agent 主链真实 E2E 也已完成，下一项是 Q6.2 语义收敛验证。
3. **质量验收**：polished API 路径真实 E2E 已完成；Agent 主链真实 E2E 已于 2026-10-11 完成；默认保持 standard + faithful，继续做 Q6.2 收敛验证及多样本质量/成本对照。
4. **质量验收之后**：R3.1 → R3.2，随后再设计 Q7 翻译上下文与术语候选包，避免新增模型步骤后立即重做 provider 和 checkpoint；R3.3 重新评估，R3.4 可穿插。
5. **上述内核稳定后**：实现只消费现有事件、Job、QA artifact、人工修订 SRT 和预览能力的可视化校对工作台。

每项均遵循 AGENTS.md 修改流程：单分支串行、超过 6 文件拆小、行为变化补测试、提交前过 ruff + pytest 硬门槛、同步文档。

## 风险与回退

- R1/R2 均不改变磁盘格式与协议，回退即 revert。
- R2.1 的主要风险是 `test_cli.py`（1516 行）patch 目标漂移——每个搬移 PR 先跑守卫 grep 再跑全量测试。
- R3.1/R3.2 的红线：artifact 文件名、manifest 格式、fingerprint 语义、事件序列不变；任何一步导致旧 Job 或等待中 manifest 不可恢复即停止并回退。
- R3.3 是唯一动 schema 的项，独立成 PR、独立可回退，且默认可取消。
