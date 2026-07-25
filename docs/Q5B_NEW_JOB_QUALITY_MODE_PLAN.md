# Q5b 新 Job 翻译质量模式技术方案

状态：已实现

## 目标

Q5b 将 Q5a 已有译文补跑润色能力接入新 Job，同时保持当前默认行为不变：

- `standard`：现有单遍语义组翻译；
- `polished`：API 模式先生成可恢复草稿，再执行可恢复润色；
- Agent 模式在一次响应中完成翻译、润色和自检，不增加第二次人工往返；
- 旧 Job 缺少质量字段时仍按 `standard` 处理；
- 相对 source cue，翻译链只允许写入 `target_text` 和请求的 `target_language`，不得修改
  cue ID、数量、顺序、时间码、源文或其他源数据；润色阶段只允许替换 `target_text`。

Q5b 不实现 Q6 的语义问题报告、自动定向修复或人工语义确认，也不改变
`standard + faithful` 默认值。

## CLI 契约

新增参数：

```text
--translation-quality standard|polished
```

接入命令：

```text
xvt run
xvt batch
xvt translate
xvt prepare-translation
```

规则：

1. `run` 和 `batch` 的默认值是 `standard`，并写入新 Job 的工作流配置或 batch
   manifest。
2. `translate` 和 `prepare-translation` 对新原子 Job 的有效默认值也是 `standard`；若
   Job 已有持久化质量目标，省略参数时沿用该目标，不能用解析器默认值静默覆盖。
3. `polished` 必须同时提供目标语言。
4. `run --translation-subtitles ... --translation-quality polished` 必须在创建 Job 前失败。
   用户提供的译文视为最终输入；需要润色时先导入，再显式执行
   `polish-translation`。
5. `import-translation`、`retranslate-cues` 和 `resegment-translation` 不新增质量参数，
   继续沿用 Q5a 已定义的 draft 重建和下游失效语义。
6. 原子命令不得覆盖端到端 Job 已持久化的质量或目标语言。参数与既有 workflow 目标不一致
   时，在 stage、artifact 和配置发生任何变化前失败；standard 端到端 Job 要升级为 polished
   必须使用 `polish-translation`。
7. `apply-translation` 保留现有 `--target-language` 参数；polished 响应中的 manifest、当前
   workflow/原子配置和命令参数必须一致，否则在任何 Job mutation 前失败。

端到端 Job 将质量写入 `job.config.workflow.translation_quality`。原子翻译 Job 将质量
写入 `job.config.translation_quality`，同时继续使用既有顶层 `target_language` 和
`translation_policy`。不为原子 Job 创建伪造的 `WorkflowConfig`，因此 `continue` 仍只处理
真正的端到端 Job。

## API 模式

### 新端到端 Job

`translation_quality=polished` 时执行：

```text
source_cues
  -> translation_progress
  -> translation_draft_cues
  -> translation_polish_batches
  -> translation_polish_progress
  -> translation_polished_cues
  -> translated_cues
  -> deterministic QA/render/burn
```

第一遍翻译仍使用现有语义组、翻译 prompt 和 `translation_progress`。完整译文不得先登记为
最终 `translated_cues`，而是写入 `translation_draft_cues`。工作流随后调用 Q5a 润色入口；
润色失败时保留 draft 和已完成批次，`continue` 从准确断点恢复。

`standard` 路径保持现状，不创建任何 polish artifact。

### 原子 `translate`

原子命令没有 `WorkflowConfig`，也不由 `continue` 恢复：

- 首次执行 `translate --translation-quality polished` 时完成 draft，再执行润色；
- 中断后用户重跑同一条 `translate` 命令；
- 仅当 checksum 有效的 `translation_progress` 指纹与当前参数匹配，且其完整 items 可重建出
  当前 `translation_draft_cues` 时，才跳过第一遍翻译；
- provider、model、batch size、策略、prompt 或语义分组版本变化时，旧 draft 不可复用，
  必须按现有第一遍翻译指纹重新生成；
- 润色继续使用 Q5a 的 batch/progress 指纹恢复，不持久化 API Key，只使用环境变量名。
- polished 满足度已经成立时返回 `reused`，不得重写 Job、QA、字幕或再次调用 provider。

实现时将 Q5a 的润色执行拆成“从 `WorkflowConfig` 解析设置”和“使用已解析设置推进”两层。
端到端入口使用前者；原子 `translate` 使用当前命令参数调用后者。该拆分只复用现有流程，
不增加新的公共配置模型。

## Agent 一次回填协议

### Manifest

`prepare-translation --translation-quality polished` 仍生成
`translation-batches.json`，但增加：

```json
{
  "task": "translate_and_polish",
  "task_version": 1,
  "fingerprint": "<sha256-hex>"
}
```

`batches` 继续使用现有 `TranslationBatch`：只包含源文、语义组、上下文、术语和显示建议，
不伪造不存在的 `draft_text`。任务说明要求外部 Agent 先翻译、再结合完整组上下文润色和
自检，最终只返回每个原 cue ID 的最终文本。

`standard` manifest 保持现有形状，不强制增加 `task` 或 fingerprint，确保已经等待输入的
旧 Agent Job 仍可按原协议回填。

### Fingerprint

Agent fingerprint 不使用 `translation-batches.json` 文件自身的 checksum，否则 fingerprint
写入文件后会形成自引用。计算方式固定为：

```text
sha256(canonical_json(manifest_payload_without_fingerprint))
```

规范化 payload 至少包含：

- Job ID；
- checksum 有效的 source cue checksum；
- source / target language；
- `task=translate_and_polish` 和独立 `task_version`；
- translation prompt version 与 semantic grouping version；
- translation policy；
- 完整 batch payload。

JSON 使用排序 key、紧凑分隔符和 UTF-8。应用响应时先验证已登记 manifest checksum，再从
manifest 去掉 fingerprint 重新计算，不能只信任文件中的字符串。

### Response 与应用

响应格式：

```json
{
  "fingerprint": "<manifest fingerprint>",
  "items": [{"id": 1, "text": "最终润色译文"}]
}
```

`apply-translation` 根据当前有效 manifest 的 `task` 分流：

- 无 `task` 的 manifest：沿用现有 standard/旧 Job 严格 ID 契约；
- `translate_and_polish`：要求 fingerprint 匹配，并精确覆盖 manifest 中全部 item ID；
- 未知 task 或 task version：明确失败，不降级为 standard。

成功后按顺序登记：

1. `translation_polish_progress`：记录 manifest fingerprint、全部 batch ID 和最终 items；
2. `translation_polished_cues`：以 source cue 为权威复制 ID、时间码、源文、词级时间戳、
   source language、置信度、来源和警告，只写入最终 `target_text` 并将 `target_language`
   设置为请求语言；
3. `translated_cues`：与 polished cues 完全一致；
4. 现有确定性译文 QA、SRT/ASS 渲染和后续烧录。

该路径不创建 `translation_draft_cues` 或 `translation_polish_batches`，因为外部 Agent 只返回
最终结果，伪造中间草稿会削弱恢复证据。

progress 在 polished cues 之前落盘是有意的恢复边界：如果进程在第 1 至第 4 步之间中断，
端到端 Job 的 `continue` 或原子 Job 重跑同一 `apply-translation` 时，必须从 checksum 有效且
完整的 progress 确定性重建缺失的 polished/final cues 并补跑 QA 和渲染，不再次请求 Agent。

## 满足度与恢复

polished 满足度分为两条互斥证据链。

### API / Q5a 链

继续使用 Q5a 条件：有效 draft、polish batches、完整且指纹匹配的 polish progress、polished
cues、最终 cues 和译文 SRT。

### Agent 新 Job 链

必须同时满足：

1. 有效 `translation_batches`，且 task/version/prompt/grouping version 均受支持；
2. manifest fingerprint 可由规范化 payload 重算；
3. `translation_polish_progress` fingerprint 匹配、batch ID 完整、item ID 精确；
4. 根据 source cues 和 progress items 可确定性重建 `translation_polished_cues`；
5. `translated_cues` 与 polished cues 完全相同；
6. `translated_srt` 和 `bilingual_srt` checksum 有效。

内部满足度拆为“核心 polished cues 已满足”和“用户可见字幕已满足”。这样在 Agent 响应已
落盘、但 QA 或渲染中断时，`plan_advance_steps` 返回 `render`，不能因缺 draft 错报
`repair_polish_draft`。

恢复优先级：

```text
await_agent_translation -> translate/import -> polish -> render -> burn
```

具体规则：

- `translate_and_polish` manifest 正在等待时仍使用 `await_agent_translation`，提示执行
  `apply-translation`，不引入第二个 Agent 命令；
- API 有有效 draft 且润色未完成时返回 `polish`；
- Agent 已有完整 polished 证据但缺 QA/SRT 时返回 `render`；
- Agent manifest 和完整 progress 有效但 polished/final cues 缺失时，同一 `render` 步骤先从
  progress 确定性重建 cues，再执行 QA 和渲染；
- 有效 `translate_and_polish` manifest 尚无完整 progress 时保持
  `await_agent_translation`，即使上一次无效响应把 stage 记为 failed；
- Agent manifest 缺失、checksum 失效或 fingerprint 无法重算时，planner 只返回 `translate`；
  后续翻译执行再按 artifact 依赖关系失效旧 progress 和下游，满足度检查本身不修改 Job；
- 只有 API/Q5a 链已经开始且 draft 缺失或 checksum 失效时，才返回
  `repair_polish_draft`；
- `batch-continue` 对等待输入项保持 `awaiting_input`，其他状态复用同一 planner；
- QA error 或 `needs_review` 继续使用现有阻断和人工确认通道。

原子 Job 不进入 `plan_advance_steps`：有效 Agent manifest 等待时，`status` 继续提示
`apply-translation`；API 中断时提示用户重跑带原参数的 `translate`。Q5b 不让原子 Job 获得
`continue` 语义。

## 事件与输出

不新增事件类型。对 polished API Job：

- 第一遍 provider 批次事件增加 `phase=draft`；
- 第二遍继续使用 Q5a 的 `phase=polish`；
- standard Job 保持原事件形状，避免无意义协议变化。

`WORKFLOW_PLANNED` 对 polished API 目标包含 `translate`、`polish`；Agent 实际以一次 manifest
完成两步，但等待原因明确显示 `translate_and_polish`。普通完成摘要显示实际质量模式和最终
产物；JSONL stdout 继续只输出事件。

## 安全与兼容性

- API Key 仍只从指定环境变量读取，不写入 Job、manifest、artifact 或事件；
- manifest、响应和 source cue 都必须来自 checksum 有效的已登记 artifact；
- 外部命令边界不变化，继续使用参数数组；
- 不新增 stage、Job schema version、artifact kind、事件类型或第三方依赖；
- 旧 WorkflowConfig 缺质量字段、旧原子 Job 缺顶层字段、旧 Agent manifest 缺 task 时均保持
  `standard`；
- 旧 batch manifest 缺质量字段时由 `WorkflowOptions` 默认值恢复为 `standard`，不升级 batch
  schema；
- 未知质量值、Agent task 或 task version 必须失败，不能静默降级；
- 已持久化的 polished 质量或目标语言与原子命令参数冲突时，必须在修改 Job 前失败；
- 一个 Job 仍只允许一个目标语言。

## 实施切分

本任务超过 6 个文件，按三组独立验证：

### 1. 协议与翻译主链

涉及：

- `src/xvt/translate.py`
- `src/xvt/pipeline/translation.py`
- `src/xvt/pipeline/polish.py`
- `src/xvt/translation_provider.py`（仅当 Agent 任务说明需要共用 prompt 常量）
- 对应 pipeline/provider 测试

交付 Agent manifest/fingerprint/一次回填、API draft 推进、原子命令恢复和 cue 不变量。

### 2. 工作流与批处理

涉及：

- `src/xvt/workflow.py`
- `src/xvt/workflow_config.py`
- `src/xvt/recovery.py`
- `src/xvt/batch.py`
- 对应 workflow/config/recovery/batch 测试

交付质量持久化、双证据链满足度、恢复优先级和 batch manifest 复用。

### 3. CLI 与文档

涉及：

- `src/xvt/cli.py`
- `tests/test_cli.py`
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/EVENTS.md`
- `ROADMAP.md`
- `HANDOFF.md`
- `CHANGELOG.md`
- `skills/video-translate/SKILL.md`

交付参数、冲突校验、普通输出、帮助文本和状态同步。

## 测试与验收

自动化测试至少覆盖：

1. `standard` 新 Job 产物与当前行为一致，不创建 polish artifact。
2. polished API 新 Job 按 draft -> polish 顺序执行，两个阶段事件可区分。
3. 第二遍中断后只恢复未完成润色批次，不重做第一遍。
4. 原子 API 重跑只有在 translation progress 可重建同一 draft 时才复用 draft。
5. polished Agent manifest fingerprint 可重算，响应必须回传并严格覆盖全部 ID。
6. 旧 Agent manifest 无 task 时继续按 standard 回填。
7. 未知 task/version、旧 fingerprint、缺失/重复/额外 ID 均失败且不写最终产物。
8. Agent 应用在 progress、polished cues 或最终 cues 落盘后中断时，端到端 Job 通过
   `status`/`continue`、原子 Job 通过重跑同一 `apply-translation` 确定性补齐后续产物，
   不再次请求 Agent。
9. Agent manifest 失效时重新准备任务；API draft 损坏仍提示人工修复，两条路径不互相误判。
10. 无效 Agent 响应后仍提示对当前 manifest 执行 `apply-translation`。
11. `run`、`batch`、`translate`、`prepare-translation` 的默认值和持久化位置正确。
12. `--translation-subtitles` 与 `polished` 在创建 Job 前失败。
13. 原子命令与端到端 Job 的质量或目标语言冲突时，在任何 Job mutation 前失败。
14. cue ID、数量、顺序、时间码、source text 和 source language 在 Agent/API polished 前后
    不变，target language 精确等于请求语言。
15. 确定性 QA error 阻断烧录，`needs_review` 不自动放行。
16. 使用 stub provider 完成一次端到端恢复测试，不访问真实网络。
17. 已完成的 polished API/Agent Job 重复执行时纯复用，不改写 Job 或调用 provider。

验证命令：

```bash
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pytest -q
uv run --no-sync xvt --help
```

涉及打包内容时再执行 `uv build`。真实 API、YouTube 和整段烧录不进入默认测试；Q5b 合入后
再按用户授权执行一次真实 polished E2E。

## 完成标准

- 四个入口的质量参数行为一致，默认和旧 Job 兼容；
- API 能从 draft 和 polish checkpoint 准确恢复；
- Agent 只需一次回填，且 stale response 无法应用；
- 两条 polished 证据链不会互相误判；
- 任一 QA 失败都不会烧录或发出完成终态；
- 所有 cue 与时间轴不变量保持；
- Ruff、全量测试、CLI smoke test 和 stub E2E 全部通过；
- README、架构、事件、Roadmap、Skill、Changelog 和 HANDOFF 与实现一致。
