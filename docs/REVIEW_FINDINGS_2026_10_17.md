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

### F1 `stage.error` 裸写进 `job.json`，绕过脱敏 — ✅ 已核验，待修

`pipeline/common.py` 的 `set_stage` 里 `record.error = error` 后直接 `save_job`；同一函数的事件分支走 `error_event_fields` 是脱敏的，持久化分支不是。`job_store.py` 全文无 `redact`。约 18 个调用点传 `error=str(error)`，而 `CommandError` 内嵌 yt-dlp / ffmpeg 原始 stderr（`process.py`），403 分支原样拼 detail（`download.py`），翻译 4xx 带 base_url（`translation_provider.py`）。

后果：签名 URL、Cookie 路径、代理凭据明文落盘，`status --json` 原样输出。**AGENTS 不可破坏约束第 5 条「任何会持久化的错误文本都必须先脱敏」目前在 job.json 层不成立。**

修法：`set_stage` 入口统一 `redact_text(error)`（1 行）+ 一条「job.json 不含签名/Cookie」的落盘断言测试。工作量：小时级。

### F2 端到端 Job 上 `xvt translate` 丢持久化 policy 与 batch_size — ○ 已复现，待修

`pipeline/translation.py` 的 `resolved_policy = policy or load_translation_policy(glossary, ...)` 从 CLI 参数现算，不回退 `config.workflow` 快照（`base_url` / `model` 有回退，policy / batch_size 没有）。

触发：`run --glossary ...` 之后执行 `xvt translate JOB --target-language L`（恰是恢复计划里「放弃重译」的推荐命令）→ 译文不带术语 → 译文 QA 按 workflow policy 报 `glossary_violation` 死锁回去；API 模式下指纹漂移触发 progress reset，**全部批次重付一次翻译费**。

修法：有 workflow 且未显式传参时回退 workflow 值，与 base_url/model 同口径。约 5 行 + 测试。

### F3 Agent manifest 失效时端到端 Job 卡死 — ○ 已复现，待修

`workflow.py` 的 planner 在 `translation_batches` manifest 无效时仍返回 `await_agent_translation`；对照 `recovery.py` 的原子分支已有防御。结果三命令互指：`status` 说等回填，`apply` 被 manifest 校验拒绝，`continue` 只发 waiting，没有出口。

修法：manifest 无效时 planner 返回 `translate`，`_ensure_translation` 同步补条件。约 5 行 + 测试。

### F4 原子 Job 的 `qa_blocked` 不给在途 Agent 等待让路 — ○ 已复现，待修

`recovery.py` 的原子分支缺少端到端分支已有的让路判断。触发：原子 Job 术语死锁后改跑 `translate --provider agent`，`status` 仍推荐 `retranslate-cues`，照做会盖掉整轮 manifest。这是端到端侧已修问题的同构遗漏。

### F5 `workflow.py` 逐字复制 pipeline 的 3 个函数 — ✅ 已核验，待修

`workflow.py` 的 `_source_selection_satisfied` 与 `pipeline/transcription.py` 的 `_source_selection_matches` 逐行相同（仅调用名差异），另两处复制自 `pipeline/common.py`。违反 AGENTS「`workflow.py` 只负责编排，不应复制 pipeline 包的算法」。

风险：以后给 `selected_kind` 加新分支只改一边，`continue` 与 `resume` 对「源是否满足」的判定分叉。修法：workflow 改为引用 pipeline（把源选择函数公有化 + re-export）。无测试桩阻碍。

### F6 烧录满足度 3+1 份口径，且两道媒体 QA 条件零测试覆盖 — ○ 已复现，待修

架构侧：`workflow.py` 三处各写一遍「媒体 QA 过 + proof 匹配」，`recovery.py` 是第四种口径。测试侧（变异实证）：`_burn_satisfied` 的 stage 判断与 `media_qa.status == pass` 判断**一起删掉，全量测试仍全绿**——根因是唯一相关测试把 `valid_artifact_path` 打了桩，「media_qa 已登记且 failed」这个真实状态从未被构造过。

放过的 bug：legacy Job 原子重烧后媒体 QA 失败，`continue` / `status` 判已完成（违反约束 4）。

修法：先补一条不打桩的真实状态测试（两道条件各一个 subTest），再抽 `_media_qa_passed` 与 proof 比对 helper，三处保留各自刻意的差异（`status` 不哈希源媒体、relink 哈希）。工作量：半天级。

### F7 人工修订预检的 error 级阻断无测试 — ○ 已复现，待修

`pipeline/revision.py` 的 error 级阻断删掉后全量仍绿；现有测试只覆盖行宽阻断。放过的 bug：修订删掉术语表译名 → `glossary_violation`（error）→ `apply --check` 报 `valid: true`，正式 apply 落盘，违反「写入前重复预检」契约（下游 QA 门仍会拦烧录，但修订已经写进 Job）。补一条 glossary 用例即可。

### F8 签名参数脱敏名单不全 — ○ 已实测，待修

`events.py` 的拒绝名单漏掉 GCS v4 的 `X-Goog-Signature` / `X-Goog-Credential` 与阿里云 `auth_key`（实测原样保留）；腾讯 `q-signature`、Akamai `hdnts` 为推断。与 AGENTS「签名 URL 不得原样写入事件」不符。主用例是 YouTube，触发概率偏低。

修法：名单改「精确键 + 子串规则」（键名含 signature / credential / token / auth 即脱敏）+ 参数化测试。

### F9 文档进度陈述过期 — ✅ 已核验，**部分已修（PR #97）**

HANDOFF 里「下一步是 Phase 4c」那句已在 PR #97 修正。**剩余**：`ROADMAP.md` 只承认 Phase 1 完成，未反映 Phase 2/4a/4b/4c；`src/xvt/errors.py` 的注释称锁「尚未落地」而 `locking.py` 已在使用。分钟级。

## 可优化

| 编号 | 内容 | 判断 |
| --- | --- | --- |
| O1 | `semantic.py` 三处 12 行逐字相同的终态收口 + 两处问题分拣重复，抽 `_conclude_semantic_review` / `_partition_issues` 两个纯搬移 helper | **值得做**，半天；是 Phase 5 的轻量替代，能吃掉其大部分收益 |
| O2 | policy 回退有 4 份、两派语义（`common.py` / `semantic_state.py` 回退顶层键，`workflow.py` / `translation.py` 回退默认值） | **值得做**，F2 正是这条结构隐患的现实案例；并入 R3.4，必须排在 Q7 之前 |
| O3 | `translation_provider.py` 的 `resolve_provider` 不拒绝带 userinfo/query 的 base_url；实测 `user:pw@host` 的凭据经 `InvalidURL` 消息泄露（消息不含 scheme，`redact_text` 的 URL 正则不命中）且随 `config.workflow` 永久落盘 | **值得做**，几行；与 F1 同族，建议一起修 |
| O4 | `paths.py` 的覆盖保护只认已登记 artifact；纯音频 Job 执行 `burn --output <job.input 的路径>` 会被 `ffmpeg -y` 静默覆盖原始媒体 | **值得做**，几行；后果是用户数据丢失。保护集加入本地 `job.input` 与用户原始字幕路径 |
| O5 | `_advance_workflow` 发 `workflow_completed` 前无终检守卫 | **值得做**，1 行：断言 `plan_advance_steps() == ()`，可兜住 B1 同类误报 |
| O6 | `_record_burn_fingerprint` 登记 v2 proof 不发事件，与约束 7 口径不一；proof 是 relink 的唯一授权凭据却在 JSONL 不可见 | 可做，几行；落在协议快照的已知盲区（烧录链路）内 |
| O7 | 三个守卫缺口：`AWAIT_STEPS` 是手抄清单，缺「planner 返回的 `await_*` ⊆ `AWAIT_STEPS`」的 AST 守卫；全仓无「禁 `shell=True` / subprocess 必须走 `process.py`」守卫；`recovery.py` 的 `checksum=None → warn` 分支无测试（改成 pass 全绿） | 各约半小时 |
| O8 | `transcription.py` 两处首跑不可达分支可直接删（零行为变化）；对齐/分段规则有 3–4 份副本且已漂移；旧 Job 路径冻结即可，突破 2000 行再按「本地转写 / 远程获取 / resume 与 rebuild」三簇拆 | 部分值得做 |
| O9 | 文档措辞：AGENTS「烧录成品缺失一律停 `awaiting_input`」比实现绝对（legacy proof 实为 retryable 会自动重烧，RECOVERY.md 才是准确口径）；RECOVERY.md「size+mtime 不承担有效性判定」不够准确（经指纹在 `_burn_satisfied` 实际起了证实作用：源媒体被同 size+mtime 的文件替换不会触发重烧，风险低但应如实写明）；AGENTS 模块表漏列 `errors` / `locking` / `cancellation` 等 7 个模块 | 值得做，分钟级 |
| O10 | macOS CI 不装 ffmpeg，主力平台的真实烧录与字体集成测试全部静默 skip，只有 Linux 覆盖，且无人看 skip 数 | 值得评估 |
| O11 | `cli.py` 一处 hint 用双引号包路径而非 `shlex.quote`（全仓唯一例外点） | 几分钟 |

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

1. **正确性剩余**：F2 + F3 + F4 + O5。全是几行级改动加回归测试，修的是术语死锁、Job 卡死、重复付 API 费——用户可感知。
2. **持久化脱敏收口**：F1 + F8 + O3。约束 5 在 job.json 层目前是空话，一个小 PR 能收口。
3. **N1 设计决策**：先定「确认是否绑定译文内容」，再动手。
4. **烧录判定收敛与 fail-closed 补洞**：F6 + F7 + O7。
5. 之后再谈 O1（Phase 5 轻量替代）与 O2（并入 R3.4，必须排在 Q7 之前）。

对既有计划的建议（来自 review，主干部分认同）：**Phase 5 降级**——做完 O1 后剩余差异是「API 逐批 checkpoint」与「Agent 一次性回填」的协议本质不同，硬合一份编排风险高收益小；**R3.2 延后、大概率取消**——它要解决的规划器/执行器对齐问题可用上述低成本替代，完整缓存框架与 AGENTS 第 6 条冲突。这两条是建议，未拍板。
