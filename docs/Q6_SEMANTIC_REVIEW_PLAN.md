# Q6 译文语义校对与一次修复技术方案

## 目标与边界

Q6 只作用于 `translation_quality=polished`，`standard` 保持现状。质量链固定为：润色候选 → 语义校对 → 按完整语义组最多修复一次 → 最终复查 → 必要时人工确认 → 确定性字幕 QA、渲染和烧录。

模型只处理文本，不得修改 cue ID、数量、顺序、时间轴或源文。语义确认只放行模型报告的 review 问题，不能绕过缺译、时间轴、术语或显示宽度等确定性 QA。

## 问题与响应契约

固定分类为 `meaning_error`、`omission`、`terminology`、`awkward_join` 和 `source_suspect`，严重级别统一为 `review`。`source_suspect` 只进入人工确认，永不自动修复；其余分类按涉及 cue 所属的完整语义组修复一次。

API 模式分别执行可 checkpoint 的初次校对、修复和最终复查。Agent 模式使用单个 `task=semantic_review_and_repair` manifest，一次响应返回初次报告、可选修复和最终报告。所有响应严格校验 batch、分类和 cue ID；修复必须完整覆盖所选语义组。

Agent manifest 必须绑定当前 Job、source/candidate checksum、目标语言、prompt/grouping/task version 和完整批次；合并响应在全部初审、修复和复审内容校验通过后才持久化，失败时保留原等待 manifest 供修正后重试。结构纠错提示按当前阶段使用 `issues` 或 `items` 契约。

## 持久化与恢复

新增 checksum artifact：`translation_semantic_qa`、`translation_semantic_repair`、`translation_repaired_cues`、`translation_semantic_qa_final`。

不新增 stage，不升级 Job schema。语义校对复用 `translate` stage，并通过 `phase=semantic_review|repair`、artifact 和 `awaiting_input` 表达细分状态。工作流计划新增 `semantic_review`、`await_agent_semantic_review`、`repair` 和 `await_translation_review`。

人工确认记录绑定 source checksum、当前候选 checksum、报告 checksum、语义校对 prompt version 和精确问题集合；候选或报告变化后自动失效。确认事件为 `translation_review_acknowledged`。

只有两类报告可以确认：没有可修复项的初次报告，或完整修复链之后的最终报告。初次报告包含可修复分类时，修复或复查失败必须继续恢复处理，不能提前确认。初次 `source_suspect` 在其他问题修复后仍保留到最终报告，因为源文没有被修改。最终报告必须与 checksum 有效的初次报告、修复 checkpoint 和修复候选配对，禁止在候选缺失时回退到另一版本文本。

## CLI

- `xvt apply-semantic-review JOB RESPONSE`：回填 Agent 的合并校对与修复响应。
- `xvt acknowledge-translation-review JOB`：高级原子 Job 的语义报告确认入口。
- `xvt continue JOB --acknowledge-translation-review`：端到端 Job 确认当前报告并继续确定性 QA、渲染和可选烧录。

`status` 与 `continue` 共用 planner，必须区分 Agent 回填等待和人工语义确认等待。批处理继续复用单 Job 恢复计划。

## 验收

- 无问题时从润色候选生成最终译文；有可修复问题时只修复一次并复查。
- 最终仍有问题或只有 `source_suspect` 时等待显式确认，不提前生成最终译文或烧录。
- API checkpoint 可从首个未完成批次恢复；过期或损坏进度不被复用。
- Agent fingerprint、完整 batch 和完整语义组覆盖均严格校验。
- Agent 无效合并响应不改写等待 manifest，可提交修正响应重试。
- 修复或复查中断不会把初次可修复报告误判为人工确认状态。
- `source_suspect` 不会因其他问题完成修复而消失。
- 最终报告只批准其实际复查过且 artifact 链完整的候选。
- 语义确认失效规则正确，且确定性 QA 仍能阻断。
- 默认继续保持 `standard + faithful`；是否切换为 `natural + polished` 留待真实 E2E 验收。
