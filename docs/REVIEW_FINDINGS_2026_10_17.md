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

### N1 `subtitle_review_acknowledgment` 代签放行 — ✅ 已核验，**已修（本轮）**

人工确认的有效性只绑定 `source_cues` 的 checksum 与「问题族 + cue id」（`pipeline/review.py` 的 `acknowledged_review_issues` / `review_gate_satisfied`），**不绑定译文内容**。用户为 v1 确认过 cue 1 的超宽后，一条完全不同、同样在 cue 1 超宽的 v2 译文会被旧确认自动代签放行，用户从未看过它。四条译文回填路径（agent / import / retranslate / semantic 收尾）同样暴露。

佐证：`pipeline/revision.py` 与 `pipeline/semantic.py` 在改写 cue 时都显式 `job.config.pop("subtitle_review_acknowledgment", None)`，说明「只绑 source checksum 不够」在那两处已被意识到，而 `invalidate_rendered_artifacts` 没做。

设计决策的结论不是「在 `invalidate_rendered_artifacts` 里一并 pop」——那会改变「acknowledge 之后 continue」的现有体验。实际修法更小：**确认记录里早就存着 `report_kind` 与 `report_checksum`**（`acknowledge_subtitle_review` 自确认通道引入时的 commit `d361828` 起就一直落盘），只是 `acknowledged_review_issues` 从来没读过它们。本轮把校验侧补齐即可，落盘格式零改动，真实 Job 的存量确认记录照常有效。

已修：`pipeline/review.py` 的 `acknowledged_review_issues` 在比对 `source_cues_checksum` 之后，再要求 `report_kind` 落在 `{"translated_subtitle_qa", "subtitle_qa"}` 内、且 `record["report_checksum"]` 等于当前登记的同类报告 checksum。报告缺失时 `artifact_checksum_for` 返回 `None`，因此显式排除 `None`——否则一条写着 `report_checksum: null` 的伪造记录会因「两边都是 `None`」相等而蒙混过关。四条译文回填路径（agent / import / retranslate / semantic 收尾）共用这一个判定，一处补齐全部收口。

不误伤「确认 → continue」：QA 报告是确定性的（只含 status / cue_count / issues / layout，无时间戳），同输入重算逐字节相同，`continue` 重建报告后 checksum 不变。

**已知性质（可接受代价）**：绑的是报告的**字节** checksum，不是语义等价。将来改动 `pipeline.common.write_json` 的序列化写法（缩进、分隔符、`ensure_ascii`）会让全部存量确认一次性失效，用户需要重新确认一次。方向是 fail-closed（宁可多问一次，不可少问一次），因此接受；真要改写法时在 Changelog 里提示即可。

测试：`tests/test_pipeline.py::PipelineTest::test_acknowledgment_does_not_sign_off_a_different_translation` 直接钉住本条——同一 Job、同一 cue、同一个 review 问题码（`bilingual_overflow_risk`，cue 1），只换译文内容，断言旧确认失效且新报告不放行；`::test_acknowledgment_rejects_unregistered_report` 覆盖 `report_checksum: null` 的伪造记录。两条都做过红先验证（临时去掉校验即复现修复前的代签行为）。同轮修掉 5 处手搓 fixture 缺这两个字段的问题，以及 `tests/test_workflow.py` 的 helper 用紧凑 JSON 写 QA 报告、与生产 `write_json` 的缩进 JSON 字节不一致——后者让 `test_continue_rebuilt_qa_keeps_stored_review_acknowledgment` 此前即使在生产路径上会失效也照样绿。

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

### F5 `workflow.py` 逐字复制 pipeline 的 3 个函数 — ✅ 已核验，**已修（本轮）**

`workflow.py` 的 `_source_selection_satisfied` 与 `pipeline/transcription.py` 的 `_source_selection_matches` 逐行相同（仅调用名差异），另两处复制自 `pipeline/common.py`。违反 AGENTS「`workflow.py` 只负责编排，不应复制 pipeline 包的算法」。

风险：以后给 `selected_kind` 加新分支只改一边，`continue` 与 `resume` 对「源是否满足」的判定分叉。

**三处的最终定位**（清单原文只说「另两处复制自 `common.py`」，本轮逐字比对确认）：

| workflow.py 的副本 | pipeline 的原件 | 差异 |
| --- | --- | --- |
| `_source_selection_satisfied` | `transcription._source_selection_matches` | 只差 helper 名（`_artifact_checksum` vs `artifact_checksum_for`）与 `"none"` vs `SourceCandidateKind.NONE`（`StrEnum`，相等） |
| `_artifact_checksum` | `common.artifact_checksum_for`（即清单说的「`record_media_download_selection` 一带」，就在它上面一个函数） | 逐字相同 |
| `_error_category` | `common.error_category` | 逐字相同 |

已修：`transcription._source_selection_matches` 按 R1.4「跨模块 `_` 前缀符号公有化」的先例改名为 `source_selection_matches` 并加 docstring 说明它是源腿满足度的唯一实现；三个名字一并经 `pipeline/__init__` re-export；`workflow.py` 删掉三份副本改为引用。行为零变化（三份副本语义等价，`SourceCandidateKind` 是 `StrEnum`）。`tests/test_pipeline_imports.py` 的白名单**无需改动**：没有新增包内依赖边，`transcription → common` 本就在白名单里。

测试桩：清单原文说「无测试桩阻碍」，不完全准确——`tests/test_pipeline_revision.py` 有一处**直接 import**（`from xvt.workflow import _source_selection_satisfied`，3 个调用点），不是 `patch()` 桩。已同步改名，仍从 `xvt.workflow` 导入（保留「这条判定确实是 workflow 在用」的覆盖），三条断言一字未改。

**第四份副本（本轮一并收掉）**：`batch.py` 也自己写了一份 `_error_category`，与 `common.error_category` 逐字相同。清单 F5 只点名 `workflow.py`，但这是同一个缺陷隔一个文件——批处理自己算一遍 error category，哪天 `common.error_category` 改了分类规则，批处理条目的 `error.category` 就会和 Job 事件里的对不上，而两边都绿。已改为复用 pipeline 实现。改完全仓 `def _error_category` / `def _artifact_checksum` / `def _source_selection_satisfied` 零命中。

守卫：新增 `tests/test_workflow.py::WorkflowReusesPipelineHelpersTest`，用 `assertIs` 把四个名字（含 `batch.error_category`）钉成 pipeline 的那一份对象。红先自证两种形态都红——① 名字在 `xvt.workflow` 上缺席（修复前的真实形态）→ 3 条全 `AttributeError`；② 有人重新在 `workflow.py` 里定义同名副本 → `assertIs` 失败。

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

### F7 人工修订预检的 error 级阻断无测试 — ○ 已复现，**已修（本轮）**

`pipeline/revision.py` 的 error 级阻断删掉后全量仍绿；现有测试只覆盖行宽阻断。放过的 bug：修订删掉术语表译名 → `glossary_violation`（error）→ `apply --check` 报 `valid: true`，正式 apply 落盘，违反「写入前重复预检」契约（下游 QA 门仍会拦烧录，但修订已经写进 Job）。

已修：`tests/test_revision_preflight.py` 新增 `_glossary_response` 构造器与两条用例，全程走真实 QA、不打桩绕过判定——术语表经 `translation_policy` 落进 workflow 快照，`translation_qa_context` 把它读成 `required_terms`，由 `check_subtitles` 真实判出 `glossary_violation`：

- `test_glossary_violation_blocks_preflight_and_formal_apply`：修订提交一条**短**译文（不触发任何行宽问题，所以唯一能让预检变红的就是 error 级那一支），断言 `valid: false`、blocker 只有 `glossary_violation` 且 `severity == "error"`、没有任何 blocker 的 message 以 `target line width ` 开头（否则这条测试证明不了 error 级分支还活着）、预检前后全目录 checksum 快照不变、正式 apply 抛 `no changes saved` 且不落 `translation_reviewed_cues`。
- `test_glossary_compliant_revision_passes_preflight`：对照组，补回术语译名后放行，证明阻断不是「凡术语表就一律挡」。

变异自证：删掉 `issue.severity == "error"` 那一支后全量 927 条里**恰好只有新增的那一条**变红。

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
| O2 | policy 回退有 4 份、两派语义（`common.py` / `semantic_state.py` 回退顶层键，`workflow.py` / `translation.py` 回退默认值） | **已修（本轮）**，并且**不是整洁问题**——已复现为真实可达的术语死锁，见下方 O2 条目 |
| O3 | `translation_provider.py` 的 `resolve_provider` 不拒绝带 userinfo/query 的 base_url；实测 `user:pw@host` 的凭据经 `InvalidURL` 消息泄露（消息不含 scheme，`redact_text` 的 URL 正则不命中）且随 `config.workflow` 永久落盘 | **已修（本轮）**，见下方 O3 条目 |
| O4 | `paths.py` 的覆盖保护只认已登记 artifact；纯音频 Job 执行 `burn --output <job.input 的路径>` 会被 `ffmpeg -y` 静默覆盖原始媒体 | **已修（本轮）**，见下方 O4 条目 |
| O5 | `_advance_workflow` 发 `workflow_completed` 前无终检守卫 | **已修（本轮）**，与 F6 同一轮落地：先按 F6 的阶段 A 把 7 处桩改成构造真实产物状态，再在 `_advance_workflow` 发 `WORKFLOW_COMPLETED` 之前断言 `plan_advance_steps(job, config)` 为空，不为空抛 `InternalError`（`category="workflow"` / `ExitCode.WORKFLOW_ERROR`）并带上剩余步骤。阶段 A 做完后零既有测试变红。详见 F6 条目的「实际修法」 |
| O6 | `_record_burn_fingerprint` 登记 v2 proof 不发事件，与约束 7 口径不一；proof 是 relink 的唯一授权凭据却在 JSONL 不可见 | **已修（本轮）**，见下方 O6 条目 |
| O7 | 三个守卫缺口：`AWAIT_STEPS` 是手抄清单，缺「planner 返回的 `await_*` ⊆ `AWAIT_STEPS`」的 AST 守卫；全仓无「禁 `shell=True` / subprocess 必须走 `process.py`」守卫；`recovery.py` 的 `checksum=None → warn` 分支无测试（改成 pass 全绿） | **已修（本轮）**，见下方 O7 条目 |
| O8 | `transcription.py` 两处首跑不可达分支可直接删（零行为变化）；对齐/分段规则有 3–4 份副本且已漂移；旧 Job 路径冻结即可，突破 2000 行再按「本地转写 / 远程获取 / resume 与 rebuild」三簇拆 | **死码部分已修（本轮）**，副本部分核实属实但不动，见下方 O8 条目 |
| O9 | 文档措辞：AGENTS「烧录成品缺失一律停 `awaiting_input`」比实现绝对（legacy proof 实为 retryable 会自动重烧，RECOVERY.md 才是准确口径）；RECOVERY.md「size+mtime 不承担有效性判定」不够准确（经指纹在 `_burn_satisfied` 实际起了证实作用：源媒体被同 size+mtime 的文件替换不会触发重烧，风险低但应如实写明）；AGENTS 模块表漏列 `errors` / `locking` / `cancellation` 等 7 个模块 | **已修（本轮）**，四处逐条核实后改，见下方 O9 条目 |
| O10 | macOS CI 不装 ffmpeg，主力平台的真实烧录与字体集成测试全部静默 skip，只有 Linux 覆盖，且无人看 skip 数 | **已修（本轮）**，见下方 O10 条目 |
| O11 | `cli.py` 一处 hint 用双引号包路径而非 `shlex.quote`（全仓唯一例外点） | **已修（本轮）**，见下方 O11 条目 |

### O8 首跑不可达分支 — ✅ 已核验，**死码部分已修（本轮）**

死码判断依据（逐条核过，不是照抄清单）：

- `run_local_transcription`（原 :824）与 `run_remote_transcription`（原 :1062）**都在同一个函数体内用字面量 config 创建 Job**，字典里固定写着 `"source_strategy": source_strategy`（参数有默认值 `"auto"`，永不为 `None`），`create_job` 只做 `dict(config or {})`、不剔除任何键。所以 `if "source_strategy" in job.config` 在这两处恒真。
- **旧 Job 走不到这两处**：它们只创建新 Job，从不 `load_job`。旧 Job（创建时还没有 `source_strategy` 键的那批）唯一会撞上同款判定并分流到 `_process_acquired_media` 的入口是 `resume_remote_transcription`（修改后 :1338），那里的 `job` 来自 `load_job`，键确实可能缺失——**该处的判定原样保留**。同文件另外 5 处 `"source_strategy" in job.config` 也都在 `load_job` 之后，一并未动。
- `_process_acquired_media` 本身不是死函数，删掉两处恒假分支后它仍由 `resume_remote_transcription` 调用，因此函数保留。

改法：删掉两个恒真 `if` 与其后不可达的 `return _process_acquired_media(...)`，并在原地留注释写明「Job 由本函数创建所以键必然存在，旧 Job 只从 resume 入口进」。零行为变化，未新增测试（没有可观测的行为差异可断言），全量测试零变红。

**「对齐/分段规则 3–4 份副本且已漂移」核实属实，本轮不动**（结构性重构，超出可优化范围）。定位与漂移内容：

| 位置 | 函数 | 置信度门 | 对齐后 QA error 门 | 失败行为 |
| --- | --- | --- | --- | --- |
| `pipeline/transcription.py:488` 起（函数 `_process_adaptive_media`，:375） | 主路径 | 有（参数来源：函数入参） | **有** | 自动回落 ASR 分段，`selection_reason` 区分 `alignment_below_threshold` / `alignment_post_qa_failed` |
| 同文件 :696 起（函数 `_process_acquired_media`，:621） | legacy 路径，O8 修完后只由 `resume_remote_transcription` 到达 | 有（参数来源：函数入参） | **无** | 抛 `ValueError` |
| 同文件 :1653 起（函数 `resume_job`，:1533） | resume 路径 | 有（参数来源：`job.config` 现读） | **无** | `set_stage(FAILED)` 后抛 `ValueError`，错误文案不带数值 |

分段侧的 `segment_transcript(transcript, segmentation_config_for_preset(str(job.config.get("segmentation_preset", DEFAULT_SEGMENTATION_PRESET))), media_duration_ms=...)` 全文出现 5 次（`segmentation_config_for_preset` 的调用点在 :537、:561、:717、:1679、:1734）。两类副本都值得单独立项：对齐门的「QA error 是否参与」这条差异是真实的语义漂移，不是刻意差异的书面记录。

### O9 四处文档措辞 — **已修（本轮）**

逐条核实后改，以代码实况为准：

1. **AGENTS 批处理句**（属实）。`recovery.py:1192` 的 relink 导航要 `burned_output_relink_recovery_available` 为真，而该谓词（`workflow.py:372`）要求 `_burn_proof(job)` 非空且是 checksum 有效的 v2 proof；legacy proof 的 Job 在这里返回 `False`，落到后面的 `retryable` + `continue`，**批处理会自动重烧**。AGENTS 的「已登记烧录成品缺失时绝不自动重烧」因此比实现绝对，已改为「缺失**且具备重新关联资格**时绝不自动重烧；不具备资格的（例如只有 legacy fingerprint 的旧 Job）按 `retryable` 由批处理重烧」，与 `docs/RECOVERY.md#批处理恢复分类与中断兜底` 已有的措辞对齐。
2. **RECOVERY.md 的元数据口径**（属实）。本地源媒体在 `_burn_fingerprint`（`workflow.py:225`）里的身份是 `{path, size, mtime_ns}`，而该指纹是 `_burn_satisfied` 的放行条件之一，v2 proof 分支也只比对 `burned_video_checksum`、不回查源媒体内容——所以 size+mtime 在这里**确实起了证实作用**。原句「`_burn_fingerprint` 的 size+mtime 只参与身份指纹，不承担有效性判定」已删，换成一节「一处已知例外」，写明命中条件、为什么接受（源媒体不是登记产物、没有 checksum 基线；`relink` 写入前仍重算内容 checksum）以及用户的确证手段。原来的通则改为更准确的「所有**已登记 artifact** 的有效性只认内容 checksum」。
3. **模块表漏列**（属实，恰好 7 个）。`ls src/xvt/*.py` 对账后补齐 `config` / `stages` / `source_selection` / `errors` / `locking` / `cancellation` / `doctor`（`__init__.py` 不列）。
4. **`semantic_state` 描述**（属实）。该模块现在有 17 个顶层函数，除报告解析外还有批次重算与指纹、issue 的校验/去重/序列化、人工修订范围、Agent 结果解析、repair checkpoint 读取——「只读状态查询」偏窄，已改为「语义链路只读纯函数」并列出实际内容。

### O10 macOS CI 不装 ffmpeg — **已修（本轮）**

**首次改法在 CI 上失败，根因与最终改法记录在此**（这条的教训比修复本身值钱）。

第一版只给 macOS 加了 `brew install --quiet ffmpeg`，安全性论证是「本机 macOS 上这 4 条一直在真实跑并通过」。**这个论证是错的**：它只验了「`ffmpeg` 存在且测试通过」，没有比对装的是哪个 formula。

实际 CI 失败 6 条，报 `No such filter: 'ass'`。对照两边的 `ffmpeg -version` 才看清：

| | formula | prefix | `--enable-libass` |
| --- | --- | --- | --- |
| 本机 | **`ffmpeg-full`** | `Cellar/ffmpeg-full/9.0.2` | **有** |
| CI | **`ffmpeg`** | `Cellar/ffmpeg/9.0.2_1` | **没有** |

homebrew 把 ffmpeg 拆成了两个 formula，精简的 `ffmpeg` 不带 libass，而烧录与预览靠 `-vf ass=filename=...` 把字幕送进 filtergraph。

**最终改法有两处，缺一不可：**

1. **门控改成检查能力而不是检查可执行文件**。新增 `tests/ffmpeg_support.py::ffmpeg_can_render_ass()`：`ffmpeg`/`ffprobe` 都在，**且** `ffmpeg -filters` 里有整词 `ass`（按字段比对，避免 `assrender` 之类同前缀误判），结果按进程缓存。两处 `skipUnless` 改用它，跳过原因直接写明「homebrew users need the ffmpeg-full formula」。
   这一条独立于 CI 也该做：原门控把前置条件写错了，任何装了精简 ffmpeg 的用户跑测试都不是跳过，而是崩在 filtergraph 上。已用假 ffmpeg（有可执行文件、无 `ass` 滤镜）实测：4 条正确跳过并给出可操作原因。
2. **CI 装 `ffmpeg-full`**，并在装完**立刻断言滤镜到位**（`ffmpeg -filters | awk '{print $2}' | grep -qx ass`）。断言步骤刻意不加 `if`，Linux 与 macOS 都跑——少了它，哪天 formula 又变精简，集成测试会退回静默跳过而 CI 照样全绿，正是本条要修掉的状态。

pytest 步骤加 `-rs`，让 skip 原因出现在日志摘要里（「无人看 skip 数」的最低成本解）。

代价：macOS job 多一次 `brew install ffmpeg-full`（比精简版重，分钟级）；探测到滤镜已可用时跳过安装。

**可复用的教训**：「本机能过所以 CI 能过」只有在两边装的是同一个东西时才成立。验证跨平台依赖时要比对的是**能力**（滤镜/编解码器是否存在），不是**存在性**（可执行文件在不在）。

### O11 `shlex.quote` 的最后一处例外 — **已修（本轮）**

`cli.py` 的 `_cmd_status` 手拼双引号（`xvt status "{arguments.job_file}"`）是全仓唯一例外，路径带单引号、空格、`$` 或反斜杠时这条提示复制出来就不对，`$` 还会在 shell 里展开。改为 `shlex.quote`，与同文件其它 14 处同口径。

新增 `tests/test_cli.py::CliTest::test_status_hint_shell_quotes_job_paths_with_special_characters`：输出根命名为 `Tom's $HOME videos`，除比对期望串外还用 `shlex.split` 反解析该行，断言解析回来的第三个 token 逐字等于真实 job 路径（只比对字符串的话，换一种同样错误的转义仍可能蒙混）。另断言旧的手拼双引号形态不得回潮。红先自证：还原旧写法后该测试变红。

### O2 policy 回退两派语义造成术语死锁 — ✅ 已复现，**已修（本轮）**

**定性要改**：清单原本把它归为「可优化 / 结构隐患」，实测它是**可达的正确性 bug**，与 F2 同款的术语死锁，只是走另一条路径。

可达路径（已构造 Job 复现，不是推断）：

1. `WorkflowConfig.translation_policy` 是 `TranslationPolicy | None`，策略快照落地之前创建的 Job 解析出来就是 `None`，而 `workflow_config.py` 里**没有任何**把顶层键同步进快照的路径。
2. 这类 Job 上跑一次 `xvt translate --glossary G`：`prepare_agent_translation` / `run_translation` 都只写 `job.config["translation_policy"] = resolved_policy.to_dict()`，快照仍是 `None`。
3. 此后 `continue` → `_ensure_translation` → `workflow._translation_policy(config)` 回退 `TranslationPolicy()`，**译文不带术语**。
4. 同一个 Job 的译文 QA 走 `common.resolved_translation_policy(job)` → `required_glossary_terms` → `translation_qa_context.required_terms` → `check_subtitles`，**按顶层键要求术语**，判 `glossary_violation`（severity 为 error，QA 直接 `failed`，连 `--acknowledge` 这条出口都没有）。

红先证据：`tests/test_workflow.py::WorkflowTest::test_continue_translates_with_the_policy_the_qa_gate_requires` 在修复前失败于 `TranslationPolicy(style='faithful', terms=()) != TranslationPolicy(style='natural', terms=(GlossaryTerm('OpenAI','OpenAI'),))`，同一条测试里 QA 侧的 `required_glossary_terms` 断言则已经为真——两侧确实分叉。另独立验证 `check_subtitles(..., required_terms=(("OpenAI","OpenAI"),))` 对不含该术语的译文返回 `failed` / `glossary_violation`，死锁闭环成立。

修法：**四份合成一份**，`common.resolved_translation_policy(job, config=None)` 是唯一实现（快照优先、顶层键兜底；`config` 显式给出时优先采用，省略时从 Job 读回）。

- `workflow._translation_policy` 删除，两个调用点改为 `resolved_translation_policy(job, config)`。
- `semantic_state.resolved_policy` 删除（语义等价：`or` 对恒真的 `TranslationPolicy` 对象等同于 `is not None`），`semantic.py` / `semantic_state.py` 共 6 个调用点改为共享实现。
- `translation.translation_progress_rebuildable` 里的第四份（清单未点名，注释原文写着「与 `workflow._translation_policy` 同口径」）一并换掉：它重算 fingerprint 判断 checkpoint 能否零成本重建，口径一旦与执行侧漂移就会把可复用的 checkpoint 判成不可重建，整轮翻译白白重付一次 API 费用。
- `translation._resolved_translation_inputs`（F2 加的）**刻意不合并**：它解决的是「CLI 显式参数 vs 快照」的逐字段合并，与「快照缺失时回退到哪里」不是同一件事。

守卫：`WorkflowReusesPipelineHelpersTest::test_translation_policy_fallback_is_the_pipeline_implementation` 用对象身份钉住 workflow / translation / semantic / semantic_state 四处用的都是 `common` 那一份，并断言 `semantic_state.resolved_policy` 与 `workflow._translation_policy` 不得复活。

### O4 覆盖保护不保护未登记的用户文件 — ✅ 已复现，**已修（本轮）**

复现：构造 `job.input` 指向本地音频、`--media` 指向视频的 Job，`protected_job_paths(job, media, writing_kind="burned_video")` 只返回 `{video.mp4, job.json}`，原始音频不在其中，`assert_safe_output` 放行——`xvt burn JOB --media video.mp4 --output <原始音频>` 会让 `ffmpeg -y` 直接覆盖它。

修法：`paths.py` 新增 `_user_input_paths(job)`，把 Job 记着的用户原始输入文件并入保护集——本地 `job.input`、`job.config["subtitles"]`（`--subtitles` 的原件）、`workflow.translation_subtitles`（`--translation-subtitles` 的原件）。URL 形式的 `job.input` 由 `_local_path` 按 `urlparse` 的 http/https 判定跳过（与 `_burn_fingerprint` 同口径）。这些路径**不**按 `writing_kind` 排除：它们从来不是本次写入的目标。错误文案补上 `an original input file`（原有的 `source media` / `existing artifact` 字样保留，既有断言未改）。烧录与预览共用 `protected_job_paths`，两条路径同时受益。

### O6 v2 proof 登记不发事件 — **已修（本轮）**

修法：`_record_burn_fingerprint` 增加可选 `event_sink`（`_ensure_burn` 透传 workflow 的 sink），写盘后的登记从「手工 `job.artifacts.append` + `save_job`」改为调用 `pipeline.common.add_artifact`——顺带把这处绕开共享登记实现的旁路也收掉，checksum、同 kind 去重、落盘与 `artifact_created` 事件全部复用同一份。不新增 `EventType`；`docs/EVENTS.md` 的 `artifact_created` 节补了一段说明 `kind=burn_fingerprint` 的来源与「原子 `xvt burn` 不签发、因此没有这条事件」。

协议快照影响：**零 diff**。7 个场景全是纯字幕 Job（模块 docstring 即如此声明），不烧录，也不经过 `workflow._ensure_burn`；实跑 `tests/test_provider_protocol_snapshot.py` 确认 8 passed + 2 subtests。

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

### O7 三个守卫缺口 — **已修（本轮）**

**O7a `AWAIT_STEPS` 是手抄清单**。新增 `tests/test_workflow.py::AwaitStepsCoverageTest`：ast 解析 `plan_advance_steps`，从各条 `return`（递归进元组）与 `steps.append(...)` 两条出路收集字符串字面量，凡以 `await_` 开头的都必须在 `workflow.AWAIT_STEPS` 里。**双向都核**——`AWAIT_STEPS` 的 docstring 本来就声称它是「planner 可能返回的全部等待步骤名」，所以反向也不许有死条目；另有一条非空断言防守卫空转。收 `append` 这条路是刻意的：只看 `return` 的话，把步骤名改成先 append 再 `return tuple(steps)` 就能绕过守卫。自证：给 planner 临时加 `return ("await_xxx",)` 与 `steps.append("await_appended")` 两种形态，守卫各自变红（验完删除）。

**O7b 全仓无 subprocess / shell 守卫**。新增 `tests/test_subprocess_boundary.py`，四条 ast 守卫：① `src/xvt/` 下除 `process.py` 外不得 import `subprocess`；② 反向钉住 `process.py` 真的在用 `subprocess`（否则例外声明空转，守卫自己先烂掉）；③ 全仓**含 `process.py`** 不得出现 `shell=True` 关键字实参——参数数组是唯一执行形式，这条没有例外，`process.py` 的例外只针对 import；④ 不得调用 `os.system` / `os.popen`，它们只收 shell 字符串，只禁 `subprocess` 会把这条后门留着。用 ast 而非文本扫描，续行写法与注释/字符串字面量都影响不了判定；`setUp` 里有扫描文件数下限断言，防止扫描根指错后静默空过。自证：`doctor.py` 临时 `import subprocess`、`process.py` 的 `Popen` 临时加 `shell=True`、`doctor.py` 临时加 `os.system(...)`，三种变异各自只打红对应那一条（验完删除）。AGENTS 不可破坏约束 6 的措辞已同步写明这条守卫。

**O7c `checksum=None → warn` 分支无测试**。清单的说法**属实，已独立复验**：把 `recovery.verify_artifact` 的 `"warn"` 改成 `"pass"`，全量 915 条 + 235 subtests 仍然全绿。新增 `tests/test_recovery.py::RecoveryTest::test_verify_job_warns_on_artifact_without_checksum`，除了钉住 verify 的三元结果（`warn` 不升级为 `fail`、`warning_count == 1`、message 与 `expected_checksum: null`），还钉住**下游后果**：`valid_artifact_path` 对这种 artifact 必须返回 `None`。后者才是真正的风险面——`verify_artifact` 同时是 `valid_artifact_path` 的判定来源，一旦退化成 `pass`，一个从未登记过内容 checksum 的文件就会被恢复链路当成有效产物复用，违反不可破坏约束 9。

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
3. ~~**N1 设计决策**：先定「确认是否绑定译文内容」，再动手。~~ 已定并已修（本轮）：确认绑定译文内容，载体是记录里早就落盘、只是没被读过的 `report_kind` / `report_checksum`。
4. ~~**烧录判定收敛与 fail-closed 补洞**：F6 + O5 + F7 + O7。~~ 四条均已修：F6 / O5（上一轮，四阶段：改桩 → 真实状态测试 → 口径收敛 → 终检守卫），F7 / O7（本轮，与 F5 同一批「守卫与去重」）。
5. ~~**去重**：F5。~~ 已修（本轮）。
6. ~~之后再谈 O1（Phase 5 轻量替代）与 O2（并入 R3.4，必须排在 Q7 之前）。~~ O2 已修（本轮，连同 O4 / O6 一批）；O1 仍未做。

**剩余未处理**：O1，O8 里「对齐/分段规则 3–4 份副本」的结构性重构（核实属实，定位见 O8 条目），以及「观察」与「未验证」两节。O9 / O10 / O11 与 O8 的死码部分已修（本轮）。

对既有计划的建议（来自 review，主干部分认同）：**Phase 5 降级**——做完 O1 后剩余差异是「API 逐批 checkpoint」与「Agent 一次性回填」的协议本质不同，硬合一份编排风险高收益小；**R3.2 延后、大概率取消**——它要解决的规划器/执行器对齐问题可用上述低成本替代，完整缓存框架与 AGENTS 第 6 条冲突。这两条是建议，未拍板。
