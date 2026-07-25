# Q5 可恢复字幕润色技术方案

状态：Q5a、Q5b 均已实现；Q5b 的决策完备实施方案见
[Q5b 新 Job 翻译质量模式技术方案](Q5B_NEW_JOB_QUALITY_MODE_PLAN.md)

当前 CLI 同时提供已有译文补跑和新 Job `standard|polished` 质量模式。

## 目标

Q5 在现有语义组翻译之后增加可恢复的第二遍上下文润色，同时保持以下不变量：

1. 润色不得修改 cue ID、数量、顺序、起止时间或 source text。
2. API 模式每完成一个润色批次即保存 checkpoint；中断后不得重做已完成批次或第一遍翻译。
3. Agent 模式保持一次回填：新 Job 的 `polished` 任务由外部 Agent 在一次响应内完成翻译、润色和自检。
4. 已有译文必须能通过显式命令补跑润色，不重新转写或翻译源文。
5. 润色成功后仍必须通过现有确定性字幕 QA；QA 失败不得继续烧录或宣告完成。

Q5 不实现语义问题报告、自动定向修复或语义人工确认。这些属于 Q6。

## 关键决策

- 使用独立 draft 和 polished artifact，不把 `translated_cues` 同时当输入和输出。复用同一种 artifact 虽然文件更少，但中断后无法判断当前内容是草稿还是最终结果。
- Q5 拆成 Q5a、Q5b 两个串行 PR，不做一个跨 CLI、工作流、provider 和恢复层的大 PR。
- Agent 新 Job 保持一次回填，不增加“先翻译、再润色”的第二次人工往返。
- 不新增 stage。新 stage 会扩大 Job schema 和所有恢复消费者的兼容面，现有 `translate` stage 加 phase 已足够表达状态。
- 润色失败时保留 draft 和 checkpoint，不回退成伪成功的 standard 任务。

本方案最关键的假设是 OpenAI-compatible provider 能在完整语义组内稳定返回严格 ID 集合。若模型连续返回结构错误，现有一次纠错预算耗尽后任务会明确失败并保留 checkpoint，不跳过润色；用户可修复 provider 配置后继续，或在 Agent 等待路径回填合规响应。

## 用户行为

### 新 Job

翻译入口增加：

```text
--translation-quality standard|polished
```

- `standard` 是 Q5 上线时的默认值，保持当前单遍语义组翻译行为。
- `polished` 在第一遍翻译后运行第二遍上下文润色。
- 旧 Job 缺少该字段时按 `standard` 处理，不回写改变历史语义。
- `--translation-subtitles` 与 `--translation-quality polished` 不同时使用。用户提供的译文默认视为最终输入；需要润色时先导入，再显式运行 `polish-translation`。

参数接入 `run`、`batch`、`translate` 和 `prepare-translation`。端到端 Job 将值持久化到 `job.config.workflow.translation_quality`；原子翻译将值写入顶层 `job.config.translation_quality`，供回填和状态展示使用。

### 已有译文补跑

新增 advanced 命令：

```text
xvt polish-translation JOB.json [--provider auto|openai-compatible|agent]
    [--base-url URL] [--model MODEL] [--api-key-env NAME]
    [--batch-size N] [--max-retries N] [--timeout-seconds N]
xvt apply-polish-translation JOB.json RESPONSE.json
```

`polish-translation` 只接受 checksum 有效的 `translated_cues`：

1. 将当前译文复制为不可变的 `translation_draft_cues`。
2. 将工作流目标持久化为 `translation_quality=polished`。
3. 失效旧 `translated_cues` 及其 QA、字幕、预览、烧录和媒体 QA 下游产物。
4. API 配置完整时直接逐批润色；`provider=auto` 且配置不完整时生成 Agent 润色批次并进入 `awaiting_input`。显式 `openai-compatible` 缺配置时继续失败，不静默降级。
5. 完成后重建最终 `translated_cues`，运行确定性 QA 和字幕渲染；端到端 Job 再由 `continue` 补齐烧录。

未传覆盖参数时复用 `WorkflowConfig` 的翻译 provider 配置；显式覆盖会更新同一份持久化配置，API Key 仍只记录环境变量名。该命令仅支持带 `WorkflowConfig` 的端到端 Job。旧原子 Job 没有足够的持久化 provider 和恢复目标，Q5 不为其增加第二套恢复配置。

首次执行严格按“写入并登记 draft -> 持久化 polished 目标 -> 失效旧最终产物”的顺序推进。若在前两步之间中断，多出的 draft 不改变原 standard 完成状态；若在持久化目标后中断，`continue` 可以从有效 draft 恢复。

重复执行必须幂等：已有匹配的 draft 和未完成 progress 时继续现有 checkpoint；polished 目标已经满足时直接返回现有产物。命令不得把 `translation_polished_cues` 再复制为新 draft，避免重复润色。

## 处理链路

```text
source_cues
    |
    +--> standard translation --------------------------> translated_cues
    |
    +--> translation_draft_cues
             |
             +--> translation_polish_batches
                        |
                        +--> translation_polish_progress
                                   |
                                   +--> translation_polished_cues
                                              |
                                              +--> translated_cues
                                                         |
                                                         +--> deterministic QA/render/burn
```

`translation_draft_cues` 有两种来源：

- 新 API `polished` Job：第一遍语义组翻译的完整结果。
- `polish-translation`：命令启动时对当前有效 `translated_cues` 的本地快照。

Agent 新 Job 不增加第二次回填。`prepare-translation --translation-quality polished` 在现有批次 manifest 中写入 `task=translate_and_polish` 和独立的 `task_version`，外部 Agent 直接返回最终文本；应用响应时登记 `translation_polish_progress`、`translation_polished_cues` 和最终 `translated_cues`。该路径没有伪造的 draft artifact。

## 润色批次协议

润色继续使用 Q4 的确定性语义组。每个批次只装入完整组，并携带前后各一个完整相邻组作为只读上下文。

每个当前组 item 包含：

```json
{
  "id": 7,
  "source_text": "...",
  "draft_text": "...",
  "duration_ms": 1800,
  "max_target_characters": 14
}
```

批次还包含：

- `source_language`、`target_language`；
- 当前 `semantic_groups`；
- `context_before`、`context_after`，同时提供源文和草稿译文；
- 命中当前组的术语；
- 翻译风格和 `translation_quality=polished`；
- 独立的 polish prompt version 与 semantic grouping version。

provider 的单批响应继续使用现有严格结构：

```json
{"items":[{"id":7,"text":"..."}]}
```

响应必须完整覆盖当前批次的 item ID，不能包含上下文 ID。解析后逐项回填到 draft cue 的 `target_text`，其他字段从 draft cue 原样保留。显式 Agent 回填文件聚合全部 items，并在顶层额外回传当前 manifest 的 `fingerprint`。

## Prompt 约束

润色模型只做目标语言表达优化：

- 保持含义、语气、专有名词、数字和术语要求；
- 可在同一语义组内调整措辞承接，但不得跨组搬运信息；
- 不得合并、拆分、增加或删除 cue；
- `max_target_characters` 仍是显示建议，不得为满足长度漏译或制造病句；
- 前后组只用于理解，响应不得包含其 ID；
- provider 单批输出只包含 JSON `items`；显式 Agent 回填另带顶层 fingerprint。

润色 prompt 使用独立常量 `TRANSLATION_POLISH_PROMPT_VERSION`。修改第一遍翻译 prompt 不应自动废弃已经完成的润色；修改润色 prompt 必须废弃润色进度及其下游。

Q5 不增加第三方服务、Python 依赖或凭据类型。API 继续读取 `XVT_TRANSLATION_BASE_URL`、`XVT_TRANSLATION_MODEL` 和 `XVT_TRANSLATION_API_KEY`，Key 不写入 Job、artifact 或事件。发送给 provider 的新增内容仅限 source cue、draft target text、术语和相邻语义组上下文。

## Checkpoint 与满足度

`translation-polish-progress.json` 同时承担 checkpoint 和完成证据，包含：

```json
{"fingerprint":"<sha256-hex>","completed_batches":[1,2],"items":[{"id":1,"text":"..."}]}
```

API 指纹至少包含：

- source cue checksum；
- draft cue checksum；
- source / target language；
- provider mode、Base URL 和 model；
- batch size；
- translation policy；
- polish prompt version；
- semantic grouping version。

API 每完成一个批次后原子重写进度并更新 artifact checksum。指纹不匹配时发出 `translation_progress_reset`，仅重置润色进度，不重做 draft。

Q5a 显式 Agent 回填成功后写入相同结构的完整 progress 文件；provider mode 已进入 fingerprint，不额外保存 `mode` 字段。Agent 响应必须回传当前 manifest 的 fingerprint，避免旧 draft 的响应误用于新 draft。该路径与 API 一样绑定 source/draft checksum、目标语言、策略、prompt 和 grouping version。

Q5b 新 Job 的一次 Agent 回填使用 source cue checksum、目标语言、translation policy、
`task_version`、prompt/grouping version 和完整 batches 生成独立指纹，不要求不存在的 draft
checksum。指纹对不含 fingerprint 字段的规范化 manifest payload 计算，避免依赖
`translation_batches` 文件 checksum 形成自引用。`task_version` 与 API 的 polish prompt version
独立，Agent 协议变化不会误用旧完成证据。

API `translation_quality=polished` 只有同时满足以下条件才算完成：

1. `translation_draft_cues`、`translation_polish_batches`、`translation_polish_progress`、`translation_polished_cues` checksum 有效；
2. progress 指纹与当前配置及输入匹配，全部批次完成；
3. `translated_cues` 与 `translation_polished_cues` 的 cue ID、时间轴和目标文本一致；
4. `translated_srt` 和 `bilingual_srt` checksum 有效。

Q5b 新 Job Agent `translation_quality=polished` 的计划完成条件为：

1. 带 `task=translate_and_polish` 的 `translation_batches` checksum 有效；
2. `translation_polish_progress` 指纹匹配且记录完整 ID 集合；
3. `translation_polished_cues` checksum 有效；
4. 最终 cue 对账及 SRT 条件与 API 路径相同。

`standard` 继续沿用当前满足度，不要求任何润色 artifact。

## Artifact 与失效关系

Q5 新增：

```text
translation_draft_cues
translation_polish_batches
translation_polish_progress
translation_polished_cues
```

直接依赖：

```text
translation_draft_cues <- source_cues + translation_response + translation_mapping + translation_progress + cue_retranslation_response
translation_polish_batches <- source_cues + translation_draft_cues
translation_polish_progress <- translation_polish_batches + translation_batches
translation_polished_cues <- source_cues + translation_polish_progress
translated_cues <- translation_response + translation_mapping + translation_progress + cue_retranslation_response + translation_polished_cues
```

`translation_draft_cues` 不依赖 `translated_cues`，避免 `translated_cues -> draft -> polished -> translated_cues` 环。显式补跑命令虽然从现有译文复制 draft，持久化依赖仍绑定产生该译文的 source 和 translation inputs。

源 cue 或第一遍译文变化会失效或替换 draft 及全部润色下游。只改变润色术语策略、prompt、provider 或质量模式时，由显式配置指纹检查保留 draft，并失效 polish batches、progress 及其下游；这部分不能只依赖静态 artifact DAG。

### 与现有译文修复命令的组合

`retranslate-cues`、`apply-cue-retranslation` 或 `import-translation` 在 polished Job 上产生新译文时，不得保留旧 `translation_polish_progress` 或 `translation_polished_cues`：

1. 先按现有命令得到完整的新译文 cue 集合。
2. 将该集合登记为新的 `translation_draft_cues`。
3. 失效旧 polish batches、progress、polished cues 和最终渲染/烧录产物。
4. 保持 `translation_quality=polished`，由 `continue` 对新 draft 完整重跑润色。

Q5 不实现局部润色 checkpoint 复用。draft checksum 变化后完整重跑第二遍，避免未选中组沿用与新上下文不一致的旧润色。standard Job 的局部重译和导入行为保持不变。`resegment-translation` 改变 source cues，继续通过 artifact 依赖闭包失效 draft 和全部润色下游。

## 工作流与恢复

不新增 `StageName`，继续复用 `translate`：

- draft 或 polish 执行中：`running`；
- Agent 等待翻译或润色响应：`awaiting_input`；
- 最终译文通过确定性 QA：`succeeded`；
- provider、响应校验或确定性 QA 失败：`failed`。

`plan_advance_steps` 增加：

```text
polish
await_agent_polish
```

优先级为：

```text
translate/import -> polish -> render -> burn
```

恢复规则：

- 有有效 draft、缺 polish 完成证据：`continue` 从润色 checkpoint 继续。
- Agent 润色等待：`status` 提示 `apply-polish-translation`，`batch-continue` 保持等待。
- 有有效 polished cues、缺最终字幕：只重新 finalization/QA/render，不调用 provider。
- polish provider 失败：保持 draft 和已完成进度，恢复类别为 `retryable`。
- 确定性 QA 失败：沿用现有人工修复或字幕确认路径，不因 `polished` 自动放行。

## 事件与普通输出

现有翻译批次事件增加可选 `phase`：

```text
draft
polish
```

Q5 不新增事件类型。`translation_batch_started`、`translation_batch_retrying`、`translation_batch_completed` 和 `translation_progress_reset` 在润色阶段写入 `phase=polish`；旧消费者忽略可选字段即可。

普通输出明确显示“翻译草稿”和“润色译文”，避免两个阶段都显示为“翻译”。JSONL stdout 继续保持纯事件。

## 模块边界

新增 `src/xvt/pipeline/polish.py`，只负责：

- draft 快照；
- 润色批次准备与 Agent 回填；
- API checkpoint 推进；
- polished cue 登记和最终化调用。

依赖方向保持：

```text
common <- translation <- polish
```

`translate.py` 保存纯数据模型、语义组和批次构造；`translation_provider.py` 保存 OpenAI-compatible 请求边界；`workflow.py` 只做目标判定和编排，不复制批次或 cue 算法。`pipeline/__init__.py` 只 re-export 公共入口。

## 实施切分

Q5 使用两个串行 PR，第二个只在第一个合入 `main` 后开始。

### Q5a：已有译文可恢复补跑润色

交付：

- 四种新 artifact 与无环依赖图；
- `WorkflowConfig.translation_quality`，缺失时默认 `standard`；显式补跑时持久化为 `polished`；
- 润色批次、provider prompt 和严格响应校验；
- API 逐批 checkpoint；
- `polish-translation` / `apply-polish-translation`；
- `status` / `continue` / batch 恢复；
- 现有译文快照、下游失效、确定性 QA 和重新渲染。

Q5a 独立可用：用户可以对任意现代端到端 Job 的现有译文补跑润色；Q5b 在此基础上接入新 Job，不改变该入口。

### Q5b：新 Job 质量模式

实现时以 [Q5b 新 Job 翻译质量模式技术方案](Q5B_NEW_JOB_QUALITY_MODE_PLAN.md) 为准；
本节仅保留阶段交付边界。

交付：

- 新 Job 和原子翻译入口的 CLI 参数；
- `standard` 默认及旧 Job 兼容；
- API 新 Job 自动执行 draft -> polish；
- Agent `task=translate_and_polish` 一次回填；
- 质量感知的工作流满足度、完成摘要、Skill 和用户文档。

Q5b 完成后 Q5 已闭环。默认仍是 `standard + faithful`；切换 `natural + polished` 必须等待 Q6 和真实 E2E。

两个 PR 都会超过 6 个文件，按项目约束在各自分支内拆成三组可单独验证的提交：

1. 协议与纯逻辑：`translate.py`、`translation_provider.py`、`pipeline/polish.py` 及对应单元测试。
2. 持久化与编排：`artifacts.py`、`workflow_config.py`、`workflow.py`、`recovery.py`、`pipeline/__init__.py` 及对应流程测试。
3. CLI 与文档：`cli.py`、Skill、README、架构、Roadmap、HANDOFF 和 CLI 测试。

每组提交都必须通过相关测试；PR 合并前再运行全量硬门槛。Q5a 合并后从更新后的 `main` 创建 Q5b 分支，不并行向旧 `main` 发散。

## 测试与验收

自动化测试至少覆盖：

1. 润色前后 cue ID、数量、顺序、时间码和 source text 完全一致。
2. 批次不切开语义组，上下文 ID 不可进入响应。
3. API 中途失败后只请求未完成润色批次，不重做 draft。
4. draft checksum、provider、model、policy、prompt 或 grouping version 改变时按边界重置。
5. Agent `translate_and_polish` 与显式 Agent 润色均严格校验完整 ID。
6. 旧 Job 缺质量字段时保持 standard 行为。
7. `--translation-subtitles --translation-quality polished` 被明确拒绝并给出补跑命令。
8. `polish-translation` 失效旧 QA、字幕、预览、烧录、指纹和媒体 QA，但保留 source 与 draft。
9. `status`、`continue` 和 `batch-continue` 对 API 重试、Agent 等待和只缺渲染三种状态一致。
10. 确定性 QA error 继续阻断烧录；needs_review 仍走现有字幕确认通道。
11. polished Job 局部重译或重新导入译文后不会复用旧 polished cues，而是以新 draft 重跑润色。
12. 重复执行 `polish-translation` 只恢复、复用或返回完成结果，不发生二次润色。

每个 PR 运行：

```bash
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pytest -q
uv run --no-sync xvt --help
```

Q5b 合入前增加一次使用 stub provider 的端到端恢复测试。真实翻译 API 和整段烧录不进入默认测试套件；Q5/Q6 全部完成后再按总方案执行真实 E2E。

## 明确不做

- 不实现 Q6 语义 QA、问题分类、自动修复或 `--acknowledge-translation-review`。
- 不修改 cue 时间轴或 source text。
- 不增加新的 Job stage、schema version、数据库、队列或后台 worker。
- 不增加第二个 Agent 翻译回填往返。
- 不改变 `faithful` 和 `standard` 默认值。
- 不为没有 `WorkflowConfig` 的旧原子 Job 增加新的恢复配置。

## 风险与回滚

最大风险是质量满足度判断只看文件存在，导致旧 standard 译文被误认为 polished。方案通过可重算 fingerprint、完整 progress 和 polished cue 对账共同阻断该问题。

第二个风险是 artifact DAG 形成环。draft 不依赖最终 `translated_cues`，并继续由现有 DAG 拓扑测试强制验证无环。

回滚时删除 `translation_quality` 入口和 polish 编排即可恢复当前 standard 行为；新增 artifact 是可选加法，旧 CLI 会忽略未知 kind，不需要 Job schema 迁移。已经生成的最终 `translated_cues`、字幕和烧录产物仍按现有 checksum 规则可读。
