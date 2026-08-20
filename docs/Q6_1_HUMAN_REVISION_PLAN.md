# Q6.1 人工校对交互技术方案

## 目标

Q6 的自动语义校对仍可能留下需要人判断的术语、误听或中文表达问题。Q6.1 在不改变 cue ID、数量、顺序和时间码的前提下，把「查看报告后只能整体放行」扩展为可恢复的人工修订闭环。

本阶段分三步串行交付：

1. **Q6.1a 译文修订**：生成可编辑模板，严格回填译文，并仅重新执行语义复查。
2. **Q6.1b 源文修订**：显式选择并修正源 cue，只改文字，重新执行源字幕 QA，并使翻译及全部下游产物失效。
3. **Q6.1c 提示词加固**：根据真实修订样本优化术语和表达；不与人工修订协议耦合。

## Q6.1a 用户流程

当 polished 译文经过 Q6 初审、最多一次自动修复和复审后仍为 `needs_review`：

1. XVT 保留不可变语义报告，并生成 `review/translation-review-<fingerprint>.json`。
2. `xvt status JOB` 展示报告、待编辑模板、首选回填命令和「直接确认」备选命令；`status` 本身不创建或修改文件。
3. 用户只编辑模板中的 `items[].text`，然后执行：

   ```bash
   xvt apply-translation-review-fixes JOB REVIEW.json
   xvt continue JOB
   ```

   端到端 Job 也可执行 `xvt apply JOB REVIEW.json --continue`，在同一进程完成译文回填与续跑。该组合不适用于源文修订；源文变化会使译文及下游失效，必须独立回填后先查看 `status`。

4. XVT 保存完整修订记录和独立候选，重新执行**语义复查**。人工修订后不再获得第二次自动修复预算。
5. 复查通过则进入确定性字幕 QA；仍有问题则生成绑定新报告的新模板，用户可再次修订或显式确认。

## 输入协议与安全边界

模板结构：

```json
{
  "task": "translation_review_fixes",
  "revision_fingerprint": "...",
  "scope": "translation",
  "issues": [],
  "items": [{"id": 1, "text": "修订后的译文"}]
}
```

`revision_fingerprint` 绑定 Job ID、source checksum、当前候选 kind/checksum、报告 kind/checksum、prompt version 和规范化问题集合。回填时必须同时满足：

- `task` 与 `scope=translation` 正确；
- fingerprint 仍对应当前报告；
- `items` 非空，ID 已存在且不重复，文本非空；
- 只更新 `target_text`，不接受时间码、cue 数量、顺序、语言或源文修改。

校验全部通过前不修改 Job。模板是用户输入文件，不登记为 artifact、不写 checksum；接受后的规范化完整结果才进入 Job 证据链。

## Artifact 与恢复

新增 checksum artifact：

- `translation_review_revision`：接受后的规范化完整修订记录；作为用户输入根节点。
- `translation_reviewed_cues`：应用修订后的完整候选快照。
- `translation_semantic_qa_reviewed`：人工候选的语义复查报告。

polished 候选还必须具备 checksum 有效且绑定当前候选 checksum 的 `translation_candidate_qa`。候选变化、报告缺失或损坏时重新计算；报告无效时审批 fail closed。只有能通过修改目标译文解决的确定性问题进入译文修订模板，源文宽度和时间轴类问题不进入该模板。

应用修订会失效旧语义报告/修复/确认、旧字幕 QA 确认以及译文渲染和烧录下游。每轮接受结果写入 fingerprint 区分的独立文件，并在一次 Job 原子保存中同时切换修订记录和候选登记；保存失败时上一轮 checksum 有效的证据链保持可恢复。复查报告与修订候选 checksum 有效时可恢复；API 与 Agent 都只做 review-only。Job schema 和 `StageName` 不升级，继续用 `translate` stage 的 `semantic_review` phase。

`RecoveryPlan` 新增可选 `input_paths` 和 `alternative_commands`。等待人工语义处理时，首选命令是 `apply-translation-review-fixes`，直接确认保留为备选。旧等待 Job 没有模板时，`continue` 会补生成模板；`status` 始终只读。

## 事件

- `translation_review_input_ready`：模板已准备；只携带路径、scope 和条目数。
- `translation_review_revised`：修订已接受；只携带 scope、修改数和候选 checksum。

事件不得包含字幕正文。

## 验收标准

- 正常、过期 fingerprint、未知/重复 ID、空文本和错误 scope 均有测试。
- 无效输入不会修改 `job.json` 或 artifact 登记。
- API/Agent 人工候选都只复查，不再次自动修复。
- 多轮修订、直接确认、`status` 只读和 `continue` 懒生成模板可恢复。
- 修订后 cue ID、数量、顺序和时间码逐项不变，确定性字幕 QA 仍可阻断烧录。

## Q6.1b 源文修订核心流程

用户可为任意 checksum 有效的 source cue 生成独立模板：

```bash
xvt prepare-source-review JOB --cue 13 --cue 14
xvt apply-source-review-fixes JOB REVIEW.json
# 或编辑同 fingerprint 的 source-review-<fp12>.srt 后回填
xvt apply-source-review-fixes JOB source-review-<fp12>.srt
xvt status JOB
# 按 status 输出的恢复命令继续
```

命令同时生成同 fingerprint 的 JSON 与 SRT。JSON 使用 `task=source_review_fixes`、`scope=source`，SRT 以固定文件名承载 fingerprint，并用原时间轴和可选的 `#<cue id>` 注释还原 cue ID。fingerprint 绑定 Job ID、当前 `source_cues` checksum 和精确 cue ID 集合。回填必须完整保留模板 ID 集合，只允许修改非空源文；过期 source、未知或重复 ID、错误 scope、改动时间轴、增删 cue、注释 ID 不匹配、改名和无实际变化均拒绝。cue ID、数量、顺序、时间码、words、语言、置信度、warnings 和 provenance 逐项保持。

接受修订后登记 `source_review_revision`、新的 `source_cues`、`source_selection`、`subtitle_qa`，QA 通过时同时登记新的 `source_srt`。所有替换文件使用 fingerprint 区分路径，写完后通过一次 Job 原子保存切换登记，避免覆盖旧文件后保存失败破坏原 checkpoint。新的选择记录使用 `selected_kind=human_reviewed`、绑定新 source checksum，并嵌入上一份选择记录供追溯；cue 自身 provenance 不改写。

源字幕修订会清除字幕与语义确认，并失效旧 source SRT/QA/selection、翻译批次与 checkpoint、润色、语义校对、译文修订、字幕渲染、预览、烧录和媒体 QA。下载字幕、候选 QA、对齐、词级转写、音频和媒体证据保留。确定性源字幕 QA 在修订命令内执行：`failed` 或 `needs_review` 会保存新 source 证据链但不登记 source SRT，从而阻断翻译；回填后先用 `status` 检查新的恢复计划，QA 满足后再从新 source cue 恢复，不重新导入旧源字幕。

配置了导入译文字幕的任务拒绝源文修订，避免静默复用与新源文不匹配的旧译文；准备和回填入口使用同一适用性判断，不能先生成最终无法提交的模板。

## Q6.1b `source_suspect` 自动引导

当前 checksum 有效的语义报告包含 `source_suspect` 时，XVT 对这些问题引用的 cue ID 取去重并集，并在进入人工等待状态前自动生成对应的 `source-review-*.json` 与同名 SRT。源文模板仍只绑定 Job、当前 source checksum 和精确 cue ID 集合，不绑定语义报告；报告变化但源文和 ID 集合不变时不会覆盖用户尚未提交的编辑。译文模板继续覆盖报告中的全部 review 问题。

`status` 保持只读，并按以下顺序展示恢复路径：

1. 优先回填源文修订；
2. 用户确认源文正确时，可只回填译文修订；
3. 用户接受当前结果时，可显式确认语义报告。

升级前已处于等待状态且缺少源文模板的 Job，`status` 引导运行 `continue`，由恢复流程按报告中的完整 ID 集合懒生成模板；这样不会把大量 `--cue` 参数展开到终端或触及系统命令长度上限。导入译文任务不生成或推荐源文修订，只保留译文修订与确认路径。该行为复用 `await_translation_review`、`RecoveryPlan.input_paths` / `alternative_commands` 和已有 source review 事件，不新增 stage、artifact、事件或 Job schema。

## Q6.1c 提示词加固

API 与 Agent 使用同一套文本质量语义。翻译和润色先把每个 `semantic_groups` 条目作为连续话语理解，再将自然目标语言表达映射回固定 cue；允许同组内协调语序、指代和连接词，但不得跨组移动含义，也不得修改 cue ID、数量、顺序或时间轴。专名、产品名、代码标识符、数字和术语优先保留；只有术语表提供明确目标词时才强制替换，模型不得自行修正疑似错误源文。

语义校对必须检查按 ID 拼接后的完整组，并区分：含义变化、漏译、术语或专名错误、单 cue 可读但拼接生硬，以及有上下文证据的源文疑点。`source_suspect` 不能仅因歧义或陌生词触发，报告必须指出可疑源片段和证据，且 `suggested_items` 为空，源文仍只能通过人工模板修改。定向修复联合重写完整语义组，把建议文本视为线索而非标准答案，不得修复 `source_suspect`。

四类提示词当前版本为 translation v5、polish v3、semantic review v4、semantic repair v3。翻译、润色和修复避免模型无依据添加破折号、公式化对比、空泛连接词、宣传式措辞或抽象拔高，同时明确保留源文真实的对比、强调、列举、打断、语气与全部信息；语义审查把简中标点收口后的半角空格和 cue 边界视为有效分隔，不会只因缺少全角逗号或句号报告 `awkward_join`。review v4 还拒绝无实际修改的建议、`source_suspect` 非空建议和单 cue `awkward_join`。版本继续进入 API checkpoint 指纹和 Agent manifest；升级后旧状态不会被误复用。Q6.1c 不新增 stage、事件、Job schema、依赖或凭据；本轮人工恢复增强新增统一 `apply --continue` 和 checksum 绑定的候选 QA artifact。

## 真实 E2E 后续项（Q6.2 候选）

2026-08-19 使用公开 YouTube 素材完成 polished API 全流程验收：下载、ASR、初译、润色、语义校对、人工修订、烧录和媒体 QA 均完成，最终 29 个 artifact 全部通过 checksum 校验。人工修订后的推进没有重新执行初译、润色或自动语义修复，证明 Q6.1 的恢复边界有效。

本次任务也记录两类尚未实施的体验优化：

1. **修订文件本地预检**：人工回填前，对 JSON/SRT 做与正式 apply 相同的 fingerprint、ID、空文本和确定性候选 QA 检查，但不得修改 Job、登记 artifact 或调用模型。输出应集中列出当前全部阻断的 cue、问题类别、实际值和限制值，并明确哪些问题只能改源文或时间轴解决。具体命令形态留待 Q6.2 设计，不在本方案中预先冻结。
2. **语义复查收敛度**：真实样本中出现过报告正文判断“可接受/无需报告”，却仍将该项登记为 `needs_review`，以及相邻 cue 连接问题在多轮修订中来回变化。后续先建立脱敏回归样本，记录每轮新增、保留、解决的问题和人工轮数，再比较提示词、响应纠错及审查策略。不得依赖中文关键词黑名单直接吞掉问题；任何优化都必须继续保留 meaning error、omission、术语问题和确定性 QA 的 fail-closed 边界。

同一任务中的 YouTube HTTP 403 通过显式浏览器 Cookie 恢复，provider 返回“建议文本未变化”则被现有响应校验拒绝并可由同 Job `continue` 恢复。这两项属于已正确处理的外部条件，不作为 Q6.2 正确性缺陷。
