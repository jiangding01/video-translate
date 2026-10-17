# 2026-10-17 全面 review 发现清单

本文件是一次全仓设计与实现 review 的结论存档，供后续逐条处理。**它是待办清单，不是契约**；每条处理完就在状态栏标记，全部清空后本文件可以删除。

## 来源与可信度

基线 `main @ 9166d3a`（review 执行时），由 5 个只读切面并行产出：正确性与恢复契约、安全边界、架构与可维护性、测试质量（含 19 个内存变异点）、文档漂移。复跑确认当时基线为 889 测试 + 178 子测试全绿。

可信度分三档，逐条标注：

- **✅ 已核验**：本会话主干亲自读代码确认了完整链条。
- **○ 已复现**：产出方声称做过端到端复现或实测，主干未独立复验。
- **△ 未验证**：静态推断，明确标注为低置信。

状态：`待修` / `已修（PR #NN）` / `待决策`。

## 阻断级

### B1 译文二次回填后交付旧字幕 — ✅ 已核验，**已修（PR #97）**

`apply_agent_translation` / `apply_subtitle_translation` 传了 `invalidate_preview_artifacts`，只失效预览链，不失效 `translated.srt` / `bilingual.srt` / `bilingual.ass`，也不重置 QA/RENDER/BURN/MEDIA_QA 与烧录 fingerprint。新译文的 QA 落在 `needs_review` 时收尾在 QA 关口早退不渲染，旧 SRT 仍 checksum 有效，`_translation_satisfied` 据此判完成并发 `workflow_completed`。已改用 `invalidate_rendered_artifacts` 并补两条红先回归测试。

## 应修

### N1 `subtitle_review_acknowledgment` 代签放行 — ○ 已复现，待决策

人工确认的有效性只绑定 `source_cues` 的 checksum 与「问题族 + cue id」（`pipeline/review.py` 的 `acknowledged_review_issues` / `review_gate_satisfied`），**不绑定译文内容**。用户为 v1 确认过 cue 1 的超宽后，一条完全不同、同样在 cue 1 超宽的 v2 译文会被旧确认自动代签放行，用户从未看过它。四条译文回填路径（agent / import / retranslate / semantic 收尾）同样暴露。

佐证：`pipeline/revision.py` 与 `pipeline/semantic.py` 在改写 cue 时都显式 `job.config.pop("subtitle_review_acknowledgment", None)`，说明「只绑 source checksum 不够」在那两处已被意识到，而 `invalidate_rendered_artifacts` 没做。

最小修法是在 `invalidate_rendered_artifacts` 里一并 pop，但会改变「acknowledge 之后 continue」的现有体验（确认会被紧随其后的回填清掉），需要先确认既有用例是否仍成立。**这是本清单里唯一需要先做设计决策再动手的条目。**

### F1 `stage.error` 裸写进 `job.json`，绕过脱敏 — ✅ 已核验，**已修（本轮）**

`pipeline/common.py` 的 `set_stage` 里 `record.error = error` 后直接 `save_job`；同一函数的事件分支走 `error_event_fields` 是脱敏的，持久化分支不是。`job_store.py` 全文无 `redact`。约 18 个调用点传 `error=str(error)`，而 `CommandError` 内嵌 yt-dlp / ffmpeg 原始 stderr（`process.py`），403 分支原样拼 detail（`download.py`），翻译 4xx 带 base_url（`translation_provider.py`）。

后果：签名 URL、Cookie 路径、代理凭据明文落盘，`status --json` 原样输出。**AGENTS 不可破坏约束第 5 条「任何会持久化的错误文本都必须先脱敏」目前在 job.json 层不成立。**

已修：`pipeline/common.py` 的 `set_stage` 在函数入口算一次 `redacted_error = redact_text(error) if error is not None else None`，`record.error` 与 `stage_failed` 事件的 `error_event_fields` 都用它——两条出口因此给出同一份文本（`redact_text` 幂等，事件分支再脱敏一次结果不变）。**刻意不补那约 18 个 `error=str(error)` 调用点**：靠调用方自觉的契约已经漏了 18 次，再补一遍还会漏第 19 次。`events.redact_text` 的 docstring 同步改写为「事件与 `job.json` 两条持久化边界都已集中接入」，并写明幂等保证。新增 `tests/test_pipeline.py::StageErrorRedactionTest`，5 个 subtest 覆盖签名 URL、Cookie 头、代理 userinfo、Bearer、provider base_url 五类真实来源，断言落在「`job.json` 落盘后的原始文本不含明文凭据」上，并校验事件与 `job.json` 文本一致。另补一条守卫 `test_messages_compared_by_value_survive_redaction_unchanged`：`pipeline/review.py:246` 用 `record.error != SUBTITLE_REVIEW_REQUIRED_MESSAGE` 识别并收口确认后的过期阻断错误，脱敏一旦改写这条常量，收口会静默失效且没有测试直接指向脱敏。当前常量是纯英文散文，脱敏为空操作。

**同轮排查的其它持久化出口**（如实记录，均未改动）：

- `batch.json` 的 `item["error"]`：`batch.py:400` 与 `:455` 已经通过 `create_event(...).data` round-trip 脱敏，无缺口。
- `job.config` 的凭据相关字段：远程 Job 只存 `proxy_enabled: bool` 与 `cookies_from_browser`（浏览器名），本就不落凭据。
- `job.config["workflow"]["base_url"]`：曾是 O3 的洞，本轮由入口校验堵上。
- `artifacts/source-candidate-qa.json` 的 `invalid_candidate` issue message 带 `load_subtitles` 的 `str(error)`，**不过脱敏边界**。未改：内容是本地路径加解析错误，无凭据面；而 QA message 被 `candidate_qa.overflow_limits` 的正则解析，改动面大于收益。
- `pipeline/revision.py:413` 的 `qa_record.error = qa_message` 绕过 `set_stage` 直写 `record.error`。未改：取值只有两个硬编码常量，零凭据面。**但目前没有守卫阻止下一处这样写**——可考虑补一条「只有 `set_stage` 能写 `record.error` 的非 None 值」的 AST 守卫（与 O7 的守卫缺口同族）。
- `job.input`：用户原样输入的 URL，是恢复依据，按原样落盘。用户若把签名 URL 当输入，它在 `job.json` 里是明文而在 `job_started` 事件里已脱敏，这条不对称是刻意的。

### F2 端到端 Job 上 `xvt translate` 丢持久化 policy 与 batch_size — ○ 已复现，**已修（本轮）**

`pipeline/translation.py` 的 `resolved_policy = policy or load_translation_policy(glossary, ...)` 从 CLI 参数现算，不回退 `config.workflow` 快照（`base_url` / `model` 有回退，policy / batch_size 没有）。

触发：`run --glossary ...` 之后执行 `xvt translate JOB --target-language L`（恰是恢复计划里「放弃重译」的推荐命令）→ 译文不带术语 → 译文 QA 按 workflow policy 报 `glossary_violation` 死锁回去；API 模式下指纹漂移触发 progress reset，**全部批次重付一次翻译费**。

已修：`pipeline/translation.py` 新增 `_resolved_translation_inputs`，两个入口在未显式传参时**逐字段**回退 workflow 快照（只给 `--glossary` 时风格仍沿用快照，只给 `--translation-style` 时术语表仍沿用快照），与 base_url/model 同口径。`batch_size` 一并修了：`cli.py` 的 `translate` / `prepare-translation` 两处 `--batch-size` 与 `--translation-style` 去掉默认值，用 `None` 表达「未显式指定」（与既有的 `polish-translation` 同口径），没有新增参数。

### F3 Agent manifest 失效时端到端 Job 卡死 — ○ 已复现，**已修（本轮）**

位置修正：洞不在 planner 的主等待分支（原 `workflow.py:732-742` 那条已经要求 `valid_artifact_path(job, "translation_batches")` 非空，注释也写明了），而在其后的**兜底 `elif`**（原 :745-746）：manifest 无效 + 没有 `translated_cues` + stage 是 `AWAITING_INPUT` 时，兜底分支照样返回 `await_agent_translation`，把主分支的守卫绕过去。对照 `recovery.py` 的原子分支在同样情形下返回 `retryable` + `translate`。结果三命令互指：`status` 说等回填，`apply` 被 manifest 校验拒绝，`continue` 只发 waiting，没有出口。

已修：删掉该兜底分支，落到 `translate` / `import_translation`；`_ensure_translation` 的等待分支同步要求 manifest 有效，否则重建批次。`status` / `continue` / `batch-continue` 三侧口径一致。

### F4 原子 Job 的 `qa_blocked` 不给在途 Agent 等待让路 — ○ 已复现，**已修（本轮）**

`recovery.py` 的让路块带 `and workflow is not None`，把原子 Job 排除在外。触发：原子 Job 术语死锁后改跑 `translate --provider agent`，`status` 仍推荐 `retranslate-cues`，照做会盖掉整轮 manifest。这是端到端侧已修问题的同构遗漏。

已修：原子 Job 没有 `plan_advance_steps`，改用等价条件——`TRANSLATE` 停在 `AWAITING_INPUT` 且 `translation_batches` checksum 有效时放行，由后面既有的原子等待分支给出 `apply-translation` 引导；manifest 已经不在的等待态残留不放行。

**本轮新发现的相邻缺口（未修）**：polished 原子 Job 在同一状态下，让路后会先命中「润色候选有效，可以重新执行原子翻译命令继续语义校对」那条 retryable 分支（它排在原子等待分支之前），推荐的 `translate --translation-quality polished` 同样会盖掉刚写入的 manifest。这是原子分支内部的判定顺序问题，不属于 qa_blocked 让路，需单独决策。

### F5 `workflow.py` 逐字复制 pipeline 的 3 个函数 — ✅ 已核验，待修

`workflow.py` 的 `_source_selection_satisfied` 与 `pipeline/transcription.py` 的 `_source_selection_matches` 逐行相同（仅调用名差异），另两处复制自 `pipeline/common.py`。违反 AGENTS「`workflow.py` 只负责编排，不应复制 pipeline 包的算法」。

风险：以后给 `selected_kind` 加新分支只改一边，`continue` 与 `resume` 对「源是否满足」的判定分叉。修法：workflow 改为引用 pipeline（把源选择函数公有化 + re-export）。无测试桩阻碍。

### F6 烧录满足度 3+1 份口径，且两道媒体 QA 条件零测试覆盖 — ○ 已复现，**已修（本轮）**

> **这一轮的第一步不是改生产代码，是改测试。** O5 的终检守卫撞红的 7 处与本条变异实证指出的是同一批桩、同一个根因：测试把 `run_translation` / `apply_subtitle_translation` / `burn_translated_job` / `valid_artifact_path` 替换成不推进 Job 的假实现，却断言 `result.status == "completed"`——**这批测试从来没有验证过「completed 对应一个真的完成了的 Job」**。待改清单（均在 `tests/test_workflow.py`）：`test_external_translation_skips_provider`、`test_automatic_translation_and_burn_run_in_order`、`test_continue_reuses_persisted_translation_policy`、`test_continue_defaults_translation_policy_for_old_jobs`、`test_continue_burns_after_agent_translation_was_applied`、`test_continue_retries_burn_when_media_qa_failed`、`test_atomic_burn_layout_failure_preserves_workflow_qa_and_recovery`（3 个 subtest）。改成构造真实产物状态后，F6 的「不打桩的真实状态测试」与 O5 的守卫可以一起落。

架构侧：`workflow.py` 三处各写一遍「媒体 QA 过 + proof 匹配」，`recovery.py` 是第四种口径。测试侧（变异实证）：`_burn_satisfied` 的 stage 判断与 `media_qa.status == pass` 判断**一起删掉，全量测试仍全绿**——根因是唯一相关测试把 `valid_artifact_path` 打了桩，「media_qa 已登记且 failed」这个真实状态从未被构造过。

放过的 bug：legacy Job 原子重烧后媒体 QA 失败，`continue` / `status` 判已完成（违反约束 4）。

修法：先补一条不打桩的真实状态测试（两道条件各一个 subTest），再抽 `_media_qa_passed` 与 proof 比对 helper，三处保留各自刻意的差异（`status` 不哈希源媒体、relink 哈希）。工作量：半天级。

**实际修法（本轮，F6 + O5 同一轮四阶段）**：

- **阶段 A（先改测试，不改生产代码）**：上面点名的 7 处（含 1 处 3 个 subtest）桩全部改成「打桩也产生真实副作用」。新增三个共享 helper（`tests/test_workflow.py` 模块级）：`_write_source_leg_artifacts`（落 `source_cues` / `source_srt`，取代 `patch("xvt.workflow.valid_artifact_path")` 的整体伪装）、`_complete_translation_artifacts`（写 `translated-cues.json` + 真实 `save_subtitles` 两份 SRT + `check_subtitles` 真实 QA 报告，推进 TRANSLATE / SUBTITLE_QA / RENDER）、`_complete_burn_artifacts`（写成品 + `media-qa.json` pass，推进 BURN / MEDIA_QA）。带 burn 的 Job 改用**真实存在的本地媒体文件**作输入，`_record_burn_fingerprint` 因此走真实实现登记 v2 proof（`run_transcription` 的桩也补上「把 workflow 目标固化进 Job」这一真实副作用，否则 proof 登记会静默跳过）。**既有断言一字未改**（`translate.call_args` / `burn.assert_called_once` / 事件序列等全部保留）。阶段结束时用临时断言逐条验证 `plan_advance_steps(job, config) == ()`，验完删除——阶段 D 的守卫永久接管这件事。阶段 A 没有暴露任何生产代码问题。
- **阶段 B**：新增 `test_burn_not_satisfied_when_media_qa_blocks_a_valid_burned_output`（不打桩，两个 subTest）。先断言基线满足（成品有效 + 指纹匹配 + 媒体 QA pass → `_burn_satisfied` 为真、计划为空），再分别只改 stage（FAILED，报告仍 pass）与只改报告（`status: failed`，stage 仍 succeeded），断言 `_burn_satisfied` 为假且计划给出 `burn`。变异自证：两道判定分别失效时对应 subtest 各自变红。
- **阶段 C**：`workflow.py` 新增 `media_qa_failure(job) -> str | None`（标签 `stage` / `missing` / `unreadable` / `status`）与 `media_qa_passed(job)`，三处接入；relink 的四条历史报错文案由 `_RELINK_MEDIA_QA_MESSAGES` 按标签映射，措辞逐字保留。proof 比对抽出 `_burn_proof_fingerprint_matches(..., require_source_media=bool)`，由 relink 资格（False）与 `relink_burned_output`（True）共用。`recovery._burn_recovery_reason` 改为接收调用方算好的 `media_qa_blocker` 标签、只在 `"stage"` 上分叉，不再自己重写 stage 判定；它对报告侧三类失败继续沿用「成品缺失 / 指纹失效」文案（见下方「刻意差异」）。
- **阶段 D（O5）**：`_advance_workflow` 在发 `WORKFLOW_COMPLETED` 之前断言 `plan_advance_steps(job, config)` 为空，不为空抛 `InternalError`（`category="workflow"` / 退出码 4），消息带上剩余步骤。阶段 A 做完后**零既有测试变红**。新增两条测试：注入真实不一致（烧录桩只写成品、不推进媒体 QA）→ 守卫触发且不发 `workflow_completed`；一致状态 → 照常完成。

**真重复 vs 刻意差异**（阶段 C 的判定依据）：

| 项 | 判定 |
| --- | --- |
| 媒体 QA 四步（stage succeeded → 报告 checksum 有效 → 可解析 → `status == "pass"`） | **真重复**，三处逐字相同，已收敛为 `media_qa_failure` |
| 指纹比对（`proof["fingerprint"] == _burn_fingerprint` 且 `_stored_burn_fingerprint` 一致） | **真重复**（relink 资格 / relink 命令），已收敛为 `_burn_proof_fingerprint_matches` |
| 是否哈希源媒体内容 | **刻意差异**，参数化保留：status 侧不读大文件，写入登记的 relink 必须比对 |
| 成品有效 vs 成品已失效 | **刻意差异**：`_burn_satisfied` 要求有效，relink 资格恰恰要求已失效 |
| 是否要求 `translated_cues` 有效 | **刻意差异**：relink 两处要求（没有译文 cue 的重新关联无意义），`_burn_satisfied` 不要求（译文满足度由 `_translation_satisfied` 单独判） |
| legacy proof 容忍度 | **刻意差异**：`_burn_satisfied` 对「无 proof artifact / proof 文档不是 v2」的 legacy Job 只认指纹匹配并返回 True，relink 一律要求 checksum 有效的 v2 proof。因此 `_burn_satisfied` 不能复用 proof helper 的「proof 必须存在且匹配」口径 |
| relink 的四条媒体 QA 报错文案 | **刻意差异**（UX），按标签映射保留逐字措辞 |
| `recovery._burn_recovery_reason` 的文案排序 | **刻意差异**：它是文案排序而非放行判定。报告侧三类失败（`missing` / `unreadable` / `status`）继续走「成品缺失 / 指纹失效」文案——对用户更具体，且 `_burn_satisfied` 早已独立判这些状态为未完成，不存在误报完成。改这个排序是用户可见行为变更（`tests/test_recovery.py` 的两条测试钉住了现文案），不塞进纯重构阶段 |

### F7 人工修订预检的 error 级阻断无测试 — ○ 已复现，待修

`pipeline/revision.py` 的 error 级阻断删掉后全量仍绿；现有测试只覆盖行宽阻断。放过的 bug：修订删掉术语表译名 → `glossary_violation`（error）→ `apply --check` 报 `valid: true`，正式 apply 落盘，违反「写入前重复预检」契约（下游 QA 门仍会拦烧录，但修订已经写进 Job）。补一条 glossary 用例即可。

### F8 签名参数脱敏名单不全 — ○ 已实测，**已修（本轮）**

`events.py` 的拒绝名单漏掉 GCS v4 的 `X-Goog-Signature` / `X-Goog-Credential` 与阿里云 `auth_key`（实测原样保留）；腾讯 `q-signature`、Akamai `hdnts` 为推断。与 AGENTS「签名 URL 不得原样写入事件」不符。主用例是 YouTube，触发概率偏低。

已修：新增 `_is_sensitive_query_key`，`redact_url` 改为调用它，三层判定：

1. **精确名单**（原 `_SENSITIVE_QUERY_KEYS` 原封不动保留），保证向后兼容。
2. **子串规则** `signature` / `credential` / `token` / `secret` / `password` / `accesskey` / `apikey`：这几个词几乎不构成正常参数名的一部分，误伤面最小而覆盖面最广（`X-Goog-Signature`、`X-Amz-Credential`、`q-signature`、`csrf_token`、`OSSAccessKeyId`、`SecretAccessKey` 全部命中）。
3. **分段整词规则** `auth` / `authorization` / `key` / `sig` / `pwd` / `passwd`：把键名小写后按 `[^a-z0-9]+` 切段，某一整段相等才命中。这几个词做子串必然误伤（`key` → `monkey` / `keyword`，`sig` → `design` / `signal` / `assignment`，`auth` → `author`），分段后阿里云 `auth_key` → `["auth", "key"]` 仍然命中。

取舍：`token` 留作子串意味着 `tokenizer=` 这类参数会被误脱敏；判断是媒体 / 下载 URL 里出现它的概率远低于漏掉一种 `*_token` 签名方案的代价。Akamai 的 `hdnts` 三层都不命中，本轮未加（原条目即标注为推断，没有实测样本）。

测试：`tests/test_events.py` 新增两条参数化测试把两侧都钉住——24 个敏感键必须脱敏（同时校验 `redact_url` / `redact_text` / `create_event` 三条入口），18 个正常键必须原样保留（`monkey` / `keyword` / `keywords` / `author` / `authors` / `design` / `designation` / `signal` / `assignment` / `itag` / `expires` 等）。另加一条 `redact_text` 幂等测试，为 F1 的「边界脱敏 + 事件再脱敏」提供前提保证。

### F9 文档进度陈述过期 — ✅ 已核验，**部分已修（PR #97）**

HANDOFF 里「下一步是 Phase 4c」那句已在 PR #97 修正。**剩余**：`ROADMAP.md` 只承认 Phase 1 完成，未反映 Phase 2/4a/4b/4c；`src/xvt/errors.py` 的注释称锁「尚未落地」而 `locking.py` 已在使用。分钟级。

## 可优化

| 编号 | 内容 | 判断 |
| --- | --- | --- |
| O1 | `semantic.py` 三处 12 行逐字相同的终态收口 + 两处问题分拣重复，抽 `_conclude_semantic_review` / `_partition_issues` 两个纯搬移 helper | **值得做**，半天；是 Phase 5 的轻量替代，能吃掉其大部分收益 |
| O2 | policy 回退有 4 份、两派语义（`common.py` / `semantic_state.py` 回退顶层键，`workflow.py` / `translation.py` 回退默认值） | **值得做**，F2 正是这条结构隐患的现实案例；并入 R3.4，必须排在 Q7 之前 |
| O3 | `translation_provider.py` 的 `resolve_provider` 不拒绝带 userinfo/query 的 base_url；实测 `user:pw@host` 的凭据经 `InvalidURL` 消息泄露（消息不含 scheme，`redact_text` 的 URL 正则不命中）且随 `config.workflow` 永久落盘 | **已修（本轮）**，见下方 O3 条目 |
| O4 | `paths.py` 的覆盖保护只认已登记 artifact；纯音频 Job 执行 `burn --output <job.input 的路径>` 会被 `ffmpeg -y` 静默覆盖原始媒体 | **值得做**，几行；后果是用户数据丢失。保护集加入本地 `job.input` 与用户原始字幕路径 |
| O5 | `_advance_workflow` 发 `workflow_completed` 前无终检守卫 | **已修（本轮）**，与 F6 同一轮落地：先按 F6 的阶段 A 把 7 处桩改成构造真实产物状态，再在 `_advance_workflow` 发 `WORKFLOW_COMPLETED` 之前断言 `plan_advance_steps(job, config)` 为空，不为空抛 `InternalError`（`category="workflow"` / `ExitCode.WORKFLOW_ERROR`）并带上剩余步骤。阶段 A 做完后零既有测试变红。详见 F6 条目的「实际修法」 |
| O6 | `_record_burn_fingerprint` 登记 v2 proof 不发事件，与约束 7 口径不一；proof 是 relink 的唯一授权凭据却在 JSONL 不可见 | 可做，几行；落在协议快照的已知盲区（烧录链路）内 |
| O7 | 三个守卫缺口：`AWAIT_STEPS` 是手抄清单，缺「planner 返回的 `await_*` ⊆ `AWAIT_STEPS`」的 AST 守卫；全仓无「禁 `shell=True` / subprocess 必须走 `process.py`」守卫；`recovery.py` 的 `checksum=None → warn` 分支无测试（改成 pass 全绿） | 各约半小时 |
| O8 | `transcription.py` 两处首跑不可达分支可直接删（零行为变化）；对齐/分段规则有 3–4 份副本且已漂移；旧 Job 路径冻结即可，突破 2000 行再按「本地转写 / 远程获取 / resume 与 rebuild」三簇拆 | 部分值得做 |
| O9 | 文档措辞：AGENTS「烧录成品缺失一律停 `awaiting_input`」比实现绝对（legacy proof 实为 retryable 会自动重烧，RECOVERY.md 才是准确口径）；RECOVERY.md「size+mtime 不承担有效性判定」不够准确（经指纹在 `_burn_satisfied` 实际起了证实作用：源媒体被同 size+mtime 的文件替换不会触发重烧，风险低但应如实写明）；AGENTS 模块表漏列 `errors` / `locking` / `cancellation` 等 7 个模块 | 值得做，分钟级 |
| O10 | macOS CI 不装 ffmpeg，主力平台的真实烧录与字体集成测试全部静默 skip，只有 Linux 覆盖，且无人看 skip 数 | 值得评估 |
| O11 | `cli.py` 一处 hint 用双引号包路径而非 `shlex.quote`（全仓唯一例外点） | 几分钟 |

### O3 `base_url` 带 userinfo 时凭据从入口漏出 — ○ 已实测，**已修（本轮）**

复现确认（CPython 3.12.9，`urllib`，有代理与无代理两条路径结果一致）：

```
http.client.InvalidURL: nonnumeric port: 'secretpw@api.example.com'
```

这条消息**不带 scheme**，`events._URL_PATTERN`（要求 `https?://`）与 `_USERINFO_PATTERN`（要求 `scheme://`）都不命中，`redact_text` 原样返回——所以**即使修了 F1 也兜不住，必须在入口拒绝**。而 base_url 还会随 `job.config["workflow"]` 永久落盘。

已修：`translation_provider.py` 新增 `_validate_base_url`，`resolve_provider` 在解析出 Base URL 之后、「配置是否齐全」判定**之前**调用它，发现 userinfo（`parsed.username` / `parsed.password` / netloc 里的 `@`）或 query 串即抛 `UserInputError`。错误消息只回显 `parsed.hostname`，绝不回显 netloc、用户名、密码或原始 base_url。

两处刻意的取舍：

- **校验排在 `missing` 判定之后**：走到那里才说明这条 base_url 真的要被用来发请求，此时带凭据或带 query 一律 fail closed。放在前面会让一条根本不会被使用的 base_url 打死 `--provider auto` 的降级路径，而 AGENTS 规定 auto 在配置不全时降级；配置不全时这条 base_url 既不发请求也不落盘（provider 身份要首批成功才写回 Job），凭据没有任何出口。有专门测试钉住这条。
- **query 串一并拒绝**：它既可能携带签名凭据，又会被 `base_url.rstrip("/") + "/chat/completions"` 拼成无效端点（`https://host/v1?x=1/chat/completions`）。入口报错比留到请求时报 404 更清楚。Azure OpenAI 的 `?api-version=` 写法本来就在这条拼接下不可用，此改动只是把静默失败变成明确失败。

测试：`tests/test_translation_provider.py` 新增 4 条——userinfo 在 `auto` / `openai-compatible` 两种 provider 下都被拒且消息不含凭据、query 串被拒、环境变量来源的 base_url 同样被校验、正常 base_url 仍能解析并保留 `rstrip("/")` 行为。

## 观察（危害低，可不处理）

- 执行器 `_ensure_translation` 的等待分支基本是防御性死码，且文案与规划器不同——这是 R3.2 的低成本替代点（执行器改为按 `advance_steps[0]` 分派）。
- `recovery` ⇄ `pipeline` 的环实际还包含 `recovery` ⇄ `workflow`（9 处函数级 import）；`common.py` 里有一处已属多余的惰性 import 可顺手转顶层。
- socks5 等非 http scheme 的 userinfo 密码含 `@` 时脱敏漏掉后半段（实测）。
- yt-dlp 的 `-o` 模板不转义 output root 里的 `%`（元数据展开可能写到 Job 目录外，事后检查只报错不清理）。
- `relink` 在源媒体缺失时误报 "fingerprint is stale"。
- `repair_polish_draft` 的 `manual_fix` 没有给出可执行命令（依赖 F2 修复后补）。
- `translation.py` 的 `agent_translation_polish_*` 与 `polish.py` 的同名前缀谓词含义相反，建议改名 `agent_translate_and_polish_*`。
- README 用法块漏了 `--fail-on-waiting`。

## 未验证（△ 低置信，动手前先复现）

- 局部重译回填内容与原文逐字相同时，残留 manifest 可能造成一次误报等待。
- `workflow.py` 注释里描述的「`target_language` 为 None 的补翻」路径疑似不可达。
- yt-dlp 的 ProxyError 消息若带不含 scheme 的 `/videoplayback?sig=...`，脱敏可能不命中。

## 本次 review 的覆盖盲区

以下区域**没有**被这次 review 覆盖，不要因为本清单没提就认为它们没问题：

- `subtitles.py` / `segment.py` / `align.py` / `render.py` 的算法正确性（只核了时间轴不变约束）。
- `download.py` 的下载策略正确性与 yt-dlp 交互细节（只扫了脱敏面）。
- `transcribe.py` 适配层。
- 协议快照 golden 的内容本身（只审了比对方式）。
- 变异测试只打了 19 个点，集中在 fail-closed 分支，不是全面变异覆盖。
- Q6.2 语义收敛与真实 E2E 按约定排除。

## 建议处理顺序

1. ~~**正确性剩余**：F2 + F3 + F4 + O5。~~ F2 / F3 / F4 / O5 均已修（本轮）。
2. ~~**持久化脱敏收口**：F1 + F8 + O3。~~ 三条均已修（本轮），约束 5 现在在 `job.json` 层成立（在 `set_stage` 边界统一脱敏）。AGENTS 约束 5 的措辞已同步。
3. **N1 设计决策**：先定「确认是否绑定译文内容」，再动手。
4. **烧录判定收敛与 fail-closed 补洞**：~~F6 + O5~~ + F7 + O7。F6 / O5 已修（本轮，四阶段：改桩 → 真实状态测试 → 口径收敛 → 终检守卫），剩 F7（人工修订预检的 error 级阻断无测试）与 O7（三个守卫缺口）。
5. 之后再谈 O1（Phase 5 轻量替代）与 O2（并入 R3.4，必须排在 Q7 之前）。

对既有计划的建议（来自 review，主干部分认同）：**Phase 5 降级**——做完 O1 后剩余差异是「API 逐批 checkpoint」与「Agent 一次性回填」的协议本质不同，硬合一份编排风险高收益小；**R3.2 延后、大概率取消**——它要解决的规划器/执行器对齐问题可用上述低成本替代，完整缓存框架与 AGENTS 第 6 条冲突。这两条是建议，未拍板。
