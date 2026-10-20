# 恢复与回填契约

## YouTube 视频下载画质与恢复

新获取的 Cookie YouTube 完整视频比较配置与匿名访问的原音轨和分辨率，固定格式 ID 下载并校验实际媒体；匿名查询错误、获选路线或匿名路线的缺格式警告、视频 ID/时长不一致、实际尺寸或音轨缺失均拒绝成功登记。Cookie 路线缺格式但匿名路线完整且胜出时可使用匿名结果；不设 1080p 上限，不以目标字幕语言选择音轨。

下载使用本次独立临时目录，不复用旧同名文件，发布时拒绝覆盖现有目标（含并发创建）。可选 `media_download_selection` 摘要记录已验证媒体 checksum；摘要缺失、损坏或失效不会触发重下载。旧 Job 的 checksum 有效媒体继续复用，`status` 不新增网络访问。已有低清 Job 需要用户提供高清文件并用 `burn --media` 显式重烧，不更换源 cue 或译文时间轴。

## 人工译文预检与布局确认

已知画幅但测量失败时仍保存请求画幅、字体样式及 `measurement=unavailable`；成功时为 `measured` 并保存宽度。回退字数规则不会丢失确认上下文，因此另一画幅或字体的旧确认也不能复用。

`xvt apply JOB REVIEW --check` 和 `xvt apply-translation-review-fixes JOB REVIEW --check` 仅检查人工译文 JSON/SRT，不写 Job、artifact、事件日志或锁文件，不调用模型。返回 `translation_review_preflight`，包括 `valid`、`changed_ids`、`blockers` 和完整 `qa`；无效退出 2。统一入口仅允许译文修订路由使用该旗标，拒绝与 `--continue` 合用。正式提交重新执行相同检查，译文超宽或 error 未解决时不保存、不失效旧产物。

已知画幅时，候选及最终译文 QA 按实际 ASS 样式测量可见宽度；建议字数超限但能容纳的降为 warning。测量失败保留原阻断规则。报告可包含 `layout`（画幅、样式、宽度和可用宽度），subtitle acknowledgment 绑定相同布局；旧无 layout 的确认仅匹配旧无 layout 的报告。确认同时绑定被确认那份报告的 checksum（`report_kind` + `report_checksum`），译文变化后报告 checksum 随之变化、旧确认自动失效；QA 报告是确定性的，`continue` 零成本重算出的同内容报告逐字节相同，不会误伤「确认 → continue」。换画幅、字体或测量结果后需重新判断，不能用旧确认放行新布局。恢复/status 仍严格只读，旧候选报告在实际推进时重算；烧录重查实际媒体与字体，新的 review 返回 workflow 等待态，不宣告完成。预览不改写正式 QA。

本文是 `status` / `continue` / `batch` / `batch-continue` / `xvt apply` / `relink-burned-output` / `relink-source-media` 共用恢复语义的单一来源。判定顺序、产物有效性口径、回填路由和烧录 proof 资格都以这里为准，其它文档不得另立说法。

原子 `burn` 与 `preview` 对所选媒体、字体执行只读布局 QA，不覆盖正式 `translated_subtitle_qa` 或更新字幕 QA stage；布局检查失败时在渲染与烧录前拒绝。本地替代媒体或字体导致**布局失败**时 Job 不变（这条路径抛 `UserInputError`，`burn_translated_job` 的兜底刻意不把它记成 stage 失败）；远程媒体若在检查前下载，仍按原契约登记并持久化。临时媒体过短、画幅过窄或字体过宽不会污染原工作流的 QA，随后仍可按原目标 `continue`。只有 `run` / `continue` 按持久化目标执行烧录时，才保存布局报告并更新字幕 QA stage；新的 review 进入等待，error 仍失败。

**「Job 不变」只覆盖布局 QA 这一类失败，不是「原子命令不写 stage」。** 原子 `burn` 成功时就会推进 `render` / `burn` / `media_qa` 三个 stage，因此布局 QA 之外的失败同样按 stage 记账：临时 `--media` 不存在、没有视频流，或 ffprobe 探测损坏文件失败时，`stages.render` 会被记成 `failed` 并写入错误文本（退出码 3 属 `dependency_or_process`）。这只影响 stage 展示，不影响恢复判定——产物齐全时 `status` 仍给出 `recovery: complete`、`verify` 全 PASS，下一次成功的 `burn` / `continue` 会把 stage 推回 `succeeded`。如实记录而不是承诺更强的口径。

模块职责、artifact 依赖图与失效闭包见 [架构说明](ARCHITECTURE.md)；事件名与字段见 [事件说明](EVENTS.md)。

## 远程字幕获取的访问方式

显式请求 YouTube 字幕并传入浏览器 Cookie 时，缺少匹配人工字幕会触发一次匿名补查。发现同一视频的匹配人工字幕后，`remote_info` 以 `subtitles_anonymous=true` 保存并重新登记 checksum；后续字幕下载匿名执行、忽略 yt-dlp 本机配置，显式代理仍保留，媒体访问参数不变。字幕下载中断后，重新进入 acquire 会从有效 `remote_info` 恢复匿名访问方式；旧元信息缺少该字段时默认使用原访问参数。补查失败会使 inspect 失败，必须显式重试，不得当成字幕缺失继续 ASR。已生成的源 cue、译文和烧录产物不会因补查规则更新而自动失效或替换。

有效的已下载字幕、已复制用户字幕或显式用户源字幕配置会阻止补查：保留候选的原来源分类，不用新查到的人工字幕语言列表把旧自动字幕重标为人工字幕。

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

**通则：在途等待优先于「已满足」。** 任何「某个产物/目标已满足」的判定都不得盖掉一次仍然挂得住的在途 Agent 等待。等待步骤与这道闸门的相对位置必须显式表态：要么写在 `_translation_satisfied` 判定**之前**早退，要么登记进 `workflow._SATISFIED_GATED_AWAIT_STEPS` 并写明理由（`tests/test_workflow.py::AwaitStepsCoverageTest` 用 AST 强制，两边对不上就红）。

1. `plan_advance_steps` 在**函数体最前**判定局部重译等待（`await_cue_retranslation`）。理由：它会叠加在一个译文目标已满足的 Job 上，放在后面就会被「已满足」短路掉。
2. 紧接着、仍在 `_translation_satisfied` 之前，判定**整轮 Agent 翻译等待**：`agent_full_translation_waiting`（`TRANSLATE` 停在 `awaiting_input` 且 `translation_batches` checksum 有效）为真，且整轮 manifest 是**有效**取代 kind 里最后登记的那份在途请求（公有单点 `workflow.full_translation_request_is_newest`，与 `cue_retranslation_batches` / `translation_polish_batches` / 三份语义报告按登记次序比较，但**只有 checksum 有效的取代 kind 参与比较**，登记仍在、文件已失效的取代 kind 一律忽略）时，早退为 `await_agent_translation`。理由与第 1 条完全同构：`await_agent_translation` 的其余 return 分支都埋在 `not _translation_satisfied` 里，旧译文仍有效时根本不会被求值，于是「按 `status` 的提示放弃重译、改跑一轮完整翻译」之后 `status` 报 complete、`continue` 报完成、`xvt apply` 又说 `not awaiting Agent input`，导航闭环断掉。登记次序这一条不能省：润色 manifest 与语义报告在译文被改写时不会被失效闭包带走，残留文件照样 checksum 有效，只看前两个条件会把它们的等待盖成整轮等待；反过来，用户在整轮等待之后又显式 `retranslate-cues` 时，重译请求更新，第 1 条照旧优先。只认**有效**取代 kind 的理由同样成立：所有取代类等待（重译 / 润色 / 语义）的等待谓词都要求自身 artifact 有效，失效的取代登记背后不可能挂着活等待；若让它参与比较，「整轮等待 → 局部重译 → 重译 manifest 被删（登记仍在）」会让 `plan_advance_steps` 返回空、`status` 报 complete、`continue` 发 `workflow_completed`、统一 `xvt apply` 拒收，而 `TRANSLATE` 仍停在 `awaiting_input`、第二轮 manifest 完好可回填（2026-10-20 实测）。有效性只认内容 checksum，登记次序只用来在有效的在途请求之间定先后。
3. 在 `TRANSLATE` 等输入且 `translation_batches` checksum 有效时，译文未满足的分支里同样早退为 `await_agent_translation`（与第 2 条共用单点谓词 `agent_full_translation_waiting`）。这一早退**必须排在 `translated_cues` → `render` 之前**：新整轮请求不会失效旧译文，按 render 先判会把等待误报成「只差渲染」。
4. `TRANSLATE` 等输入但 manifest 已 checksum 失效、且没有有效 `translated_cues` 时落到最后的兜底分支，给出 `translate`（导入译文 Job 给出 `import_translation`），**不报等待**。没有 manifest 就没有任何响应能回填这次等待，报等待会让 `status` 说等回填、`xvt apply` 被 manifest 校验拒绝、`continue` 只发一次 waiting，三方互指没有出口。`_ensure_translation` 的等待分支复述同一个条件，manifest 失效时落到 `run_translation` 重建批次；recovery 的原子分支对同一状态早已是 `retryable` + `translate`，三侧由此一致。

### 已知遗留：满足度之后才判的三类等待

`await_agent_polish` / `await_agent_semantic_review` / `await_translation_review` 刻意只在「译文目标未满足」的分支里判定（登记在 `workflow._SATISFIED_GATED_AWAIT_STEPS`）。理由：它们的产物（polished cues、语义报告与人工确认）本身就是 polished 质量下译文满足度的组成部分，正常流程里「已满足」与「自己那次等待仍挂着」互斥。

遗留情形：用户在一个**已满足**的 polished Job 上显式重开 `polish-translation` 或语义校对的 Agent 等待时，计划仍会是空的（`status` 报 complete）。它不是本轮修复的路径，也没有已知的 E2E 复现；真要修就按第 2 条同样补一条满足度之前的早退，并同步更新上面那张表态表。

相邻但**不同**的一种情形已经有出口：Agent `task=translate_and_polish` 的一次成型 Job 结构上就没有 `translation-draft-cues.json`，`polish-translation` 对它根本开不起来——`pipeline.polish._prepare_draft` 复用 `agent_translate_and_polish_started` 识别这类 Job，以退出码 2 明确拒绝并说明「润色在初译那一步就完成了」，不再和「当前目标已完成」的恢复建议拼成一句自相矛盾的话。「draft 曾存在但 checksum 失效」仍走原来那条「需要恢复原始译文草稿」的引导，两种情形不共用文案。

### `repair_polish_draft` 的恢复出口

`repair_polish_draft` 先问 `translation_progress_rebuildable`（status 与 continue 共用的单点谓词）：checkpoint 覆盖全部批次且 fingerprint 一致时归 `retryable`，首选 `continue`，零 API 成本重建草稿。不可零成本重建时归 `manual_fix`，诊断文案仍是 `REPAIR_POLISH_DRAFT_MANUAL_FIX_MESSAGE`（与 `continue` 抛出的那句逐字相同），其后按 Job 里**有没有 checksum 有效的译文字幕**分成两种导航：

- **有** checksum 有效的 `translated_srt`（且目标语言已持久化）：首选命令是 `xvt import-translation JOB <那条字幕的确切路径> --target-language LANG`。诊断文案自己说的「重新导入原始译文草稿」就是它——polished Job 上 `import-translation` 经 `finalize_translation` 的 draft 分支，把回填结果写成 `translation-draft-cues.json` 并重新登记 `translation_draft_cues`，计划随即从 `repair_polish_draft` 回到 `polish`。代价必须在 `reason` 里讲明：这份字幕会被登记为导入译文，现有译文与语义校对结论一并失效，润色腿要重跑（openai-compatible 模式下是真实 API 成本）。
- **没有**：保持 `primary_command=None`，但 `reason` 点名那个缺失或内容已变化的草稿文件的绝对路径。此前这里只有一句 `hint: re-check with xvt status ... after repairs`，既没有命令也没有路径，对用户是纯死路。

两种情形的 `alternative_commands` 都是 `xvt translate JOB --target-language LANG --provider <持久化 provider>`（整轮重译 + 重润色，成本更高）。`continue`（在这个状态下退出码 4）与 `polish-translation`（退出码 2）**不放进任何推荐位**。

只认 checksum 有效的 artifact 是刻意的（约束 9）：`pipeline.polish._reset_for_polish` 只是把下游产物**移出登记**，`artifacts/translated.srt` 之类的物理文件往往还在原地，但没有登记就没有可比对的基线，照「通常还在」推荐等于再给一条可能指向不存在文件的命令。kind 选 `translated_srt` 而不是 `bilingual_srt`：前者是纯译文、cue ID 与时间轴和 `source_cues` 一一对应，后者掺着原文，导入回去会污染译文文本。

### 一次成型 `translate_and_polish` 不留草稿记录

`complete_agent_polished_outputs`（一次成型路径的唯一收尾点）在收尾时把 `translation_draft_cues` 登记移出 `job.artifacts`。这条路径结构上从不写 `translation-draft-cues.json`：初译与润色在同一次回填里完成，`translation_polished_cues` 派生自 `source_cues` 与那份响应。此前某一轮 Q5a 润色留下的草稿登记会一路挂到 `completed`，于是 `status` 报 `complete` 并推荐 `xvt verify`，而 `verify` 必然以 `artifact file is missing` 失败——「Job、artifact、checksum 能解释当前状态」不成立，推荐的也是一条注定失败的命令。

摘记录沿用既有的「按 kind 过滤 `job.artifacts`」失效机制，只摘这一条，**不**走 `downstream_of(TRANSLATION_DRAFT_CUES)`（那个闭包含刚刚产出的 `translation_polished_cues`）。不影响恢复依据：planner 的 polished 腿对一次成型 Job 只看 manifest 与响应（`agent_translate_and_polish_core_satisfied` / `agent_translate_and_polish_started`），`polish-translation` 本来就以 `AGENT_ONE_SHOT_POLISH_RERUN_MESSAGE` 拒绝这类 Job，`translation_progress` 等 checkpoint 记录原样保留；物理文件不删，只是不再登记为本 Job 的产物。

### 宣告完成前的终检守卫

`_advance_workflow` 在发 `workflow_completed` **之前**重新跑一次 `plan_advance_steps`，不为空就抛 `InternalError`（`category="workflow"`，退出码 4），消息列出剩余步骤。执行侧与 planner 对同一份 Job 状态给出相反结论属于内部不一致：`status` 会说还差一步而 `continue` 说已完成，`batch-continue` 更会把条目记成完成，用户不会再回来看它。守卫刻意 fail closed，不降级成警告。

这条守卫同时是「`completed` 必须对应一个真的完成了的 Job」的常驻回归网：测试里打桩只允许替掉真实的网络、模型与 FFmpeg 调用，产物落盘、artifact 登记与 stage 推进必须照真实运行来，否则守卫会在断言 `completed` 之前先抛错。

### 翻译策略与批次大小的回退

`xvt translate` / `xvt prepare-translation` 在端到端 Job 上未显式传入 `--glossary` / `--translation-style` / `--batch-size` 时，**逐字段**回退 `config.workflow` 的 `translation_policy` 与 `batch_size`（与 `base_url` / `model` 同口径，单点实现 `pipeline.translation._resolved_translation_inputs`）。不回退会让这两条命令产出不带术语的译文，而译文 QA 仍按 workflow 快照判 `glossary_violation`——术语死锁没有出口；策略与 `batch_size` 都进翻译指纹，漂移还会清零批次进度、全部批次重付一次翻译费。逐字段而不是整份替换：只给 `--glossary` 时风格仍沿用快照，只给 `--translation-style` 时术语表仍沿用快照。调用方直接传 `policy=`（workflow 执行路径）时原样采用。原子 Job（无 `config.workflow`）保持历史行为：顶层 `translation_policy` 由本次调用重写，翻译与 QA 用的是同一份策略，不存在同款死锁。

### 原子 Job 的 qa_blocked 让路

`SUBTITLE_QA` 停在 `failed` / `awaiting_input` 只说明上一次译文 QA 没过，它不会因为随后开了一次新的 Agent 等待而复位。端到端 Job 先问 planner（首步是正式等待步骤就改走工作流恢复分支）；原子 Job 没有 planner，改用 planner 内部的同一份谓词 `workflow.agent_full_translation_waiting`（`TRANSLATE` 停在 `awaiting_input` 且 `translation_batches` checksum 有效）跳过 `qa_blocked`，由原子等待分支给出 `apply-translation` 引导。否则术语死锁后改跑 `translate --provider agent` 的用户会继续拿到 `retranslate-cues`，照做会盖掉刚写入的整轮 manifest。manifest 已经不在的等待态残留不放行，QA 引导照旧生效。

### polished 原子 Job 的「润色候选有效」让路

原子分支内部还有一条同判据的让路，位置在「润色候选有效，可以重新执行原子翻译命令继续语义校对」那条 `retryable` 上。它排在既有的原子等待分支**之前**，而两者在同一个状态上同时成立：`prepare_agent_translation` 只把 `TRANSLATE` 置为 `awaiting_input`，**不失效** `translation_polished_cues`，所以「润色候选仍有效」与「整轮 Agent 翻译正在等回填」可以叠在一起（典型路径：polished 原子 Job 回填过一轮、语义报告缺失或失效，用户又开了一轮整轮 `translate --provider agent`）。

不让路时 `status` 推荐的是 `xvt translate JOB --target-language LANG --translation-quality polished`，而它在这个状态下**不会**继续语义校对：Agent 模式下 `run_translation` 走到 `prepare_agent_translation` 就返回，`_cmd_translate` 后面的润色与语义校对腿根本不执行。后果有两层：

- 命令零推进。manifest 被原样重写一遍（内容逐字相同），`status` 的分类与推荐逐轮不变，形成一个没有出口的循环；真正能推进的 `apply-translation` / `xvt apply` 从头到尾不出现在导航里。
- 用户当初显式传过 `--batch-size`（或 `--glossary`）时更糟。原子 Job 的策略与 batch size **不**回退 Job 内的快照（见上文「翻译策略与批次大小的回退」），重写会把 manifest 的 `batch_size` 与 `fingerprint` 一起改掉，手里那份已经准备好的响应随即被 `apply-translation` 的指纹校验拒收（`translation response fingerprint does not match the current manifest`），一次本可回填的等待变成要重新回填。

因此该分支额外要求 `not workflow.agent_full_translation_waiting(job)`——与上面 `qa_blocked` 让路**同一份谓词**，不另造条件。让路后由原子等待分支给出 `apply-translation` 引导；`TRANSLATE` 不在等待态、或 manifest 已经不在的等待态残留，本分支照旧生效（后者不让路正是因为那种状态下没有任何响应能回填）。

**相邻缺口（已修）**：原子分支的两条语义等待（`agent_semantic_review_waiting` / `semantic_review_waiting`）排在更前面，此前不判「整轮请求是不是最新的那一份」，于是语义等待态下再开一轮整轮 `translate` 时 `status` 仍指向已被取代的 `apply-semantic-review`（照做会静默作废用户的新一轮并留下残留死循环）。现在这两条分支前计算 `agent_full_translation_waiting(job) and full_translation_request_is_newest(job)`（与 `plan_advance_steps` 满足度早退闸门同一份组合判定），为真时让路给原子等待分支的 `apply-translation` 引导。**必须带 newest 条件**：正常语义等待（初译已回填、停在 Agent 语义校对）上 `agent_full_translation_waiting` 恒为真（manifest 仍有效、`TRANSLATE` 仍 `awaiting_input`），裸谓词让路会推荐 `apply-translation` 重放初译后回到一模一样的状态（零推进死循环）。上面「润色候选有效」那道闸门则**保持裸谓词**：语义报告失效的形态里登记次序上 `semantic_qa` 仍在 `translation_batches` 之后，给它加 newest 条件会倒退。

### 强制对齐置信度不足的死循环出口

`--align-subtitles` 与 `--allow-low-confidence-alignment` 都固化在 `job.config` 里，置信度门不过时 `ALIGN` 记 `failed`、`source_cues` 缺失，但转写 checkpoint 全在。此前这种 Job 落到「存在 checksum 有效的转写 checkpoint，可以从断点继续」，首选命令是 `continue`，`alternative_commands` 为空——而 `continue` 拿的是同一份 `input_subtitles` 与同一份 `word_transcript`，对齐是确定性的，每次都复现同一个退出码 2，用户没有任何出口。

恢复计划现在在这个状态下归 `manual_fix`，首选命令是 `xvt rebuild-source JOB --strategy asr`（它把 `align_subtitles` 置为 `false` 并优先复用 checksum 有效的 words/audio checkpoint，是真正能推进的出口；另一条出口是重新运行 `run` 调整对齐选项）。判定只看 Job 状态，不猜异常：媒体 Job、`align_subtitles is True`、未放行低置信、`subtitle_alignment` checksum 有效且其 `confidence` 低于 `minimum_alignment_confidence`，并且 **`word_transcript` 与 `input_subtitles` 两份对齐输入都 checksum 有效**。缺任一输入时重跑可能得到不同置信度，不能断言「必然失败」，那种 Job 仍按既有的转写 checkpoint 恢复处理。批处理侧 `manual_fix` 记为 `failed` 并保留恢复指令，不再反复重试一条注定失败的 `continue`。

### 强制对齐被对齐后 QA 拒绝的死循环出口

`--align-subtitles` 的第二种失败：对齐本身置信度达标，但**对齐后的字幕 QA 出 error 级问题**，强制对齐被那道门拒绝（报错点名「rerun without `--align-subtitles`」）。形态与上一节相同——`ALIGN` 记 `failed`、`source_cues` 缺失、转写 checkpoint 全在，`continue` 拿同一份 `input_subtitles` 与同一份 `word_transcript` 重跑，对齐与对齐后 QA 都是确定性的，实测连跑两次 stderr 逐字相同、退出码都是 2。此前它同样落到「存在 checksum 有效的转写 checkpoint，可以从断点继续」+ `primary_command=xvt continue`、`alternative_commands` 为空（本仓库第 9 次同类缺陷）。

恢复计划现在归 `manual_fix`，首选命令是 `xvt rebuild-source JOB --strategy asr`（实测退出码 0，之后 Job 能被驱动到 `completed`、`xvt verify` 全项 PASS）。单点实现是只读谓词 `recovery._alignment_post_qa_deadlock`，成立时返回那份报告的路径，守卫链：

- 不是纯字幕 Job（纯字幕 Job 不支持 ASR 重建，也不支持对齐）。
- `align_subtitles is True`。**自动策略**下对齐后 QA 失败不是死局：那一支会回落 ASR 并正常产出 `source_cues`（`source-selection.json` 记 `alignment_post_qa_failed`），不能被判死。
- `aligned_subtitle_qa` checksum 有效，且报告里有 error 级 issue。判据直接取自这份报告（`_qa_report_has_errors`，与译文 QA 侧同一份实现），而不是 `subtitle_alignment`：挡住对齐的就是报告里的 error 级 issue。这份报告自「对齐后 QA 报告落盘」那次改动起在抛出之前无条件登记，所以失败的 Job 上一定有。
- **`word_transcript` 与 `input_subtitles` 两份对齐输入都 checksum 有效**，与置信度门同一条守卫：缺任一输入时重跑会重新转写或重新导入，结论可能不同，不能断言「必然失败」。
- **`_source_can_be_rebuilt`**——比置信度门**多的一道**。本条的唯一出口就是 `rebuild-source --strategy asr`，而 `media_info` 失效且本地媒体也不在盘上时那条命令自己会以「source rebuild requires an available local media file」失败；推荐一条跑不起来的命令正是这条判定要修的毛病，所以不留这个角。不成立时让既有分类原样生效，那种 Job 的去向与本改动之前完全相同。刻意**不**回头给置信度门补这道守卫：那会改动一条既有判定，不在本条的范围内。

**与置信度门的关系**：两道门可以在同一个 Job 上同时成立（置信度低且对齐后 QA 也有 error），而执行侧先抛置信度门，所以本判定**排在 `_alignment_confidence_deadlock` 之后**，文案对得上先失败的那一条。两条谓词各自独立、互不覆写；测试 `AlignmentPostQaDeadlockTest::test_confidence_gate_keeps_its_own_reason_when_both_gates_trip` 钉住这个顺序。

**刻意不照抄的一条守卫**：`--allow-low-confidence-alignment` 与本条**无关**，本判定不看它。那个旗标只放行「低置信但 QA 干净」的对齐，对齐后 QA 的 error 门在它之后无条件执行（`pipeline.transcription`），照抄会让带旗标的 Job 继续卡在必然失败的 `continue` 上。

`report_paths` 带上 `aligned-subtitle-qa.json` 的绝对路径，`reason` 点名这个文件名。该字段本来就不是 `SUBTITLE_QA` stage 专属（局部重译等待带 `cue-retranslation-batches.json`、语义等待带语义 QA 报告，都在 `TRANSLATE` stage），这里属于同一种用法；`xvt status` 与命令失败时的恢复导航都按 `verbose` 渲染它（`report: <路径>`），用户不必再拿 `alignment.json` 去跟 `media.json` 的时长对账。

**没有第二条命令可推荐**，`alternative_commands` 刻意为空，理由都实测过：

- 报错点的「rerun without `--align-subtitles`」与「`--source-strategy asr`」都是 `run` 的旗标，**会创建新 Job**，推不动这一个；`align_subtitles` 固化在 `job.config` 里，除 `rebuild-source` 外没有命令会把它翻成 `false`。前者保留的是用户字幕的**文本与原时间轴**，与 `rebuild-source`（改用 ASR 文本与 ASR 时间轴）并不等价，因此只在 `reason` 里点名，不进推荐位。
- 「fix the source subtitles」在既有 Job 上**没有受支持的入口**。`prepare-source-review` / `apply-source-review-fixes` 不适用（实测以 `source review requires valid source cues` 退出码 2 拒绝——这个 Job 的源 cue 还没产出），也没有「重指源字幕」的命令（`relink-source-media` 只管源媒体）。改用户原始字幕文件本身也不起作用：`_candidate_from_job` 优先取 Job 内 checksum 有效的 `input_subtitles` 副本，实测改原文件后 `continue` 仍复现同一个失败。这同样只在 `reason` 里以「修好源字幕后重新运行 `run`」出现。
- `continue` 必然再次失败，**任何推荐位都不放**。

批处理侧 `manual_fix` 记为 `failed` 并保留恢复指令，`batch-continue` 不再每轮重撞同一个失败。**约束 4 不受影响**：只改导航分类与文案，QA 的放行与阻断判定、错误文案与退出码一字未改，强制对齐失败仍然失败。

### 已存译文套不回重建后的源时间轴

端到端 Job 上「补齐译文」这一步不一定调用翻译提供方：`workflow._ensure_translation` 会优先把 Job 内保留的 `input_translation_subtitles` 副本（退而求其次是 `config.workflow.translation_subtitles` 指向的用户原始文件）重新套到当前 `source_cues` 上。`rebuild-source --strategy asr` 换掉源时间轴之后，这份已存译文可能根本套不回去：`map_subtitle_translations` 原地失败（退出码 2），而失败发生在 `set_stage(RUNNING)` **之前**，Job 一个字节都不改。此前恢复计划照旧给 `retryable` + `primary_command=xvt continue JOB`、`alternative_commands` 为空——`continue` 失败后自荐的还是刚失败的那一条，导航零变化，用户没有出口（本仓库第 7 次同类缺陷）。

`recovery` 严格只读、不感知运行期异常，所以判定必须从 artifact + config 纯推导，和上一节的对齐置信度死局同一范式。单点实现是 `pipeline.common.pending_translation_import_blocker(job, config)`：**只读纯谓词**，不写盘、不登记 artifact、不改 stage，重叠度量直接复用 `translate.map_subtitle_translations`（连同它的 `require_timeline_overlap` 语义与 `_timeline_overlap_ratio` 阈值），不在恢复侧另造一套。`status`（`recovery._recovery_for_workflow` 的 `translate` / `import_translation` 分支）、`continue`（`_ensure_translation` 回填前的闸门）与 `rebuild-source` 收尾三处消费同一份实现与同一句诊断。

出声的条件（任一不成立就返回 `None`，让既有分类原样生效）：

- 持久化目标有 `target_language`，且质量档**不是** `polished`——polished 腿的 `_ensure_translation` 永远在自己的块里返回，draft 失效时直接 `run_translation`，从不重新套用已存译文，对它出声会把一次真能推进的 `continue` 判死。
- 取到一份待回填的译文：`input_translation_subtitles` checksum 有效时用它，否则用 `config.workflow.translation_subtitles`（取值顺序与 `_ensure_translation` 逐字一致）。两者都没有时这一步会真的去调用提供方，不归本谓词管。
- `source_cues` checksum 有效且非空（约束 9：产物有效性只认内容 checksum）。源 cue 本身不可信时断言「译文套不上」没有依据。
- 用上面两份输入真跑一次映射并失败。

诊断分两种成因，给用户的动作不同，文案因此分开：**那份文件读不出来**（用户原始文件被删或改坏，只可能出现在 `config` 那一支——artifact 那一支的 checksum 已经证明文件在）与**时间轴对不上**。两者都点名那份字幕的绝对路径，后者还带上 `map_subtitle_translations` 的实测细节。

分类是 `manual_fix` 而不是 `awaiting_input`：这不是「等一份可回填的输入」，而且 `batch` 把 `manual_fix` 归入「绝不自动推进」并记 `failed`，`batch-continue` 因此不再每轮重撞同一个失败（此前这类条目是 `retryable`）。

三条出口都实测走得通，但不等价（顺序即推荐位顺序：首选 + 两条 `alternative_commands`）：

- **首选 `xvt import-translation JOB TRANSLATED_SUBTITLE --target-language LANG`。** 导入一份对齐当前源 cue 的译文后 Job 走到 `completed`、`verify` 全项 PASS；而且 `add_artifact` 按 kind 去重，这一次导入会**顶掉**那份错位的 `input_translation_subtitles` 登记——死局本身被拆掉。字幕路径是占位符：哪一份译文对齐了新时间轴只有用户知道，导航不猜（与 `relink-source-media` 的 `NEW_MEDIA_PATH` 同款写法）。
- **备选一 `xvt drop-imported-translation JOB`。** 不打算再用这份译文时的诚实出口：两处来源一起清、带完整失效闭包，随后 `continue` 走常规重新翻译。它不需要目标语言，因此在 `alternative_commands` 里不受 `target_language` 守卫约束。详见下一节。
- **备选二 `xvt translate JOB --target-language LANG --provider <持久化 provider>`。** 同样能走到 `completed`，但它**只是绕过**：错位的 `input_translation_subtitles` 与 `config.workflow.translation_subtitles` 原样留着，再跑一次 `rebuild-source` 会在同一处复发（上面那条命令存在的理由）。`reason` 里必须讲明这一点（openai-compatible 模式下它还是真实 API 成本）。

`continue` 在这个状态下必然再次失败，**任何推荐位都不放**。`xvt resegment-translation` 也不是出口：它换的是源 cue 的切分，解决不了「这份译文对不上」；持久化了 `workflow.translation_subtitles` 的 Job 上它还有一道配置级闸门（`resegment.py`），恒以退出码 2 拒绝。

`continue` 抛出的异常类型刻意保持 `ValueError`（今天这条路径就是 `map_subtitle_translations` 的 `ValueError` 一路冒到 CLI，退出码 2），提前失败只是把同一句话说得更清楚，不顺手改调用方看得见的异常契约。`rebuild-source` 收尾同样复用 `build_recovery_plan` 的命令，不在调用点自己拼：重建完成后当场告诉用户这条路已经断了，别让他们再跑一次注定失败的 `continue` 才发现。

### 第三条出口：显式放弃这份导入译文

`xvt drop-imported-translation JOB`（advanced）是「不再用这份译文」的出口，排在 `alternative_commands` 的**第一位**（整轮重译退到第二位）。它不新增能力，是把一条既有的**隐式旁路**诚实化：今天直接跑原子 `xvt translate` 也能走到 `completed`——那条命令不看导入分支、直接改写 `translated_cues`，之后 `_ensure_translation` 在导入分支**之前**就按译文有效满足返回——但错位的 `input_translation_subtitles` 与 `config.workflow.translation_subtitles` 原样留着，实测再跑一次 `rebuild-source` 原地复发同一个死循环，而且 Job 的持久化目标仍写着「用导入译文」，解释不了自己的产物从哪来。

两条纪律，缺一条这条命令就是空话：

- **双源同时清。** 只摘 artifact 登记不行：`_ensure_translation` 的 `retained_translation or config.translation_subtitles` 会从持久化目标把它捡回来。只清 config 也不行：`plan_advance_steps` 会改口说 `translate`（那一步只看 `config.translation_subtitles`），而执行侧仍旧重新套用 Job 内那份副本。
- **必须带失效闭包。** 健康导入 Job 上只摘登记的话，`translated_cues` 仍然 checksum 有效，`_ensure_translation` 更早的一道早退直接判满足，产物**仍然来自被放弃的译文**——drop 等于没做。照 `resegment` 的纪律：`downstream_of(INPUT_TRANSLATION_SUBTITLES)`（21 个 kind）+ 这条登记自身 + stage 复位（`translate` / `subtitle_qa` / `render` / `burn` / `media_qa`）+ `clear_burn_fingerprint`。

刻意**不**并入 `translation_candidate_qa` 与 `translation_review_revision`（`resegment` 把它们显式并入）：那里的理由是重分段丢弃了 cue id、报告永久作废，而这里源 cue 一个字节都没动。两者都以内容 checksum 绑定在被放弃的那份候选上（`candidate_qa.translation_candidate_qa_issues` 比对 `candidate_cues_checksum`，`semantic_state.human_revision_scope` 先要求 `translation_reviewed_cues` 仍是当前候选），候选失效后它们自行判否；读它们的两条恢复分支又都在 `await_translation_review` 之内，而那个等待步骤要求候选有效。候选 QA 报告会在下一次候选产生时按原路径重写。

其余刻意边界：

- **保留文件，不删字节。** 导入译文是 `shutil.copy2` 进 Job 的副本（`artifacts/input-translation.srt`），摘登记不丢数据；用户原始文件更不碰。留痕按约束 7 走两层：每个被失效的 artifact 一条 `artifact_invalidated`，外加一条 `translation_import_dropped` 记下 kind、被放弃副本的路径与内容 checksum、被清掉的 `translation_subtitles` 与失效 kind 清单。路径进事件不违反约束 5（禁的是凭据与内容，路径不是），文本仍走 `create_event` 的统一脱敏。
- **两处来源都没有时以退出码 2 拒绝**，不静默成功：这条命令的全部作用就是移除某样东西，没得移时报成功会让用户以为自己改对了 Job。代价如实记：它因此**不是幂等的**，连跑两次第二次失败。
- **原子 Job（无 `config.workflow`）支持**，那里只有 artifact 一个来源，之后用 `xvt translate` 重新翻译。drop 之后 `revision.source_review_supported`（**双源**：config 或 artifact）的「正在复用导入译文」闸门会从拒绝**翻转成放行**——这是正确的（Job 确实不再复用任何导入译文），语义由 `tests/test_drop_imported_translation.py` 钉住。`resegment` 的那道闸门**不是双源，是 config-only**（详见下节），原子导入 Job 本来就不被它拦住；drop 之后重分段仍会以「缺 `translated_cues`」拒绝，只是拒绝的理由换成了真正的那一个。

### 重分段闸门为什么只看 config

`pipeline/resegment.py` 的导入译文闸门只问 `workflow.translation_subtitles`，不问 `input_translation_subtitles` 这条 artifact 登记；`pipeline/revision.py` 的 `source_review_supported` 两处都问。**这不是遗漏，两道闸门防的不是同一件事**，2026-10-18 用真实命令逐条实测确认：

- **`revision` 防的是「用户的原文↔译文配对」被改坏。** 修订源 cue 文本之后那份导入译文再也对不上，而它是用户亲手交的、系统无法重新生成——只要这份译文还在被复用，无论来自 config 还是 artifact，都不能动源文。双源是对的。
- **`resegment` 防的是「自动重新导入」的复发循环。** 持久化了 `workflow.translation_subtitles` 的 Job 上，重分段换掉时间轴后 `continue` 会把同一份译文再套一遍、再次超宽、再次要求重分段。原子导入 Job 没有那条持久化目标：重分段按失效闭包把 `input_translation_subtitles` 连同全部下游一起移出登记（文件留在盘上），Job 干净地回到 `translate`，没有东西会把旧译文捡回来。

实测对照（同一支素材、同一份译文字幕）：端到端导入 Job（`run --translation-subtitles`）上 `xvt resegment-translation` 以退出码 2 拒绝；原子导入 Job（`run --target-language` → `import-translation`）上它跑完并退出 0，6 条新 `asr_resegmented` 源 cue 落盘，`status` 给 `retryable` / `steps: translate`。

**刻意不把 `resegment` 收紧成双源。** 那会新增一条**没有出口**的拒绝：重分段要靠 `translated_cues` 量超宽，而唯一的「放弃导入译文」命令 `xvt drop-imported-translation` 会连 `translated_cues` 一起失效——实测 drop 之后 `resegment-translation` 立刻以「缺 `translated_cues`」退出码 2 失败。被收紧的用户只剩「先花钱整轮重译一遍，才被允许重分段」这一条路，而他手里本来就有一份能用来量宽度的译文。保持现状。
- **放弃不等于完成。** 命令收尾复用 `build_recovery_plan` 打印下一步（不在调用点自己拼命令）：`--provider auto` 且当前终端没有 API 配置时，随后的 `continue` 会落到 Agent 模式停在 `awaiting_input`，输出必须让用户看出「下一步是重新翻译」而不是「已经好了」。
- **覆盖保护集随之收缩。** `paths.py` 的 `_user_input_paths` 把 `config.workflow.translation_subtitles` 算作受保护的用户原始输入；清掉它之后那份文件退出保护集，Job 内那份副本也因为不再登记而退出 artifact 保护。这是正确的——Job 确实不再把它们当输入——但如实记下来：此后 `burn` / `preview` 的 `--output` 指向那两个路径不会再被拦住。`job.input` 与 `config["subtitles"]` 的保护不受影响。

## 翻译提供方身份的固化

### 固化时机

- 端到端 Job 的 `config.workflow.base_url` / `model` 允许在 `run` 时为 null，由环境变量补齐。两者都进入 API 翻译进度指纹，因此 `continue` 若在另一个环境变量不同的终端里执行，指纹会漂移、批次进度清零，还可能拿过期模型名去撞 4xx。
- openai-compatible 模式在**首个翻译批次成功后**（或全部批次都命中 checkpoint、指纹按这套身份校验通过时）把实际生效的 `base_url` / `model` 写回 `config.workflow`，与该批次的 `save_job` 一并落盘。此后 `continue` / `status` 读到非 null 值，解析提供方与指纹都不再受进程环境影响。
- `xvt translate` 在端到端 Job 上不传 `--base-url` / `--model` 时回退到 Job 固化值（与 `polish-translation` 同口径），显式传入则覆盖并按同一时机写回，保证 `translate` 与 `continue` / `status` 看到同一套身份。

### 失败不写回的原因

- 写回必须排在首个批次成功**之后**：先写回再请求，环境变量里的过期模型名会先被钉进 `job.json`，而 `continue` 没有 `--model`，用户修好环境变量也没有入口再改它。首批 4xx 后字段保持 null，修好环境变量即可直接 `continue`。
- 指纹早在解析时已按同一份 provider 配置算出，推迟写回不影响一致性。
- Agent 降级不写回（半套配置会误导下一次 `continue`）；原子 Job 没有 `config.workflow`，不写。
- `api_key` 永不落盘，仍只读当前终端的环境变量。Job 已固化身份、当前终端只缺 API Key 时，auto 模式的降级原因会点名 Job 钉住的 `model` / `base_url` 与缺失的环境变量，不静默降级。
- 换模型的途径：重新 `run`，或 `xvt translate JOB --target-language LANG --model NEW`，或直接编辑 `job.json`。

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
- 所有**已登记 artifact** 的有效性（`verify_artifact` / `valid_artifact_path`）只认内容 checksum，元数据不参与。

### 一处已知例外：烧录派生指纹里的源媒体身份

本地源媒体在 `_burn_fingerprint` 里的身份是 `{path, size, mtime_ns}`，不是内容 checksum（刻意如此：`status` 不为每次查询哈希整个视频文件，见下文「status 的成本边界」）。该指纹又是 `_burn_satisfied` 的放行条件之一，所以这里的 size+mtime **确实起了「证实」作用**：把源媒体换成一个 size 与 mtime 都相同的不同文件，`_burn_satisfied` 仍判烧录已完成，不触发重烧。

如实记录而不是承诺更强的口径。接受它的理由：

- 命中需要外部写入者精确伪造 size 与 mtime（`cp -p` / `rsync -a` 只在**同一份内容**上保留 mtime，不构成命中）。
- 源媒体是用户自己的输入文件，不是 Job 登记的产物；它的 checksum 从未被登记过，没有可比对的基线。
- v2 proof 里存有实际源媒体内容 checksum，`relink-burned-output` 写入前会重算并比对，这条路径不受影响。
- 需要确证时，用户可以显式 `xvt burn` / `continue` 重烧。

**不要再把 `touch` 源媒体当作「让指纹失效」的手段**（本文此前如此推荐，已不成立）：元数据漂移现在走下面的内容 checksum 回退，内容没变就不判失效。要强制重烧请直接运行 `xvt burn JOB` / `xvt continue JOB`。

### 指纹不匹配时的源媒体内容 checksum 回退

上面那条「只能证伪、不得证实」的通则有一个反方向的坑：元数据证伪的可能是一个**内容上完全有效**的完成态。只 `touch` 一次源媒体（内容 checksum、size、路径全不变）就会让 `status` 从 `complete` 翻成 `retryable - 烧录派生指纹已失效`，代价是整段重编码；不带 `-p` 的 `cp`、不带 `-a` 的 `rsync` 和备份还原同样触发。

因此指纹判定现在是两段式，单点实现 `workflow._burn_fingerprint_satisfied`（烧录满足度与 v2 proof 比对共用）：

- **快路径**：已登记指纹与现算的 `{path, size, mtime_ns}` 指纹相等即放行。常见情形走这里，**零额外 IO，`status` 的成本边界不变**（下文那条承诺继续成立）。
- **回退**：快路径判不匹配时才执行，要求三条**全部**成立，否则照旧判失效（fail closed）：
  1. 存在 checksum 有效的 v2 proof，且其 `fingerprint` 就是已登记那份（legacy fingerprint 拿不到这条回退）；
  2. proof 里登记的烧录时源媒体身份（`source_media_identity`）与**当前**其余指纹输入重算出的指纹仍等于已登记指纹——这一步证明指纹差异**只**来自媒体身份；
  3. 源媒体当前内容 checksum 等于 proof 登记的那份内容 checksum。

为什么这不破坏成本边界：回退只发生在**本来就要报「失效」**的那条分支上，而那条分支的后果是重新编码整部视频，一次哈希远比它便宜；快路径命中时一次额外的读都没有。

为什么第 2 条不能省：没有它，「手改 `job.json` 里的 `target_font` / `source_font` / `hardware_encoding` 后指纹漂移」会被内容 checksum 一起放过，`continue` 报「已完成」而新设置永远烧不上去。指纹的另一项输入 `translated_cues_checksum` 不需要单独判：`burn_fingerprint` 在 `artifacts.DERIVES_FROM` 里是 `burned_video` 的下游（而它派生自 `translated_cues`），改写译文的各条路径还会显式 `clear_burn_fingerprint`，proof 登记随即消失，第 1 条就不成立。媒体 `path` 漂移无害：换到另一份**内容相同**的媒体上，烧录结果本来就一致。

**已知边界**：`source_media_identity` 是本次新增的**可选**字段，只对本地媒体登记（远程媒体的身份本就是内容 checksum，不含 mtime，不会假失效）。存量 v2 proof 没有它，回退对那些 Job 不生效（仍按旧行为重烧一次），下一次由 `run` / `continue` 完成的烧录即补上。proof 文档的三个必需字段口径未变，存量 proof 的既有完成语义与 relink 资格一律不受影响。

### 源媒体不在登记路径时的导航与报错

源媒体不在登记路径时 `render` 与 `burn` 都会以 `video file does not exist: <path>` 失败（两步都要读实际媒体文件）。因此：

- **`status` / `continue` 的恢复分类**：计划只剩 `render` / `burn` 且**本地**源媒体不在登记路径时归 `manual_fix`，`reason` 点名那个路径——不推荐一条必然再次失败的 `continue`。导航给出两条出口：把源媒体**原样**放回该路径，或者（媒体是永久搬家时）用下一节的 `relink-source-media` 把持久化路径指到新位置。**`primary_command` 只在 `relink-source-media` 真的会成功时才给它**：该命令本身要求 checksum 有效的 v2 proof，拿不到就以退出码 2 失败，因此这道资格交给 workflow 侧的 `source_media_relink_available` 把关（与命令本体共用同一份判定，不在 `recovery` 就地重造）；只有 legacy fingerprint 的旧 Job 维持 `primary_command` 为 `None`，只点名那个路径。两支都**不**承诺「放回去就不必 `continue`」——指纹快路径要求 `{path, size, mtime_ns}` 全等，mtime 变了得走内容 checksum 回退，而存量 proof 缺 `source_media_identity` 时回退拿不到授权、Job 仍落到 `burn`；因此统一让用户之后重新运行 `xvt status` 看下一步。`alternative_commands` 两支都为空。刻意**不**推荐 `xvt burn JOB --media NEW`：它能产出合格成品，但不改持久化的源媒体路径，Job 会继续停在同一状态。
- **远程 Job 刻意例外**：下载产物缺失时 `continue` 能重新获取媒体，那是真出口，仍按 `retryable` 处理。
- **计划里还有 `translate` 等步骤时不拦**：那些步骤 `continue` 能真正推进。
- **`relink` 的报错**：源媒体不在原路径时报「source media is missing: `<path>`」而不是「fingerprint is stale」——指纹并没有过期，是算不出来。方向不变（两者都拒绝），只是说对了原因。

**媒体是永久搬家（而不是临时不在）时**，出口是下一节的 `xvt relink-source-media`，它把持久化路径指到新位置。**恢复导航已经接入它**：Job 具备 checksum 有效的 v2 proof 时 `primary_command` 就是 `xvt relink-source-media JOB NEW_MEDIA_PATH`，新路径位置是**占位符**——媒体搬到哪里只有用户知道，导航不猜（与 `relink-burned-output` 的 `EXISTING_VIDEO` 同款写法）。分类仍是 `manual_fix` 而不是 `awaiting_input`：这个状态确实需要人工给出新路径，而 `batch.py` 把 `manual_fix` 归入「绝不自动推进」，改分类会改变批处理语义。

## 源媒体搬家后的路径重指

`xvt relink-source-media JOB NEW_MEDIA_PATH`（advanced）是把持久化的烧录源媒体路径指到新位置的唯一入口。

**它不是新的状态机行为，是给一个已经正确的行为配一个入口。** 上一节的内容 checksum 回退本来就处理得了「媒体搬家」：路径变了、内容没变时，回退的三条件仍然全部成立（proof 里的烧录时身份重算出的指纹不变，源媒体当前内容 checksum 不变），指纹照旧判成立、Job 零重烧回到 `complete`。此前缺的只是一个受支持的方式去改那个路径——只能手改 `job.json`。

### 放行条件（全部满足才写入）

- Job 身份匹配、有持久化 workflow 目标且 `burn` 为真。
- 媒体不是远程 URL（见下）。
- 存在 checksum 有效的 `version=2` burn proof。没有它就没有可比对的基线，证明不了是同一份媒体；legacy fingerprint 不给这条出口，与 `relink-burned-output` 同口径。
- 候选文件存在且是文件，其内容 checksum 等于 proof 登记的 `source_media_checksum`。

其中「`burn` 为真」「媒体不是远程」「存在 v2 proof」三条（与候选文件无关、只看 Job 本身的条件）收敛在 `workflow.source_media_relink_blocker` 一份实现里：命令本体消费它的标签取报错文案，恢复导航消费布尔包装 `source_media_relink_available` 决定要不要把这条命令放进推荐位。两侧同口径是刻意的——只暴露「有没有 proof」会让另外两条前置条件在导航侧再写一遍，命令收紧时导航不跟着动。

任一条不满足以退出码 2 拒绝，Job 一个字节都不改。

### 写入面与事件

只写 `config.workflow.burn_media` 一个字段。不重烧、不碰 artifact 登记、不改 stage，因此**刻意不发 artifact 事件**——`relink-burned-output` 发 `artifact_invalidated` / `artifact_created` 是因为它真的改写了 `burned_video` 的登记路径，这条命令借用那两个事件会谎报产物变动。改动落在 `job.json`（恢复依据本身），JSONL 模式下由 `cli_completed` 的 `media` 字段带出新路径。候选已经是当前生效媒体时直接返回，不写盘。

### 刻意的边界

- **不要求源媒体「当前确实缺失」**：指到另一份内容相同的副本同样安全（烧录结果本来就一致），多一道状态判定只会多一条没必要的拒绝路径。
- **`burn_media` 原本为 `None` 的 Job 照写**（媒体来自 `job.input` 的常见情形）。代价如实记录：`_burn_media_value` 是 `config.burn_media or job.input`，写入后 `burn_media` 覆盖 `job.input` 成为烧录与 QA 的媒体来源。可观察到的差异有两处，都只在后续**重跑翻译链路**时出现：`pipeline.common.translation_qa_context` 对有 `burn_media` 的 Job 改为 `probe_media(burn_media)`（而不是读 `media_info` artifact），因此布局 QA 多一次 ffprobe，且媒体若再次移走会直接探测失败而不是回落到 `media_info`；`paths.py` 的覆盖保护里，`job.input` 一直在 `_user_input_paths` 中无条件受保护，而 `burn_media` 只在它是本次烧录/预览实际使用的媒体时经 `media_path` 受保护（这一条与 `run --burn-media` 的既有行为一致，不是本命令引入的）。
- **存量 v2 proof（缺可选字段 `source_media_identity`）仍放行**：授权依据是 v2 的必需字段 `source_media_checksum`，匹配即证明是同一份媒体，写入是正确的。零重烧是回退判定的性质、不是本命令的承诺——缺身份字段时回退拿不到授权，Job 落到 `burn` 重烧一次（新 proof 随即补上该字段）。拒绝它才是把用户推回手改 `job.json` 的死角。
- **远程 Job 不支持**：媒体是 Job 内登记的下载产物（`source_video`），它在指纹里的身份本就是内容 checksum、不含路径，「搬家」这个概念不适用；把 `burn_media` 指到一个本地路径还会把媒体身份从「内容 checksum」口径换成 `{path, size, mtime_ns}` 口径，指纹必然漂移、反而逼出一次重烧。下载产物自身缺失时 `continue` 能重新获取，那是真出口。

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

理由显式登记在 `cli._UNROUTABLE_AWAIT_STEPS`。**新增等待步骤必须先登记进 `workflow.AWAIT_STEPS`，再在两张表（`_STEP_ROUTES` / `_UNROUTABLE_AWAIT_STEPS`）之一里表态**，两道都由守卫测试强制：`tests/test_workflow.py::AwaitStepsCoverageTest` 用 AST 比对 `plan_advance_steps` 实际产出的 `await_*` 与 `AWAIT_STEPS`（双向不许有差集），`tests/test_cli_command_tables.py` 管路由表。漏了第一道，`recovery` 的让路判定（`workflow_steps[0] in AWAIT_STEPS`）与 `xvt apply` 的路由会同时静默失效，两处都只是「不命中」、不报错。

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

legacy proof、媒体 QA、proof 或 fingerprint 无效的 Job 都不具备重新关联资格，按 `retryable` 恢复并由 `continue` 重烧。旧 Job 必须先 `continue` 重烧一次生成 v2 proof，不得宣称可直接 relink。源媒体不在原路径属另一类，报错与导航见「源媒体不在登记路径时的导航与报错」。

### 媒体 QA 放行与 proof 比对的单点实现

「媒体 QA 过了」只有一份实现 `workflow.media_qa_failure`（stage 必须 succeeded → 报告 checksum 有效 → 可解析 → `status == "pass"`，返回失败原因标签），烧录满足度、relink 资格、`relink_burned_output` 与 `recovery` 的烧录原因文案一律复用它，不得就地重造。v2 proof 的指纹比对同样单点实现 `_burn_proof_fingerprint_matches`，源媒体内容 checksum 是否参与比对由参数表达（status 侧不参与、relink 侧参与）。

刻意保留的差异：`_burn_satisfied` 要求成品**有效**而 relink 资格要求成品**已失效**；`_burn_satisfied` 不要求 `translated_cues` 有效（译文满足度单独判），relink 两处要求；`_burn_satisfied` 对「无 proof artifact 或 proof 文档不是 v2」的 legacy Job 只认指纹匹配即判完成，relink 一律要求 checksum 有效的 v2 proof。

### 烧录腿复用渲染腿的布局 QA 与 ASS

译文渲染腿（`pipeline.translation._qa_and_render_translation`）与工作流烧录腿（`pipeline.burnin.burn_translated_job`，`persist_workflow_qa=True`）此前各自算一遍同一件事：同一条 `run` / `continue` 里译文刚落盘，渲染腿按持久化目标测一次 libass 行宽、写 `qa-translated.json` 并渲出 `bilingual.ass`，紧接着烧录腿按同一份目标把两件事原样再做一遍。产物逐字相同、Job 状态正确，纯属重复做功——代价是**每次烧录多一次整轮 libass 测量**（`measure_bilingual_line_widths` 起一个 ffmpeg 进程，渲染 cue 数 × 2 帧过 `ass` 滤镜 + `bbox`），长片上按 cue 数线性增长。

现在烧录腿在 `_reusable_rendered_layout` 判定可复用时直接沿用已登记的两份产物：不重测、不重写 `qa-translated.json`、不重渲 `bilingual.ass`、不重记 `RENDER`（它已经是 `succeeded`）。**全部条件成立才复用，否则照旧现场重测重渲（fail closed）**：

- `SUBTITLE_QA` 与 `RENDER` 两个 stage 都是 `succeeded`；
- 报告 status 是 `pass` 或 `warning`。**已确认的 `needs_review` 刻意不复用**：那条路径的放行来自存档确认，`complete_qa_stage` 要为它发一条 `subtitle_review_acknowledged`（「这次放行依据哪条确认」的唯一事件证据），而布局确认正是约束 4 点名要绑定的东西，省下的一次测量不值这笔；
- `translated_subtitle_qa` 与 `bilingual_ass` 都 checksum 有效（两者都在 `downstream_of(TRANSLATED_CUES)` 的失效闭包里，译文一变登记当场被摘掉，复用不到旧产物）；
- 报告**自己**的 `layout` 块里 `measurement == "measured"`、`video_width` / `video_height` / `style_options` 与本次烧录真正要用的画幅和 `AssStyleOptions` 完全一致——画幅与字体的绑定取自那份报告被测量时的真实输入，不是「当前配置碰巧长这样」的推断，手改 `job.json` 的字体也绕不过去；
- 本次烧录媒体的时长已知，且全部 cue 都落在时长内。媒体时长没有记进报告，所以这一项不推断而**就地重判**：时长在整份报告里只影响 `outside_media`（error 级）一条，按本次媒体重算一次即可，时长未知（0）时拒绝复用。

术语表不参与比对：两腿都调 `required_glossary_terms(job)`，同一个 Job 必然同值，且该判定与画幅、字体、媒体一概无关。

**约束 4 不因此松动。** 放行判定仍在复用判定**之前**执行：`_assert_translated_qa_gate` 对同一份报告跑 `review_gate_satisfied`（与 `complete_qa_stage` 同口径），`failed` 或未确认的 `needs_review` 在那里就以 `RuntimeError` 拦住，烧录一帧都不会开始。复用只省掉一次**结论必然相同**的重算，不省掉任何一次判定。

**原子 `xvt burn` 不复用**（`persist_workflow_qa=False`）：它可以覆盖持久化目标之外的媒体、字体与编码参数，必须按本次实参现场校验，而且它从不紧跟在渲染腿之后，本来就没有第二遍可省。`xvt preview` 同理不变。

### status 的成本边界

普通 `status` 不会为 v2 proof 额外计算源媒体内容 checksum；内容 checksum 只在烧录落证、relink 校验，以及**指纹快路径已判不匹配**后的回退判定（见「指纹不匹配时的源媒体内容 checksum 回退」）时计算。后者不削弱这条边界：快路径命中时一次额外的读都没有，而快路径不命中的那条分支本来就要整段重编码。

`xvt status JOB` 对缺失的已登记烧录成品只做静态资格判断，不额外计算源媒体内容 checksum；符合资格时归类为 `awaiting_input`，优先给出 `relink-burned-output`，并列出显式 `continue` 重烧作为备选。
