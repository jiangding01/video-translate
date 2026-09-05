# 恢复与回填契约

本文是 `status` / `continue` / `batch` / `batch-continue` / `xvt apply` / `relink-burned-output` 共用恢复语义的单一来源。判定顺序、产物有效性口径、回填路由和烧录 proof 资格都以这里为准，其它文档不得另立说法。

模块职责、artifact 依赖图与失效闭包见 [架构说明](ARCHITECTURE.md)；事件名与字段见 [事件说明](EVENTS.md)。

## 批处理恢复分类与中断兜底

### 中断兜底

批次首次落盘后的 `batch_started` 事件必须位于批处理的中断兜底内。任何中断都要留下 `interrupted` 与 `next_action`，不允许出现「批次已落盘但事件流里看不出被中断」的状态。

### 等待项绝不自动推进

- 已登记烧录成品缺失、但具备重新关联资格的 Job 会停在 `awaiting_input`，**绝不由批处理自动重烧**。用户须先显式 `relink-burned-output`，或明确选择 `continue` 重烧。
- 未回填的局部重译（`retranslate-cues`）同样是等待项，条目记为 `awaiting_input` 而不是已完成。
- 通则：等待项绝不由批处理自动推进。

### 已完成条目的产物失效分类

**非目标产物**（`normalized_audio`、`word_transcript`、批次 manifest 等中间 checkpoint）失效只做标注，不改写终态：

- `item["verification"].status == "degraded"`
- `batch_item_completed.verification`

**目标产物**失效仍按各自路径处理：

- `burned_video`：走 relink / 重烧的 `awaiting_input`。
- `bilingual_ass`：已经在 `plan_advance_steps` 的译文满足度判定内（登记过就必须有效），失效时条目的恢复分类根本不是 `complete`，直接落到 `continue`，由 `render` 零 API 成本重建。
- `source_srt`：不在 `plan_advance_steps` 判定内，但在纯转写批次上就是交付物，条目落到 `continue` 由 `resume_job` 真正重建，不得当作「非目标产物」放行。

### 输出与退出码

- 普通模式下 `batch` / `batch-continue` 会逐条打印需要处理的条目及其恢复命令。数据取自**已持久化的 `recovery_plan`**，不重算。
- 两者同样支持 `--fail-on-waiting`：无失败条目、有等待条目时返回 10；有失败条目仍返回 6（**6 优先于 10**）。

## 端到端推进计划的判定顺序

### 译文满足度

译文满足度包含 SRT、译文 QA 报告与**已登记过**的 `bilingual_ass`。渲染侧只对有正整数宽高的媒体写 ASS，因此从未登记过 ASS 的 Job 不要求它。缺渲染、QA 报告或 ASS 时走 `complete_translated_outputs`。

### 判定顺序

1. `plan_advance_steps` 在**函数体最前**判定局部重译等待（`await_cue_retranslation`）。理由：它会叠加在一个译文目标已满足的 Job 上，放在后面就会被「已满足」短路掉。
2. 在 `TRANSLATE` 等输入且 `translation_batches` checksum 有效时，早退为 `await_agent_translation`。这一早退**必须排在 `translated_cues` → `render` 之前**：新整轮请求不会失效旧译文，按 render 先判会把等待误报成「只差渲染」。

## 产物有效性校验与 checksum 记忆化

### 与 continue 同一套校验

`status` 的产物有效性判定与 `continue` 使用**同一套全量 checksum 校验**，只是同一次判定内对同一文件只哈希一次：进程内、单次调用作用域内的记忆化，不跨命令、不落盘。

### 已知且被接受的边界

记忆化的身份键含 `st_mtime_ns`，由此有一条已知且被接受的边界：在同一个只读作用域执行期间（毫秒级）把文件改成等长内容并把 mtime 恢复原值，会复用旧哈希——与 git 的 racy-timestamp 取舍同类。

`build_recovery_plan` 本身只读，但 `batch` 会把 plan 及其完成分类写入 `batch.json`，所以该窗口里的误判**可能持久化**。接受它的理由：

- 命中需要外部写入者绕过 Job 锁、精确插入首次哈希后的极短窗口。
- 无缓存时「计划返回后、落盘前」仍有更宽的同类 TOCTOU。
- `xvt verify JOB` 会在作用域外重新哈希并识破。

### 元数据只能证伪，不得证实

除上述记忆化窗口外，size、mtime 等文件元数据只能用于**证伪**（元数据与登记值不符即判失效），不得用于**证实**有效——`cp -p`、`rsync -a` 和备份还原都会保留 mtime。

既有成例同此界线：

- `add_artifact` 写后立即哈希登记。
- `_burn_fingerprint` 的 size+mtime 只参与身份指纹，不承担有效性判定。

## `xvt apply` 统一回填路由

`xvt apply JOB RESPONSE` 是统一回填入口，但路由规则依响应类型而不同。

### 路由规则

- **人工修订模板**（`source-review-*.json` / `translation-review-*.json`，CLI 生成、用户编辑后原样提交）携带 `task` 字段，按该字段路由。
- **人工修订 SRT**（固定命名 `translation-review-<fp12>.srt`）按文件名识别为修订通道，不与 `import-translation` 的字幕输入混淆。
- **Agent 翻译/润色/语义校对响应**契约（见 `translation-protocol.md`）不含 `task` 字段，按 Job 当前等待步骤路由——仅支持有持久化 `config.workflow` 目标的端到端 Job。
- **原子 Job**（无 `config.workflow`，例如单独 `xvt translate` 创建的等待）不受统一入口支持。路由不出来时**报错而不猜测**，需改用对应的显式 `apply-*` 命令。

### `--continue` 与 `--fail-on-waiting`

译文相关响应可加 `--continue` 在回填后继续同一工作流。`--fail-on-waiting` 透传给这次续跑：停在下一个等待态时返回 10；不加 `--continue` 时该旗标无作用。

源文修订会失效译文及下游，必须先独立回填并查看 `status`，因此**拒绝 `--continue` 组合**。

### 推荐命令

- 端到端 Job 上的人工 translation review：`xvt apply JOB FILE --continue`（回填并续跑同一工作流）。
- 原子 Job（无 `config.workflow`）拿不到 `--continue`，推荐命令退回 `xvt apply-translation-review-fixes JOB FILE`。
- 人工 source review 的推荐命令仍是 `xvt apply-source-review-fixes JOB FILE` 加一次显式 `status`，保持两步。

各 `apply-*` 命令保留为稳定入口，语义零改动。

### 刻意不纳入路由的命令

需要额外参数的 `apply-cue-retranslation` 与输入非 JSON 的 `import-translation` 不纳入统一路由。

局部重译现在是 `plan_advance_steps` 的一个正式等待步骤（`await_cue_retranslation` ∈ `AWAIT_STEPS`），但仍刻意不进 `_STEP_ROUTES`——`apply-cue-retranslation` 必须显式收 `--cue` 与 `--target-language`，而 `xvt apply` 的命令行里没有这两个参数、也不能从响应里安全地推出来。

理由显式登记在 `cli._UNROUTABLE_AWAIT_STEPS`。**新增等待步骤必须在两张表（`_STEP_ROUTES` / `_UNROUTABLE_AWAIT_STEPS`）之一里表态**，守卫测试强制。

## 烧录 proof 与成品重新关联资格

### proof 的签发

- `xvt burn JOB` 是独立烧录入口（advanced）。该原子命令可覆盖持久化 workflow 之外的媒体、字体和编码参数，因此**不签发 workflow v2 proof**。
- 只有 `run` / `continue` 按持久化目标完成的烧录，才登记 checksum 保护的 `version=2` `burn_fingerprint` proof，绑定 fingerprint、`burned_video` checksum 和实际源媒体内容 checksum。
- 旧 / legacy fingerprint 可按既有轻量完成语义读取，但**不能用于 relink**。

### relink 的匹配条件

`xvt relink-burned-output JOB EXISTING_VIDEO` 是已移动烧录成品的唯一重新关联入口（advanced）。仅接受 checksum 有效的 v2 proof，且以下必须**全部匹配**：

- 重算的源媒体内容 checksum
- 原 `burned_video` checksum
- 当前 fingerprint
- 媒体 QA
- 候选文件 SHA-256

重复 `burned_video` 一律 fail closed。

### 不具备资格的情形

legacy proof、媒体 QA、proof 或 fingerprint 无效的 Job 都不具备重新关联资格，按 `retryable` 恢复并由 `continue` 重烧。旧 Job 必须先 `continue` 重烧一次生成 v2 proof，不得宣称可直接 relink。

### status 的成本边界

普通 `status` 不会为 v2 proof 额外计算源媒体内容 checksum；内容 checksum 只在烧录落证和 relink 校验时计算。

`xvt status JOB` 对缺失的已登记烧录成品只做静态资格判断，不额外计算源媒体内容 checksum；符合资格时归类为 `awaiting_input`，优先给出 `relink-burned-output`，并列出显式 `continue` 重烧作为备选。
