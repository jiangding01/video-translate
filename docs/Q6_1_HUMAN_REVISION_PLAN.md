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
xvt continue JOB
```

模板使用 `task=source_review_fixes`、`scope=source`，fingerprint 绑定 Job ID、当前 `source_cues` checksum 和精确 cue ID 集合。回填必须完整保留模板 ID 集合，只允许修改非空 `items[].text`；过期 source、未知或重复 ID、错误 scope 和无实际变化均拒绝。cue ID、数量、顺序、时间码、words、语言、置信度、warnings 和 provenance 逐项保持。

接受修订后登记 `source_review_revision`、新的 `source_cues`、`source_selection`、`subtitle_qa`，QA 通过时同时登记新的 `source_srt`。所有替换文件使用 fingerprint 区分路径，写完后通过一次 Job 原子保存切换登记，避免覆盖旧文件后保存失败破坏原 checkpoint。新的选择记录使用 `selected_kind=human_reviewed`、绑定新 source checksum，并嵌入上一份选择记录供追溯；cue 自身 provenance 不改写。

源字幕修订会清除字幕与语义确认，并失效旧 source SRT/QA/selection、翻译批次与 checkpoint、润色、语义校对、译文修订、字幕渲染、预览、烧录和媒体 QA。下载字幕、候选 QA、对齐、词级转写、音频和媒体证据保留。确定性源字幕 QA 在修订命令内执行：`failed` 或 `needs_review` 会保存新 source 证据链但不登记 source SRT，从而阻断翻译；人工确认后 `continue` 从新 source cue 恢复，不重新导入旧源字幕。

配置了导入译文字幕的任务首版拒绝源文修订，避免静默复用与新源文不匹配的旧译文。Q6.1b 的 `source_suspect` 自动模板和 `status` 多路径引导在后续独立阶段接入；当前核心命令已经可单独使用。
