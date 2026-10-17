# 项目交接说明

更新时间：2026-10-17

2026-10-17 真实 E2E 验收抓到的 P0 已修（整轮 Agent 翻译等待被「译文目标已满足」吞掉），外加一处文档口径冲突。**复现路径**（验收在三个 Job 上复现，不是构造的 advanced 用法）：Job 停在局部重译等待 → `status` 自己提示「重新运行一轮完整翻译即可取代它」→ 照做 `xvt translate JOB --target-language zh-CN --provider agent`（EXIT 0，写出新 `translation-batches.json`，`stages.translate=awaiting_input`）→ `status` 第一行显示 `translate=awaiting_input` 而恢复计划报 `complete`、下一步给 `xvt verify` → `continue` 报「任务处理完成」EXIT 0 → `xvt apply JOB RESPONSE` EXIT 2「the job is not awaiting Agent input; run xvt status」。导航闭环断掉，唯一出路是自己猜到 README 里的 `apply-translation`。**根因**：`plan_advance_steps` 里 `await_agent_translation` 的**全部** return 分支都埋在 `if goals.needs_translation and not _translation_satisfied(job, config):` 里，旧译文仍 checksum 有效时根本不会被求值——与函数体最前那条 `await_cue_retranslation` 早退（注释里写明了同一条理由）完全不对称。这是同一类缺陷的第四次（前三次：`qa_blocked` 给在途等待让路、F3 manifest 失效死局、F4 原子分支让路）。**修法**：`workflow.py` 新增单点谓词 `agent_full_translation_waiting`（`TRANSLATE` 停在 `awaiting_input` + `translation_batches` checksum 有效，planner 两处分支共用，manifest 失效时判否以保住上一轮「给 `translate` 重建」的修复）与 `_full_translation_request_is_newest`（按 `job.artifacts` 登记次序比较整轮 manifest 与 `cue_retranslation_batches` / `translation_polish_batches` / 三份语义报告，与 `pipeline.retranslate._newer_full_translation_request` 同判据、方向相反）；在 `cue_retranslation_waiting` 早退之后、`_translation_satisfied` 之前新增对称早退。满足度只算一次（提成局部变量 `translation_satisfied`），避免在没有只读记忆化作用域的 `continue` 路径上把四五个产物哈希两遍。**各类等待的优先级**：① `await_cue_retranslation`（函数体最前，谓词严格，`FAILED` 态额外让位更新的整轮请求）→ ② 整轮 Agent 翻译等待（满足度之前，要求整轮 manifest 是最后登记的在途请求）→ ③ 满足度之后、`not translation_satisfied` 分支内的语义 → 润色 → 整轮既有分支。三侧自动一致：`status` 的 `_recovery_for_workflow`、`continue` 的 `_advance_workflow` 入口等待判定、`xvt apply` 的 `_STEP_ROUTES` 都读同一份 `plan_advance_steps`，`batch-continue` 读持久化的 `recovery_plan` 分类。**让这一类更难复发**：新增 `workflow._SATISFIED_GATED_AWAIT_STEPS` 表态表（语义/润色/人工 review 三条刻意只在「未满足」分支里判，理由写在注释里）＋ `AwaitStepsCoverageTest` 第三条 AST 守卫——planner 里每个 `await_*` 要么出现在满足度闸门**之外**，要么登记进该表，两边对不上就红；做过变异自证（把新早退删掉即红在「未表态的 `['await_agent_translation']`」）。**P1 判断：改文档不改代码。** `xvt burn JOB --media <损坏 mp4>` 把 `stages.render` 记成 `failed`（EXIT 3）与 `docs/RECOVERY.md`「本地替代媒体或字体导致布局失败时 Job 不变」看似冲突，但依据有三：原子 `burn` 成功时本来就推进 `render` / `burn` / `media_qa` 三个 stage，所以「原子命令不写 stage」不是契约；文档那句的真实适用范围是**布局 QA 失败**，而该路径抛 `UserInputError`、`burn_translated_job` 的兜底刻意 `except UserInputError: raise` 不记 stage（`tests/test_pipeline_burnin.py` 的 `persist_workflow_qa=False` 用例已钉住「不覆盖 QA 报告/stage」）；损坏文件是 `probe_media` → `CommandError` 落到 `except Exception`，属另一类失败。文档已按「只覆盖布局 QA 一类失败」改写并写明其它失败照旧按 stage 记账、不影响恢复判定。**红先验证**（备份源码、未执行任何 git 命令）：3 条新测试红在 `Tuples differ: () != ('await_agent_translation',)` 与 `ExitCode.USER_ERROR: 2 != 0`（stderr 逐字复现「response file has no task field, and the job is not awaiting Agent input」＋「恢复建议：当前目标已完成」）。修后第 3/4/5 步变成：`status` 归 `awaiting_input`、首选 `continue`、前置给可复制的 `xvt apply JOB RESPONSE`、备选 `apply-translation --target-language zh-CN`；`continue` 返回 `awaiting_input` 并只发一次 `workflow_waiting`、不发 `workflow_completed`；`xvt apply JOB RESPONSE` EXIT 0 并写入新译文。**协议快照零 diff**（8 passed + 2 subtests），**既有测试零变红、既有断言零改动**。新增 5 条测试，936 → 941，子测试仍 235。遗留（如实记录）：已满足的 polished Job 上**显式重开** `polish-translation` 或语义校对的 Agent 等待仍会被报成空计划（登记在表态表里，已写进 `docs/RECOVERY.md` 的「已知遗留」一节，无 E2E 复现，本轮未修）；新判定依赖 `job.artifacts` 的登记次序，手工编辑 `job.json` 重排 artifact 会改变等待归属；P1 那条「原子 `burn` 失败留下的 `render=failed` 永久留在 `job.json` 里」只在下一次成功烧录时才被推回 `succeeded`，刻意未加清理。

2026-10-17 N1 已修（清单里最后一条「需设计决策」的条目，安全性修复）。**生产侧**：`pipeline/review.py` 的 `acknowledged_review_issues` 此前只比对 `source_cues_checksum`，于是「为 v1 译文确认过 cue 3 行宽超限」会自动代签一条内容完全不同、恰好同样在 cue 3 超宽的 v2 译文，用户从没看过它，四条译文回填路径（agent / import / retranslate / semantic 收尾）都暴露。**关键事实：`report_kind` / `report_checksum` 自确认通道引入（`d361828`）起就一直落盘，只是校验侧从来没读过它们**——所以修法是补校验而不是改落盘格式，真实 Job 的存量确认记录照常有效，只有测试里手搓的 fixture 缺字段。防御上显式排除 `report_checksum is None`，否则报告未登记时 `artifact_checksum_for` 返回 `None`，一条写着 `report_checksum: null` 的伪造记录会因「两边都是 `None`」蒙混过关。**测试侧**：新增 2 条——`test_acknowledgment_does_not_sign_off_a_different_translation`（同一 Job、同一 cue、同一个 review 问题码 `bilingual_overflow_risk`，只换译文内容，断言旧确认失效且新报告不放行）与 `test_acknowledgment_rejects_unregistered_report`（`report_checksum: null` 的伪造记录），两条都做过红先验证（备份源码临时去掉校验即复现修复前的代签行为）。修掉 6 条变红：5 条是手搓确认记录缺 `report_kind` / `report_checksum`（`test_pipeline.py` 1 条、`test_pipeline_burnin.py` 的 `_acknowledge_review_issues` helper 带掉 2 条含 4 subtest、`test_revision_preflight.py` 3 条），**纯补 setup 字段，断言一字未动**；第 6 条 `test_workflow.py::test_continue_rebuilt_qa_keeps_stored_review_acknowledgment` 是 helper `_standard_job_with_translated_outputs` 用紧凑 JSON 写 QA 报告（311 字节）、与生产 `pipeline.common.write_json` 的缩进 JSON（394 字节）**解析后逐字段相同但字节不同**，改为调用 `write_json`——改完这条才真正在验它声称要验的东西，此前它即使在生产路径上会失效也照样绿。顺带把 `test_pipeline.py` 另两条本就绿的确认测试（bool cue id、新增 review 问题）也补齐字段，免得它们因「记录格式不全」而不是想验的原因变绿。**全仓有没有别的 fixture 手写 QA 报告 JSON**：用「临时把 `write_json` 的 `indent=2` 改成 `indent=3` 跑全量」做了实证探测，除 `tests/test_provider_protocol_snapshot.py`（它本就冻结 artifact 内容 checksum，预期内）外**零额外变红**，说明没有第二处 fixture 与生产序列化字节耦合；其余 ~30 处手写的 QA JSON 都是从不被生产重建的静态桩（多为 `{"status":"pass","issues":[]}`），刻意不动。**协议快照零 diff**（8 passed + 2 subtests）。新增 2 条测试，934 → 936，子测试仍 235。已知性质（写进 review 清单）：绑的是报告**字节** checksum 而非语义等价，将来改 `write_json` 的序列化写法会让全部存量确认一次性失效，方向 fail-closed 故接受。遗留：`report_kind` 的取值白名单原本在校验侧与写入侧各写一份字面量，验收时已提成共享常量 `ACKNOWLEDGEABLE_QA_KINDS`（两处同源，新增报告种类只需改一处）。

2026-10-17 macOS CI 集成测试修复：给 macOS 加 ffmpeg 后 CI 报 `No such filter: 'ass'`。根因是 homebrew 把 ffmpeg 拆成两个 formula——精简的 `ffmpeg` 不带 `--enable-libass`，而烧录/预览靠 `-vf ass=filename=...` 送字幕；本机装的是 `ffmpeg-full` 所以一直绿，「本机能过所以 CI 能过」的论证只验了可执行文件存在、没比对 formula。两处同时修：`tests/ffmpeg_support.py` 把门控从「有 ffmpeg」改成「有 `ass` 滤镜」（任何装精简版的用户此前都会崩在 filtergraph 上而不是跳过），CI 改装 `ffmpeg-full` 并在装完断言滤镜到位（断言两个平台都跑，防止将来退回静默跳过）。

2026-10-17 O8 + O9 + O10 + O11 已修（清单「可优化」的最后一批，四条互相独立）。**O8（死码）**：`run_local_transcription`（原 :824）与 `run_remote_transcription`（原 :1062）的 `if "source_strategy" in job.config` 确认恒真——两处都在同一函数体内用字面量 config 创建 Job，字典里固定写着这个键（参数默认 `"auto"`，永不为 `None`），`create_job` 只做 `dict(config or {})` 不剔键。**旧 Job 走不到这两处**（它们只创建新 Job、从不 `load_job`）；旧 Job 唯一会撞上同款判定并分流到 `_process_acquired_media` 的入口是 `resume_remote_transcription`，那里 `job` 来自 `load_job`，判定**原样保留**，同文件另外 5 处 `"source_strategy" in job.config` 同理未动。`_process_acquired_media` 因此仍被调用，函数保留。零行为变化、无新增测试（没有可观测差异可断言）。清单附带的「对齐/分段规则 3–4 份副本且已漂移」**核实属实但本轮不动**（结构性重构）：三份对齐门在 `_process_adaptive_media`（:488）/ `_process_acquired_media`（:696）/ `resume_job`（:1653），真实漂移是「对齐后 QA error 门」只有第一份有，失败行为也分成自动回落 ASR、抛 `ValueError`、`set_stage(FAILED)` 后抛三种；分段侧同一串 `segmentation_config_for_preset(job.config.get(...))` 出现 5 次。定位已写进 review 清单供后续立项。**O9（文档四处）**：逐条核过代码才改——① AGENTS 的批处理句改为「缺失**且具备重新关联资格**时绝不自动重烧，不具备资格的（legacy fingerprint）按 `retryable` 重烧」，依据是 `burned_output_relink_recovery_available` 要求 checksum 有效的 v2 proof、legacy 在那里返回 `False`；② `docs/RECOVERY.md` 删掉「`_burn_fingerprint` 的 size+mtime 不承担有效性判定」，换成一节「一处已知例外」如实写明本地源媒体在指纹里的身份是 `{path, size, mtime_ns}`、而该指纹是 `_burn_satisfied` 的放行条件之一，所以同 size+mtime 的替换文件不会触发重烧，并写明接受理由与确证手段；③ 模块表补齐 `ls src/xvt/*.py` 对账出的 7 个（`config` / `stages` / `source_selection` / `errors` / `locking` / `cancellation` / `doctor`）；④ `semantic_state` 改为「语义链路只读纯函数」（17 个顶层函数，不止报告解析）。**O10（CI）**：先摸影响面才改——全仓 ffmpeg 门控只有 `tests/test_e2e.py::EndToEndTest`（3 条）与 `tests/test_burn.py::BurnFilterPathSmokeTest`（1 条 + 4 subtest），没有独立字体门控；本机 macOS 装了 ffmpeg，`pytest -q -rs` 的 **skip 数为 0**，这 4 条一直真实在跑并通过；它们都显式 `hardware_encoding=False`（libx264，不依赖 VideoToolbox），homebrew bottle 带齐 libx264 / aac / ass，因此判断加装 ffmpeg 变红风险低。`ci.yml` 的 Linux 步骤改名 `Install ffmpeg (Linux)`，新增 `Install ffmpeg (macOS)`（`brew install --quiet ffmpeg` + `HOMEBREW_NO_AUTO_UPDATE` / `HOMEBREW_NO_INSTALL_CLEANUP`），pytest 步骤加 `-rs` 让 skip 原因在 CI 日志可见。代价是 macOS job 多一次分钟级的 brew 安装。**O11**：`cli.py` 的 `_cmd_status` 手拼双引号（全仓唯一例外）改 `shlex.quote`，新增测试用 `Tom's $HOME videos` 作输出根，除比对期望串外还 `shlex.split` 反解析断言第三个 token 逐字等于真实路径，并断言旧形态不得回潮；红先自证（还原旧写法后变红）。**协议快照零 diff**（8 passed + 2 subtests），**既有测试零变红、既有断言零改动**。新增 1 条测试，933 → 934，子测试仍 235。CHANGELOG 只记 O11（唯一用户可见变化）；O8 零行为变化、O9 纯文档、O10 只动 CI，刻意不记。遗留（如实记录）：O10 的「加装 ffmpeg 后 CI 仍绿」只有本机 macOS 的证据，**真实 CI 跑绿尚未验证**，合并后要看一次 macOS job；`brew install ffmpeg` 没有缓存，每次 macOS job 都重装；O8 的死码删除没有测试守住「将来有人再加一条恒真分支」。下一项按清单仍是 N1 的设计决策，以及 O1 与 O8 的副本重构。

2026-10-17 O2 + O4 + O6 已修（三条互相独立）。**O2 的定性要改——它不是整洁问题，是可达的正确性 bug**：构造「workflow 快照无 `translation_policy`、顶层 `job.config["translation_policy"]` 有术语」的 Job 后实测确认，翻译侧拿到 `TranslationPolicy(style='faithful', terms=())`，而译文 QA 侧的 `required_glossary_terms` 同时返回 `(('OpenAI','OpenAI'),)`——两侧确实分叉；独立验证 `check_subtitles` 对不含该术语的译文返回 `failed` / `glossary_violation`（error 级，连 `--acknowledge` 出口都没有），死锁闭环成立。这种 Job 真实可达：`WorkflowConfig.translation_policy` 对策略快照落地之前创建的 Job 解析为 `None`，`workflow_config.py` 里**没有任何**把顶层键同步进快照的路径，而 `xvt translate --glossary G` 只写顶层键。修法是**四份合成一份**：`common.resolved_translation_policy(job, config=None)` 为唯一实现（快照优先、顶层键兜底，`config` 显式给出时优先采用），`workflow._translation_policy` 与 `semantic_state.resolved_policy` 删除，`translation.translation_progress_rebuildable` 里清单未点名的第四份（注释原文「与 `workflow._translation_policy` 同口径」）一并换掉——它重算 fingerprint 判 checkpoint 能否零成本重建，口径漂移会把可复用的 checkpoint 判成不可重建、整轮重付 API 费用。`translation._resolved_translation_inputs` 刻意未合并（它解决「CLI 参数 vs 快照」的逐字段合并，不是同一件事）。**O4**：`paths.py` 新增 `_user_input_paths`，把本地 `job.input`、`job.config["subtitles"]`、`workflow.translation_subtitles` 并入覆盖保护集（URL 形式的 `job.input` 按 `urlparse` 的 http/https 跳过，与 `_burn_fingerprint` 同口径），这些路径不按 `writing_kind` 排除；错误文案补 `an original input file`，原有 `source media` / `existing artifact` 字样保留因此既有断言未改。烧录与预览共用同一保护集。**O6**：`_record_burn_fingerprint` 增加可选 `event_sink`（`_ensure_burn` 透传），登记改走 `pipeline.common.add_artifact`——顺带收掉这处绕开共享登记实现的旁路，checksum / 同 kind 去重 / 落盘 / `artifact_created` 事件全部复用一份；不新增 `EventType`。红先证据齐全：O2 的行为测试红在 policy 不相等、身份守卫红在 `AttributeError`；O4 红在原始音频不在保护集；O6 红在 `artifact_created(kind=burn_fingerprint)` 事件数 0 != 1。**协议快照零 diff**（7 个场景全是纯字幕 Job，不经过 `_ensure_burn`），**既有测试零变红、既有断言零改动**。新增 5 条测试，928 → 933，子测试仍 235。文档：CHANGELOG 记了 O2 / O4 / O6 三条（都是用户可见变化），`docs/EVENTS.md` 的 `artifact_created` 节补了 `kind=burn_fingerprint` 的来源说明，review 清单三条改标「已修（本轮）」并改写了 O2 的定性。遗留（如实记录）：身份守卫只能拦「同名副本」，换个名字再抄一遍仍拦不住；O4 只覆盖 Job 里**记着**的用户文件，用户在 Job 之外的任意文件仍可被 `--output` 指中（这是 CLI 显式输出路径的固有语义，未扩大保护面）；`job.input` 为非 http/https scheme（如 `ftp://`）时按本地路径处理，不会误拦但也不精确。下一项按清单仍是 N1 的设计决策。

2026-10-17 F5 + F7 + O7 已修（同一批「守卫与去重」，零生产行为变化）。**F5（去重）**：`pipeline/transcription._source_selection_matches` 公有化为 `source_selection_matches`（R1.4 先例），与 `common.artifact_checksum_for` / `common.error_category` 一起经 `pipeline/__init__` re-export；`workflow.py` 删掉三份逐字副本改为引用。清单只点名「另两处复制自 `common.py`」，本轮逐字比对确认第三处就是 `_artifact_checksum` ≡ `common.artifact_checksum_for`（在 `record_media_download_selection` 正上方）。行为零变化（`"none"` vs `SourceCandidateKind.NONE` 是 `StrEnum`，相等）。清单所称「无测试桩阻碍」不完全准确：`tests/test_pipeline_revision.py` 有一处**直接 import**（非 `patch()` 桩）`from xvt.workflow import _source_selection_satisfied` 加 3 个调用点，已同步改名、仍从 `xvt.workflow` 导入，三条断言一字未改。`tests/test_pipeline_imports.py` 白名单无需改动（没有新增包内依赖边）。**F7（补测试）**：`tests/test_revision_preflight.py` 新增 `_glossary_response` 与两条用例，走真实 QA（术语表 → workflow 快照 → `translation_qa_context.required_terms` → `check_subtitles`）不打桩绕过判定；阻断用例刻意用**短**译文，使行宽那一支不可能命中，唯一能让预检变红的就是 error 级分支，并断言预检只读 + 正式 apply 抛错不落 `translation_reviewed_cues`。**O7a**：`AwaitStepsCoverageTest` 用 ast 从 `plan_advance_steps` 的各条 `return`（递归进元组）**与** `steps.append(...)` 收字符串字面量，`await_*` 与 `AWAIT_STEPS` 双向不许有差集（只看 `return` 的话，改成先 append 就能绕过）。**O7b**：新增 `tests/test_subprocess_boundary.py` 四条 ast 守卫——除 `process.py` 外禁 import `subprocess`、反向钉住 `process.py` 真在用它、全仓含 `process.py` 禁 `shell=True`、禁 `os.system` / `os.popen`；AGENTS 不可破坏约束 6 的措辞已同步。**O7c**：清单「改成 pass 全绿」的说法**独立复验属实**（915 条全绿），新增测试除钉住 verify 三元结果外，更钉住下游后果——`valid_artifact_path` 对无 checksum 的 artifact 必须返回 `None`（这才是违反约束 9 的真实风险面）。红先/变异自证齐全：F5 两种形态（名字缺席 → `AttributeError`；重新复制同名副本 → `assertIs` 失败）各红 3 / 2 条；F7 删掉 error 级分支后全量 927 条里**恰好只有新增那一条**变红；O7a 两种注入形态（`return` 与 `append`）各自变红；O7b 三种变异（`doctor.py` import `subprocess`、`process.py` 加 `shell=True`、`doctor.py` 加 `os.system`）各自只打红对应那条；O7c 变异后新测试红。协议快照零 diff，**既有测试零变红、既有断言零改动**。新增 12 条测试，915 → 927，子测试仍 235。CHANGELOG 未记：三条都是内部去重与测试/守卫，无任何用户可见行为变化。遗留（如实记录）：`batch.py:265` 还有一份与 `common.error_category` 逐字相同的 `_error_category`，不在本轮 F5 的点名范围（清单只说 workflow.py），未动；F5 的身份守卫只能拦「同名副本」，有人换个名字再抄一遍仍拦不住。下一项按清单是 N1 的设计决策。

2026-10-17 F6 + O5 已修（四阶段，**第一步是改测试而不是改生产代码**）。**阶段 A**：`tests/test_workflow.py` 里 7 处（含 1 处 3 个 subtest）把 `run_translation` / `apply_subtitle_translation` / `burn_translated_job` / `valid_artifact_path` / `_record_burn_fingerprint` 打成「不推进 Job 的假实现」却断言 `completed` 的测试，全部改成构造真实状态。新增三个模块级 helper：`_write_source_leg_artifacts`（落 `source_cues` / `source_srt`，取代整体 patch `valid_artifact_path`）、`_complete_translation_artifacts`（译文 cue + 真实 `save_subtitles` 两份 SRT + 真实 `check_subtitles` QA 报告，推进 TRANSLATE / SUBTITLE_QA / RENDER）、`_complete_burn_artifacts`（成品 + `media-qa.json` pass，推进 BURN / MEDIA_QA）。带 burn 的 Job 改用真实存在的本地媒体文件，`_record_burn_fingerprint` 因此走真实实现登记 v2 proof；`run_transcription` 的桩补上「把 workflow 目标固化进 Job」这一真实副作用（缺它 proof 登记会静默跳过，是阶段 A 唯一一次卡住的地方）。**既有断言一字未改**，只改桩的伪造方式；用临时断言逐条验过 7 处结束时 `plan_advance_steps` 为空后删除。阶段 A **没有暴露任何生产代码问题**。**阶段 B**：新增 `test_burn_not_satisfied_when_media_qa_blocks_a_valid_burned_output`（不打桩，2 个 subTest），构造「成品 checksum 有效 + v2 proof 指纹匹配 + 媒体 QA 被否」两种真实状态；变异自证通过（分别失效 `_burn_satisfied` 的 stage 判定与报告 `status` 判定，对应 subtest 各自变红，其余全绿）。**阶段 C**：`workflow.py` 新增 `media_qa_failure` / `media_qa_passed`（唯一实现，标签 `stage` / `missing` / `unreadable` / `status`）与 `_burn_proof_fingerprint_matches(..., require_source_media=)`；烧录满足度、relink 资格、`relink_burned_output` 三处接入，relink 的四条历史报错文案按标签映射逐字保留；`recovery._burn_recovery_reason` 改为接收调用方算好的标签、只在 `"stage"` 上分叉，不再自己重写判定。刻意差异全部参数化或写进 docstring（源媒体是否哈希、成品要求有效 vs 已失效、是否要求 `translated_cues`、legacy proof 容忍度、relink 文案、recovery 文案排序），清单见 [review 清单](docs/REVIEW_FINDINGS_2026_10_17.md) 的 F6 条目。**阶段 D（O5）**：`_advance_workflow` 发 `WORKFLOW_COMPLETED` 之前断言 `plan_advance_steps(job, config)` 为空，不为空抛 `InternalError`（退出码 4），消息带剩余步骤；**零既有测试变红**。协议快照零 diff。新增 5 条测试 + 2 个 subtest，911 → 914 测试、233 → 235 子测试。遗留（如实记录）：`recovery._burn_recovery_reason` 对「stage succeeded 但报告失效 / 判定非 pass」仍沿用「成品缺失 / 指纹失效」文案，没有归因到媒体 QA——这是文案排序而非放行判定（`_burn_satisfied` 早已独立判这些状态未完成），改它属用户可见行为变更且被 `tests/test_recovery.py` 两条测试钉住，不塞进纯重构；`relink_burned_output` 的「媒体 QA 报告不可解析」分支由共享谓词接走后丢了 `raise ... from error` 的异常链（报错文案不变，无测试依赖）。下一项按清单是 N1 的设计决策，之后 F7 / O7。

2026-10-17 持久化脱敏收口（F1 / F8 / O3）已修。**F1**：`pipeline/common.py` 的 `set_stage` 在入口统一 `redact_text(error)`，`record.error` 与 `stage_failed` 事件共用同一份脱敏文本（`redact_text` 幂等，事件分支再脱敏一次结果不变）；刻意不去补那约 18 个 `error=str(error)` 调用点——靠调用方自觉的契约必然再漏。`events.redact_text` 的 docstring 同步改成「两条持久化边界已集中接入」的现状。**F8**：`events.py` 的 `_SENSITIVE_QUERY_KEYS` 从精确匹配集合扩成三层判定 `_is_sensitive_query_key`——精确名单（向后兼容）→ 子串规则（`signature` / `credential` / `token` / `secret` / `password` / `accesskey` / `apikey`，覆盖 GCS v4、腾讯 `q-signature`、`OSSAccessKeyId`）→ 按非字母数字分段的整词规则（`auth` / `authorization` / `key` / `sig` / `pwd` / `passwd`，命中阿里云 `auth_key`，同时不误伤 `monkey` / `keyword` / `author` / `design` / `signal`）。**O3**：`translation_provider.py` 新增 `_validate_base_url`，在解析出 Base URL 之后、「配置是否齐全」判定之前拒绝带 userinfo 或 query 的 Base URL，错误消息只回显 hostname；`--provider auto` 也抛错而不静默降级。实测确认过必须入口拒绝：`https://user:secretpw@api.example.com/v1` 抛的是 `http.client.InvalidURL: nonnumeric port: 'secretpw@api.example.com'`（有无代理两条路径一致），不带 scheme，`_URL_PATTERN` 与 `_USERINFO_PATTERN` 都不命中，脱敏原样返回。红先取证齐全（未改生产代码直接跑新测试：F1 的 5 个 subtest 全红，F8 的 14 个 subtest 红，O3 的 2 个 subtest + 2 条测试红）。协议快照零 diff，**既有测试零变红**。新增 10 条测试 + 55 个 subtest，900 → 910 测试、178 → 233 子测试（其中 `test_messages_compared_by_value_survive_redaction_unchanged` 是 review 时补的守卫，不是红先测试：`pipeline/review.py:246` 按文本相等识别 `SUBTITLE_REVIEW_REQUIRED_MESSAGE` 来收口过期阻断错误，脱敏若改写这条常量会让收口静默失效）。AGENTS 约束 5 的措辞随之改为「在 `set_stage` 边界统一脱敏」。遗留（如实记录，未修）：`artifacts/source-candidate-qa.json` 的 `invalid_candidate` issue message 带 `load_subtitles` 的 `str(error)`，不过边界、未脱敏（内容是本地路径与解析错误，无凭据面，且 QA message 被 `candidate_qa.overflow_limits` 正则解析，改动面大于收益）；`pipeline/revision.py:413` 的 `qa_record.error = qa_message` 绕过 `set_stage` 直写（取值只有两个硬编码常量，零凭据面，但目前没有守卫阻止下一处这样写）；`job.input` 作为恢复依据按原样落盘，用户若把签名 URL 当输入，它在 `job.json` 里是明文而在事件里已脱敏。下一项按清单是 N1 的设计决策。

2026-10-17 review 清单正确性剩余（F2 / F3 / F4）已修，O5 停下待裁决。F2：`pipeline/translation.py` 新增 `_resolved_translation_inputs`，`prepare_agent_translation` 与 `run_translation` 在未显式传参时逐字段回退 `workflow.translation_policy` 与 `workflow.batch_size`，与同文件 `base_url` / `model` 的回退同口径（`cli.py` 的 `translate` / `prepare-translation` 两处 `--batch-size`、`--translation-style` 随之去掉默认值，改用「None 表示未指定」，与 `polish-translation` 一致）。F3：`workflow.py` 的 `plan_advance_steps` 删掉「manifest 失效仍报 `await_agent_translation`」的兜底分支（原 :745-746），落到 `translate` / `import_translation`；`_ensure_translation` 的等待分支同步要求 `translation_batches` checksum 有效，否则重建批次。F4：`recovery.py` 在 `qa_blocked` 判定后为原子 Job 补一条等价让路条件（`TRANSLATE` 等待态 + manifest 有效 → 放行给既有的原子等待分支）。红先取证齐全（备份源码跑新测试，8 条红、3 条控制组绿）。协议快照零 diff。新增 9 条测试，891 → 900。**O5（`workflow_completed` 前的 fail-closed 终检）已实现但按约定停下未落地**：补上 `plan_advance_steps(job, config) == ()` 的断言后，`tests/test_workflow.py` 里 6 条测试 + 1 条测试的 3 个 subtest 变红，原因全部是这些测试把 `run_translation` / `apply_subtitle_translation` / `burn_translated_job` / `valid_artifact_path` 打了桩，Job 状态从未真正推进却断言 `result.status == "completed"`——与 F6 的根因同族（真实状态从未被构造）。守卫代码与测试暂存在本次会话的 scratchpad，是否落地取决于「先把这 7 处桩改成构造真实状态」的决策。另有两条既有测试的 **setup**（断言字节不变）被修正：`test_plan_advance_steps_awaits_agent_without_burn` 与 `test_recovery_plan_describes_agent_translation_prerequisite` 原本在「没有有效 manifest」的状态下断言等待引导，F3 后这正是应给出 `translate` 的状态，故给它们补上有效 manifest，测试本意（等待步骤不追加 burn / 等待引导的命令形状）不变。

2026-10-17 全面 review 发现存档：一次只读的全仓设计与实现 review（5 个切面：正确性与恢复契约、安全边界、架构与可维护性、测试质量含 19 个内存变异点、文档漂移）产出 1 条阻断级、10 条应修与一批可优化项，逐条带锚点与触发条件，存档在 [review 发现清单](docs/REVIEW_FINDINGS_2026_10_17.md)。阻断级 B1（译文二次回填交付旧字幕）已修。**下次开工优先按该清单的「建议处理顺序」走**：F2/F3/F4/O5 正确性剩余 → F1/F8/O3 持久化脱敏收口 → N1（确认是否绑定译文内容）的设计决策 → F6/F7/O7 烧录判定与 fail-closed 补洞。清单同时记录了本次 review 的覆盖盲区（字幕算法、下载策略、转写适配层、快照 golden 内容本身），不要因为它没提就认为那些区域没问题。

2026-10-17 译文回填失效口径修复（阻断级正确性 bug）：`pipeline/translation.py` 的 `apply_agent_translation`（:1402）与 `apply_subtitle_translation`（:1465）此前给 `finalize_translation` 传的是 `invalidate_preview_artifacts`，而同一函数的另外两个调用点（`retranslate.py:333`、`semantic.py:529`）传的是 `invalidate_rendered_artifacts`——四个调用点分成两派。preview 口径只删 `preview_ass` 及其下游，不碰 `translated_srt` / `bilingual_srt` / `bilingual_ass`，不清烧录 fingerprint，也不重置 SUBTITLE_QA / RENDER / BURN / MEDIA_QA。链条是：`finalize_translation` 结尾的 `_qa_and_render_translation` 在 `if not complete_qa_stage(...): return (qa_path,)` 处早退，QA 非 pass 时根本不渲染；于是一个已渲染过的 Job 回填 v2 译文且 v2 的译文 QA 落在 `needs_review` 时，`translated_cues` 是 v2 而磁盘上的 SRT 还是 v1 且 checksum 有效（文件没被动过），`workflow._translation_satisfied` 全绿、`continue` 发 `workflow_completed`，用户拿到的是旧译文——违反不可破坏约束第 4 条的精神（QA 未放行的内容被当成交付）。两处改为 `invalidate_rendered_artifacts`，与另外两个调用点对齐。判定「原 preview 口径不是刻意」的依据：同一个 `finalize_translation` 内 polished 草稿分支自己就做了等价的完整重置（`downstream_of(TRANSLATION_DRAFT_CUES)` + `clear_burn_fingerprint` + 四个 stage 回 PENDING）；`invalidate_preview_artifacts` 的注释与唯一合理调用点 `burnin.py:276` 都表明它是为「译文 cue 未变、只重做预览」设计的；rendered 口径在集合上严格包含 preview 口径；首次翻译时下游产物尚未登记、失效为空操作，stage 置 PENDING 后紧接着被 `_qa_and_render_translation` 推进，无回归面。红先取证：新测试在修复前的代码上失败于「过期 v1 `translated.srt` 仍是 checksum 有效的交付物」。协议快照零 diff（两个场景都是首次翻译，失效为空操作且不发事件）。新增 `TranslationRevisionInvalidationTest` 两条（Agent 回填走完整断言，导入译文走轻量断言），889 → 891。遗留观察（本次未改）：`subtitle_review_acknowledgment` 只绑定 `source_cues` checksum 与「问题族 + cue id」，译文内容变化不会使其失效——v1 的超宽确认会替一条用户从未看过的 v2 超宽译文代签放行。`revision.py` / `semantic.py` 改写 cue 时都显式 pop 掉这条确认，`invalidate_rendered_artifacts` 没有；四条译文回填路径同样暴露，属于既有缺口而非本次引入，需单独决策。下一项仍是 Phase 5（语义链路编排单份化）。

2026-10-17 R3.1 Phase 4c：semantic 接入共享批次循环，Phase 4 收尾——四条链路（translation / retranslate / polish / semantic）至此共用 `run_provider_batches` 一份循环骨架。`_run_review_phase`（初审 / 复审共用，三次调用）与 `_run_repair_phase` 两处同形循环各减 20 / 26 行，`_emit_batch_event` 这个只服务于它们的 helper 一并删除（`semantic.py` 1163 → 1094 行）。semantic 相对已接入三处的两条新差异按参数化承接，没有硬塞：`with_batch_index=True` 让共享循环内部 `enumerate(batches, start=1)` 把**本轮 1 起序号**注入 started / retrying / completed 三类事件（它与稳定的 `batch.id` 是两个不同的东西，混用就是历史上「批次 38/1」那个显示 bug）；`fail_stage=False` 表示循环内不收口 stage——语义校对的 stage 失败由 `run_translation_semantic_review` 的外层 `except` 统一带 `extras={"phase": "semantic_review"}` 收口，循环里再收一次会多发一个不带 phase 的 `stage_failed`。其余差异走既有参数：`request=semantic_review_batch` / `semantic_repair_batch`（显式传，保住 51 + 11 处桩位）、review 传 `_SEMANTIC_REVIEW_RESPONSE_CORRECTION` 而 repair 沿用默认、`phase` 为 `semantic_review` / `repair`、`skip=batch.id in completed`、`on_result` 写 checkpoint。循环之后那次额外的 `_write_review_checkpoint` / `_write_repair_checkpoint`（重新赋值 `path` / `progress_path` 的那行）保留在循环外。链路决策逻辑（初审 → 可修复？→ 修复 → 复审 → 确认/等待）零改动，留给 Phase 5。协议快照零 diff；`fail_stage=False` 此前全仓无覆盖，新增 `test_api_review_failure_collars_stage_once_with_semantic_phase` 与 `test_api_repair_failure_collars_stage_once_with_semantic_phase` 两条（887 → 889）。负向取证：把 `fail_stage` 写死 True 时这两条新测试红（`stage_failed` 2 != 1，快照不覆盖失败路径仍绿）；去掉 `batch_index` 注入时 `api_polished` 快照场景与 `test_human_revision_scopes_review_and_preserves_untouched_issues` 红。下一项是 Phase 5（语义链路编排单份化）。

2026-10-17 R3.1 Phase 4b：polish 接入共享批次循环，并收敛两处 Agent 降级事件。`_run_translation_polish` 的循环块 62 → 22 行，差异全部参数化：`request=polish_batch`（显式传，保住 `xvt.pipeline.polish.polish_batch` 这个桩位）、批次事件带 `phase=polish`、checkpoint 跳过只看 `batch.id in completed`、每批后 `_write_progress` 走 `on_result`。`run_provider_batches` 新增 4a 预留未加的 `stage_extras`（默认 `None`，只服务润色失败收口的 `extras={"phase": "polish"}`，与事件里的 `phase` 刻意不互相派生），并补上它的专项测试 `test_api_polish_failure_marks_stage_with_polish_phase`——此前全仓无人断言润色失败事件的 `phase`。`pipeline/common.py` 新增叶子 helper `emit_agent_fallback`，收敛 translation 与 polish 两处 `TRANSLATION_FALLBACK` 发射（`phase=None` 时不写该键）；helper 只发事件，不含 `set_stage(AWAITING_INPUT)`——两处的 stage 位置与 extras 本就不同。polish 侧 `_load_progress` 的 `translation_progress_reset` 保持原样未动（Phase 1 记录的快照已知缺口）。协议快照零 diff；负向取证：去掉 `stage_extras` 只有新增的 polish 测试会红（快照不覆盖润色失败路径），漏传 fallback 的 `phase` 则 `polish_agent_rerun` 场景会红。

2026-10-17 R3.1 Phase 4a：provider 批次循环单份化（translation 与 retranslate）。`pipeline/translation.py` 新增 `run_provider_batches`，承接取消点、批次事件、逐批回调与失败时的 stage 收口；`run_translation` 的循环块 82 → 40 行，`retranslate_cues` 44 → 23 行。两处的真实差异全部参数化保留：局部重译不发 `translation_batch_started` / `translation_batch_completed`、没有 checkpoint、事件不带 `phase`，初译只在 polished 质量下带 `phase=draft`。共享循环刻意留在 `translation.py` 而非新模块：38 处测试桩钉在该模块的 `translate_batch` / `time.sleep` / `_backoff_delay` 上，移出会一次性废掉这些桩并与 translation 成环。协议快照零 diff；负向取证确认把「重译不发进度事件」这条差异错误统一后快照会红。原 Phase 3（Agent 交接前奏单份化）已折入 Phase 4：实测 `TRANSLATION_FALLBACK` 全仓只有 2 处发射，不值一轮独立 PR，留到 4b 随 polish 循环一起收。

2026-10-17 R3.1 Phase 2：消解 pipeline 包内的函数级 import 环。新增零依赖叶子 `src/xvt/pipeline/semantic_state.py`，下移 `semantic_review_target` 及其完整传递闭包共 18 个只读派生函数（批次重算、policy 回退、报告解析、repair checkpoint 读取、Agent 结果解析与序列化），`review` / `revision` 顶层 import 该叶子，`semantic` 顶层 import `review` / `revision` / 该叶子，原先 8 处函数体内惰性 import 全部消除。纯搬移：18 个函数在改名映射下 AST 完全等价，协议快照零 diff。`tests/test_pipeline_imports.py` 新增 ast 守卫，禁止 pipeline 包内出现任何函数级 `xvt.pipeline.*` import（覆盖任意嵌套深度与自引用）并防止扫描范围静默缩小。`xvt.recovery` ⇄ `xvt.pipeline` 的跨层环不在本阶段范围。

2026-10-17 R3.1 Phase 1：provider 协议快照回归网落地（`tests/test_provider_protocol_snapshot.py` 与 `tests/fixtures/provider-protocol-snapshot.json`），零生产代码改动。7 个场景冻结 API polished、Agent polished、`auto` 降级、进度重置、局部重译（API 与 Agent）和显式补跑润色（Agent）的事件序列、artifact 内容 checksum、manifest 键序与 fingerprint；负向对照确认改掉事件发射点、事件类型、artifact 文件名或 manifest 键名都会让快照变红。R3.1 据此拆为 5 个独立可合阶段，后续每个阶段以「快照零 diff」为硬验收，出现 diff 即回退而不改 golden。已知缺口：polish 侧 `translation_progress_reset`、Agent 链路 fingerprint 字面值（绑定 job id，只冻自洽性与传播分组）、provider 彻底失败后的 stage 收口文案。

2026-10-11 Q6.2 离线基线：只读扫描全部 20 个历史 Job，整理 39 份语义报告与 26 次已接受修订，提取 4 类最小样本并增加确定性边界测试。多轮修订引入不同遗漏、源文疑点保留和历史无变化建议分别处理；不把模型输出当真值，不修改生产提示词或默认质量。当前模型重复输入实验与原音 oracle 仍待验证，Q6.2 未整体完成。详见 [收敛基线](docs/Q6_2_CONVERGENCE_BASELINE.md)。

2026-10-11 polished Agent 验收：独立临时 Job 完成公开视频下载、84 条英文人工字幕、Agent natural + polished 翻译与语义校对、1080p H.264/AAC 整段烧录及媒体 QA。24 个 artifact 全部校验通过，时间轴和源文不变；错误 fingerprint 回填不改 Job/manifest，完成后 continue 不重翻译/重烧。字幕 QA 有 17 条 warning、无阻断项。三帧目视抽查正常。此样本不覆盖 ASR 或真实语义修复，默认保持 standard + faithful；下一项为 Q6.2 语义复查收敛验证。详见 [验收记录](docs/POLISHED_AGENT_E2E_ACCEPTANCE.md)。

2026-10-11 YouTube 画质修复：已复现 Chrome Cookie 仅返回 360p，而匿名提供 1080p。完整视频获取现比较两种访问方式，原音轨优先、分辨率无上限、固定格式 ID、独立临时下载与实际媒体校验；原子发布拒绝覆盖。新增可选脱敏摘要，不自动更换旧 Job 媒体或字幕。check 专项与对抗复查已修复缺格式判定、无音轨候选及覆盖竞态，最终无剩余发现。全量 868 测试、176 子测试通过，Ruff、help、doctor 通过；授权临时整段下载得到 1920×1080、原音轨与正常时长。隔离验证及限制见 [画质修复记录](docs/YOUTUBE_DOWNLOAD_QUALITY_PLAN.md)。用户已授权最终检查通过后提交推送；实际提交状态以 Git 记录为准。

2026-10-04 校对体验改动：译文 QA 已加入 FFmpeg/libass 实际 ASS 宽度测量，超过字数建议但容纳得下时仅 warning；测量不可用仍保守阻断。候选 QA、人工预检、译文收尾与烧录使用实际媒体/字体上下文，确认绑定 layout；预览不覆盖正式 QA。人工 JSON/SRT 修订新增 `apply --check`，正式提交也在写入前完整预检，不合格不改 Job 或失效下游。此次不修改语义 severity、源文重建和下载策略。实现与验证结果见本次分支；不重写用户已完成的视频任务。

## 当前快照

2026-10-04 review 修复：原子 `burn` 与 `preview` 的所选媒体/字体 QA 只读，不覆盖工作流正式报告；本地替代媒体或字体导致布局失败时 Job 不变，检查前发生的远程媒体获取仍按原契约持久化。端到端 `run` / `continue` 仍持久化目标布局 QA。短视频、窄画幅和字体变化的回归覆盖失败后继续原工作流。polished Agent 本地纯字幕 CLI 冒烟已完成，3 条 cue 时间轴保持不变、14 个 artifact 校验通过；公开视频到烧录的 Agent 主链验收已于 2026-10-11 完成，覆盖范围见本文件顶部验收记录。

| 项目 | 状态 |
| --- | --- |
| 包名 | `xvt-video-translate` |
| CLI | `xvt` |
| 版本 | `0.1.1` |
| Python | 3.11 及以上 |
| 当前主线 | 先完成字幕质量与自适应源字幕闭环，再评估 App |
| Job schema | v1（端到端目标为可选 `config.workflow` / `WorkflowConfig`） |
| 自动化测试 | 941 个测试、235 个子测试，全部通过（2026-10-17） |
| 静态检查 | Ruff lint 和 formatter 通过 |
| 打包 | wheel 和 source distribution 已验证 |
| CI | GitHub Actions（ubuntu + macos × Python 3.11/3.13 矩阵）跑 ruff、pytest、CLI 冒烟与 `uv build` |
| Git | 功能修改使用 `codex/` 开发分支，Review 通过后以 PR 合并 `main` |
| 默认 CLI 路径 | `run` → `status` → `continue`（批处理 `batch` / `batch-continue`） |

当前代码可以完成本地视频或 YouTube 视频的转写、翻译、双语字幕生成和可选烧录。端到端流程收敛到 `xvt run`；中断后先 `xvt status` 查看 `recovery_plan` / `advance_steps`，再 `xvt continue` 按持久化目标补齐。翻译与烧录推进与 status 共用 `plan_advance_steps`。由 `run` / `continue` 按持久化目标完成的烧录写入 checksum 保护的 `version=2` burn proof，绑定烧录 fingerprint、`burned_video` checksum 和实际源媒体内容 checksum；高级原子 `xvt burn` 不签发 workflow proof。仅有效 v2 proof 可让 `relink-burned-output` 重新关联已移动成品。若其余静态条件满足但已登记成品缺失或 checksum 失效，`status` 返回 `awaiting_input`，首选重新关联占位命令、备选显式 `continue` 重烧；这只表示成品可能可重新关联，执行时仍会校验实际源媒体内容。`batch-continue` 停在该状态，不自动重烧。旧/legacy fingerprint、媒体 QA/proof/fingerprint 无效仍为 `retryable`，只推荐重烧。

2026-10-04：修复显式 YouTube 字幕请求在浏览器 Cookie 查询漏报人工字幕时直接回退 ASR 的问题。匹配人工字幕缺失时匿名补查一次，同一视频匹配成功后匿名下载字幕；显式代理保留、媒体访问方式不变。`remote_info.subtitles_anonymous` 与选字幕事件记录访问方式，下载中断恢复继续匿名；补查失败报错。已有源 cue 的 Job 不自动重建，polished Agent 主链 E2E 已于 2026-10-11 完成，默认质量继续保持 standard + faithful。

本次 review 补齐已有有效候选的保护：不重新补查、不把旧自动字幕重标为人工字幕。公开 YouTube 视频的临时 Job 已实际完成匿名补查、人工字幕下载与源字幕 QA，共 67 条 cue，无媒体下载、ASR 或翻译 API 调用；用户原 Job 保持原样。

CI 回归修复：烧录 QA 测试分别模拟布局测量成功和不可用，检查短字幕对应的 pass/warning、警告内容与 QA 放行状态，不再依赖宿主机 FFmpeg 和字体。生产 QA 规则保持不变。

## 项目背景

这个项目要解决的是视频字幕翻译链路中最容易失控的三件事：时间轴、翻译上下文和最终显示质量。用户可以只提供视频，也可以同时提供源字幕、机翻字幕或译文字幕。系统需要识别哪些步骤可以跳过，并且始终以源 cue 的时间轴为准。

方案选择了 Python CLI 作为核心，原因是 FFmpeg、yt-dlp、Whisper 和字幕生态在 Python 中接入成本较低。Skill 只负责调用 CLI 或完成 Agent 翻译，不拥有时间轴和媒体处理逻辑。App 暂缓，未来只消费 `job.json` 和 JSONL 事件，不复制业务逻辑。

## 已完成能力

### 输入和素材获取

- 支持本地视频、音频和 HTTP/HTTPS 远程 URL。
- YouTube 通过 yt-dlp 获取元数据、人工字幕、自动字幕、音频或完整视频。
- 人工字幕优先。只有显式传入 `--allow-auto-subtitles` 才使用自动字幕。
- yt-dlp 把语言写进字幕文件名，同一 Job 的下载目录可能同时留有多个语言或变体；选取时先精确匹配 `source.<language>.srt`，再匹配 `source.<language>-*` / `source.<language>.*`（大小写不敏感），都没有才回退字典序首个。
- 请求烧录时，远程完整视频在前置阶段下载一次，ASR 和烧录复用同一文件。
- Cookie 来源和代理必须显式传入，不会默认读取浏览器状态。

### 转写和时间轴

- 支持 MLX Whisper 和 faster-whisper，保留词级时间戳；faster-whisper 提供真实百分比，MLX 提供运行时长心跳。
- 分段综合停顿、标点、字幕时长和显示宽度；`conservative`、`balanced`、`compact` 预设仅调整 ASR 的停顿和最大时长，默认保持平衡行为。
- 可导入 SRT、VTT、ASS 和 SSA 源字幕，默认保留用户时间轴。
- 输入字幕必须是 UTF-8（带不带 BOM 都可以）。非 UTF-8（GBK、Big5、Shift_JIS、带 BOM 的 UTF-16/32）与解析失败都归为 `invalid_input`（退出码 2），报错指向用户传入的原始路径并给出 `iconv` 转码建议；带 BOM 的 UTF-16/32 会点名疑似编码，无 BOM 的 UTF-16/32 走解析失败分支，消息里按 NUL 字节提示疑似宽字符编码。XVT 不做本地字符集探测——宽松解码器会把内容「成功」解成乱码，乱码会流进翻译 API 并固化进 checksum。
- 源字幕可直接作为 `run` 输入创建纯字幕 Job，无需媒体即可翻译、QA、恢复和导出三种 SRT；无媒体时不生成 ASS，也不支持预览、烧录或重分段。
- `--align-subtitles` 会额外运行 ASR，并用顺序模糊对齐修正整体偏移或漂移；它必须与 `--subtitles` 同用。强制对齐 fail-closed：置信度低于阈值失败（`--allow-low-confidence-alignment` 可显式放行），对齐后字幕 QA 出现 error 级问题同样失败且该参数不放行。去掉 `--align-subtitles` 保留的是源字幕原时间轴，不是 ASR 分段；需要 ASR 时间轴要用 `--source-strategy asr`。
- cue 结束时间、相邻重叠和媒体边界会在分段与 QA 阶段校正或阻断。
- 分段与对齐共用同一个边界归一实现：重叠优先按中点切开；中点无法给两条 cue 都留出正时长（严重重叠、完全嵌套、词时间戳回退）时把后一条推到前一条结束处，仍不足则补足 1ms 最小时长，受影响的 cue 在 `warnings` 留痕但不产生新的 QA 问题。尾部收口的 `min(padded_end, next_start, media_duration_ms)` 同样钳制回正时长。归一后做单调性自检，不把零/负时长时间轴交给下游 QA。

### 翻译

- 支持 OpenAI-compatible Chat Completions，实际请求地址为 `<base-url>/chat/completions`。
- 默认环境变量为 `XVT_TRANSLATION_BASE_URL`、`XVT_TRANSLATION_MODEL` 和 `XVT_TRANSLATION_API_KEY`。
- `--provider auto` 在配置完整时调用 API，配置不完整时降级为 Agent 批次。
- `--provider openai-compatible` 缺少配置时直接失败，不静默降级。
- `translate` 和 `prepare-translation` 支持 JSON 术语表与 `faithful`、`natural`、`concise` 风格；批次仅携带命中源 cue 的术语。
- `run` 也支持同一套参数，并将解析后的策略快照写入工作流配置；`continue` 不依赖原术语表文件，始终复用该快照。
- 译文 QA 对命中源 cue 的术语确定性检查目标词；违规生成 `glossary_violation`、保留 QA 报告并阻断渲染和烧录。阻断的 error 全部是 `glossary_violation` 时，`status` / `continue` 给出 `retranslate-cues --cue … --target-language …` 出口（该 severity 不能由 acknowledge 通道放行）；`standard` 只改指定 cue，`polished` 会写成新草稿并重启润色与语义校对。必需术语优先取端到端 Job 的 `config.workflow` 策略快照，顶层 `config.translation_policy` 只是原子 Job 的回退位置；`prepare-translation` 在最后一次落盘前写入该顶层配置，兼容 API 模式下也不会被随后的 `load_job` 丢弃。
- API 翻译按批次保存进度，指纹包含源 cue checksum、语言、端点、模型、批次大小、提示词版本、语义分组版本和翻译策略。
- 默认兼容 API 翻译批次为 25 个 cue，请求超时为 180 秒；单个新 Job 可通过 `--batch-size` / `--timeout-seconds` 覆盖，已创建 Job 始终复用持久化配置。
- 429、部分 4xx、5xx、网络异常、远端断连、响应读取连接重置、分块响应中途断开（`IncompleteRead` / `BadStatusLine` / `LineTooLong`）和 TLS 连接中途终止（`SSLEOFError` / `SSLZeroReturnError` / `SSLSyscallError`）具有明确的传输重试分类；证书与握手类 `SSLError` 在请求和响应两个阶段都立即失败（请求阶段经 `URLError` 包装，按 `reason` 判定）。传输故障默认最多重试 4 次，优先使用最长 30 秒的有效 `Retry-After`（RFC 9110 的 delta-seconds 与 HTTP-date 两种形态，其余值退回本地退避），否则按 equal jitter 指数退避；结构性无效响应另有固定 2 次纠错重试，两个预算互不消耗，且传输重试保留上一轮的纠正提示。
- 翻译响应必须完整覆盖 cue ID，不允许重复、缺失或额外 ID；`id` 必须是真正的整数，JSON 布尔值（`true` / `false`）与全部其他解析器一致被拒绝，不再当作 cue 1。
- 标准 Agent manifest 记录 `source_cues_checksum`，回填时拒绝源 cue 已变化的陈旧响应；已登记的 manifest 若 checksum 失效、缺失或内容损坏同样拒绝回填并引导重新 `prepare-translation`。从未登记 manifest 的极旧 Job 与没有该字段的旧 manifest 仍按原样接受，只读查询路径不受影响。
- 回填的输入校验是纯只读的，失败不改写 Job 状态：`apply-translation`（含统一 `xvt apply`）与 `import-translation` 把「读不出 / 非 UTF-8 / 结构不符 / 覆盖不全 / manifest 失配 / 源 cue 无效」这些检查全部放在触碰 stage 之前，坏输入下 `TRANSLATE` 原地保持 `awaiting_input`。否则等待态被顶成 `failed`，`plan_advance_steps` 不再返回 `await_agent_translation`，统一 `xvt apply` 会误报「not awaiting Agent input」，恢复计划也从「等待回填」降级成「重跑翻译」——用户只是敲错一个文件，却要重付一次翻译成本。校验通过后才置 `running`，随后真正的改写（`import-translation` 的字幕拷贝与 artifact 登记、`finalize_translation` 的落盘与 QA）失败仍按原约定收口成 `failed`。`translate_and_polish`（一次翻译加润色）分支不在此列，仍按原有的 stage 记账。
- 翻译在不改动 cue 的前提下建立确定性语义组：句末标点或 650ms 间隔结束组，组上限为 `min(20, batch_size)` 个 cue 或 30 秒，超限按软标点、连接词前和最大停顿择边；单个超长 cue 单独成组。provider 批次只按完整组装箱，批次外前后各一个完整相邻组仅作只读上下文。
- `TranslationBatch.semantic_groups` 记录当前批次的组 ID；响应仍只覆盖当前 `items` 的原 cue ID，不得输出上下文 ID。局部重译使用 singleton groups；`max_target_characters` 是显示建议，最终显示风险仍由确定性 QA 判定。
- `resegment-translation` 仅对超过硬显示阈值且具有 ASR 词级时间戳的 cue 按语义优先边界重切；它重建 source cue 和 source SRT，废弃旧翻译及其下游产物，禁止复用或字符拆分旧译文。
- `retranslate-cues` 只翻译指定 cue ID，保留其他译文和时间轴；每个目标 cue 带相邻源 cue 作为上下文，回填后完整重跑译文 QA 并使旧渲染/烧录产物失效。Agent 模式的等待是 `plan_advance_steps` 的正式步骤 `await_cue_retranslation`：`status` 归 `awaiting_input` 并拼出带具体 cue ID 与目标语言的 `apply-cue-retranslation` 命令，`continue` 不宣告完成，`batch-continue` 停在该条目。谓词严格绑定当前 source/translated cue checksum，回填后的残留 manifest 不会盖掉后续的润色或语义等待。回填失败不再丢掉这次等待，两种模式的 stage 收口不同：用户显式跑的 `apply-cue-retranslation`，其校验（响应 fingerprint 对不上、`--cue` / `--target-language` 写错、manifest 与当前 XVT 版本失配等）全是只读动作，失败不改写 Job 状态，`TRANSLATE` 原地保持 `awaiting_input`；API 模式（`retranslate-cues` 直连提供方）下响应是 XVT 自己刚落盘并登记的内部产物、stage 已由调用方置 `running`，同一段校验失败属于内部故障，仍按原约定收口成 `failed`。两种情形下盘上的 manifest 都原样保留且仍然有效，那次等待并没有作废，因此 `cue_retranslation_waiting` 的 stage 条件同时接受 `awaiting_input` 与 `failed`；`failed` 这一支现在只覆盖 API 模式重译失败与旧版本留下的历史 Job（旧版本把用户输入错误也打成 `failed`）。`status` / `continue` 继续提示回填，不会在 `SUBTITLE_QA` 成功时改口报完成、也不会在 `SUBTITLE_QA` 失败时重复给出用户已经跑过的 `retranslate-cues`。`failed` 情形额外要求「没有更新的在途整轮翻译请求」：用户改走整轮重翻时 `prepare_agent_translation` 写出的新 `translation_batches` 在 `job.artifacts` 里排在重译 manifest 之后，此时整轮等待优先，回填出口不会指向已被取代的那一份。恢复计划拼出的 `--cue` 顺序与 manifest 内的批次顺序一致（不排序）：`apply-cue-retranslation` 对 manifest 做全等比对，乱序 `--cue` 的 Job 上排序会让复制的命令必然失败。放弃一次重译不必手工删文件：`prepare_agent_translation` 写出新的整轮 `translation_batches` 时会把 `cue_retranslation_batches` 移出登记，否则残留 manifest 会遮蔽合法的初译等待。`polished` Job 上回填结果会成为新草稿并重启润色与语义校对（真实模型成本），`standard` Job 只改选中的 cue。

### 字幕和显示质量

- 输出 `source.srt`、`translated.srt`、`bilingual.srt` 和 `bilingual.ass`。
- 模型生成的显式简中译文（`zh-CN`、`zh-Hans` 及 `zh-Hans-*` 区域后缀形式）写入时消除全角 `，`、`。`：句中按需替换为一个半角空格以保留词边界，行尾与换行前直接移除；连续全角句号归一为单个 `…`；问号、感叹号、分号、冒号、破折号、ASCII 标点和原有换行保留。源 cue 末尾问句而译文缺少句末语义标点时补 `？`，位置镜像源文中问号相对末尾闭合符号的位置。导入译文、人工修订、源字幕、bare `zh`、繁中及其他目标语言不变。
- 双语字幕固定为译文一行、原文一行。每种语言内部的原始换行会被折叠。
- 16:9 横版英文在 64 个显示单位以内通过，65 到 72 为警告，超过 72 需要复核。窄画幅会收紧限制，中文限制更严格。
- ASR / `asr_resegmented` 的 `low_timing_confidence` 为 warning；对齐或用户导入字幕的低置信度仍为 review，需确认后继续。
- ASS 使用独立的中英文字体和字号。macOS 默认中文字幕字体为 libass 可读取的 `Hiragino Sans GB`。
- cue 文本中的字面 `{` `}` 在 ASS 渲染时转义为 libass 的 `\{` `\}`，不再被当作覆盖标签块吞掉，`{\an8}` 一类内容也不会真的改变字幕位置；`\N` 换行与 `{\rSource}` 样式切换是渲染器拼接的结构，不参与转义。反斜杠刻意不转义（ASS 无标准反斜杠转义，libass 原样输出未知序列）。SRT/VTT 导出保留花括号。导入端已同口径收口：`load_subtitles`（`--subtitles`、`--translation-subtitles`、`import-translation`）按 pysubs2 解析出的实际格式分叉，SRT/VTT 保留字面花括号、只剥 `{\…}` 形态的覆盖块，ASS/SSA 仍整段剥除（那里 `{...}` 是标签语法本身），导出→人工编辑→导回链路不再丢内容；人工修订 SRT 走独立纯文本解析器，同样保留。仍存在的限制：SRT/VTT 里 `{\…}` 形态的字面文本与真覆盖标签无法区分，仍会被剥除。
- 字幕 QA error 记为 `failed`；纯 `needs_review` 记为 `awaiting_input`。两者都会停止工作流且不允许继续烧录，原子命令也不会误发 `cli_completed`。
- 高级原子 `xvt burn` / `xvt preview` 与端到端链路共用同一道 fail-closed 译文 QA 门（`pipeline.review.review_gate_satisfied`），不能绕过 QA：要求 checksum 有效的译文 cue 与 QA 报告，`pass` / `warning` 放行，`needs_review` 只在报告内全部 review 问题都被当前有效确认覆盖时放行，error、缺失、checksum 失效或结构异常一律拒绝。确认通道只有 `continue --acknowledge-subtitle-review`，因此原子 Job 的报错文案指向重跑 `xvt translate`，不指向不可用的 `continue`。
- 语义复核确认后，若译文候选和渲染产物已有效或被补齐，`translate` stage 会收口为 `succeeded` 并清除旧 QA 错误；字幕 QA 的独立 `needs_review` 仍需单独确认。
- 「译文已满足」的判定包含译文 QA 报告与双语 ASS：需要 checksum 有效的 `translated_cues`、`translated_srt`、`bilingual_srt`、`translated_subtitle_qa`，外加**已登记过**的 `bilingual_ass`（`workflow._translation_satisfied` 与 polished 链路的 finalize 短路共用同一口径）。漏掉 QA 报告会让报告单独失效的 Job 被判成「已完成」而空转，`continue` 永不重建。`bilingual_ass` 同理：它是 README / 架构说明列出的交付物，也是 `workflow_completed` 汇报的产物之一，删掉或改坏 `bilingual.ass` 后旧口径会让 `continue` / `status` / `batch-continue` 三路都报完成，而 CLI 没有任何命令能单独重建它；补上之后 `plan_advance_steps` 给出 `render`，走零 API 成本的重渲染。该条件只对已登记过 `bilingual_ass` 的 Job 生效——纯音频、纯字幕等没有分辨率的 Job 从来不渲染 ASS，对它们要求 ASS 会让 `continue` 永远收敛不了。烧录 Job 不额外分叉：至多多渲染一次 ASS，代价为零。原子 Job 上这条缺口的零成本出口（`recovery.atomic_translated_qa_rebuild_command`，`status` 与 `pipeline.burnin` 的 QA 门共用）只对 `standard` 成立：`import-translation` 与其余回填路径共用 `finalize_translation`，在 `polished` Job 上走 draft 分支，QA 不重建、渲染不执行、整条润色腿失效，因此该函数对 `polished` 返回 `None`，两处调用方各自落回既有引导。
- 恢复计划的判定顺序有一条硬约束：局部重译等待必须排在 `qa_blocked` 之前。术语死锁的出口就是 `retranslate-cues`，跑完后译文 QA 还没重跑、`SUBTITLE_QA` 仍是 `failed`，排在后面会把等待重报成 `manual_fix` 并重复给出同一条命令，端到端 Job 上还与 `plan_advance_steps` 的 `await_cue_retranslation` 自相矛盾。原子与端到端两条分支共用 `_cue_retranslation_waiting_plan`。
- 同构的第二条让路：端到端 Job 上 `qa_blocked` 也要给**任何**在途 Agent 等待让路。`SUBTITLE_QA=failed` 只说明上一次译文 QA 没过，它不会因为随后开了一次新的等待而自动复位——用户改走整轮重翻时 `prepare_agent_translation` 会移走重译 manifest、写入新的 `translation_batches` 并把 `TRANSLATE` 置为 `awaiting_input`，却不碰 `SUBTITLE_QA`。此时 `plan_advance_steps` 首步是正式等待步骤，等待才是真状态：`recovery._build_recovery_plan` 与 `workflow._advance_workflow` 用同一口径把 QA 引导排在等待之后，于是 `status` 报等待、`continue` 返回 `awaiting_input` 而不是抛错要求重跑、`batch-continue` 停在该条目并给出统一 `xvt apply` 或 `apply-cue-retranslation` 回填引导，三侧不再各说各话。
- 恢复计划里出现的 `--target-language` 一律 `shlex.quote`：该值只被 Job 保存、没有内容校验，而恢复计划是「复制即执行」面。
- 字幕 QA `needs_review` 被确认后，`continue` 会清除 `translate` stage 上的过期阻断错误（要求 QA 门已放行、确认仍对当前源 cue 有效、译文 cue 与两份 SRT 均有效）；旧 Job 下次 `continue` 自动收口。

### 烧录和媒体 QA

- FFmpeg 烧录支持 VideoToolbox 和 libx264。
- 默认优先硬件编码，只在明确的硬件编码器错误时回退 libx264。
- 视频码率按分辨率选择，不再使用 FFmpeg 8.1 不兼容的 VideoToolbox 质量参数。
- 输出媒体会检查视频流、音频流、AAC 音频、分辨率和时长偏差。
- 烧录派生 fingerprint 包含译文 checksum、媒体身份、编码方式和字体；端到端 `run` / `continue` 烧录写入 checksum 保护的 `version=2` `burn_fingerprint` proof（`burn-fingerprint.json`），并绑定 `burned_video` checksum 与实际源媒体内容 checksum；高级原子 `xvt burn` 不签发 workflow proof。旧 Job 若仅有 `workflow.burn_fingerprint` 仍可按既有轻量完成语义读取，但不能作为 relink 证明；必须 `continue` 重烧一次生成 v2 proof。`continue` 只复用成品、proof/指纹与媒体 QA 均有效时的结果。
- 烧录目标只有在 `burned_video` checksum、烧录指纹和媒体 QA 全部有效时才算完成；指纹失效或媒体 QA 失败会通过 `status`/`continue` 提示重新烧录。
- `preview` 可按 cue 或起始时间生成短片段预览；未显式覆盖时复用工作流保存的烧录媒体、字体和编码设置，不修改正式烧录 stage。缺 checksum 有效的译文与 QA 报告时以退出码 4（`workflow`）失败，不再按用户输入错误退 2；`burn` 的 QA 门在远程媒体下载之前执行。
- 预览生成 `preview.ass` 和 `preview.mp4` checksum artifact；译文局部重译或重新分段后旧预览会失效。
- 烧录与预览输出允许写到 Job 目录外的绝对路径，但不得覆盖源媒体、`job.json` 或其他已登记 artifact；同 kind（`burned_video` / `preview_video`）允许重写；路径别名和硬链接也会在写入前检查（`xvt.paths`）。
- 字幕经 `-vf ass=filename=...` 进入 filtergraph，路径按两层规则转义（先 filter 选项值层 `\ : '`，再 filtergraph 层 `\ ' , ; [ ]`）且不加引号包裹，因此 Job 目录含单引号（如 `Tom's Videos`）、逗号、分号或方括号时也能正常烧录与预览。

### Job、恢复和事件

- 每次 `run` 创建独立 Job，stage 和 artifact 写入 `job.json`。
- 普通 CLI 默认把人类可读的阶段、下载、翻译批次、转写和烧录进度写入 stderr，最终路径继续写入 stdout。
- `job_started` 在 Job 首次持久化后立即携带 `job_file`，早期素材获取失败也能定位任务文件。
- 已有 Job 的原子命令会从 CLI 参数预先记录任务路径，早期失败不会误报为任务尚未创建。
- `run` / `continue` / `apply --continue` / `batch` / `batch-continue` 停在 `awaiting_input` 时默认返回退出码 0；显式 `--fail-on-waiting` 时返回 10（`AWAITING_INPUT`），批处理有失败条目时仍是 6（6 优先于 10）。`apply` 的旗标经 continuation Namespace 透传给 `_cmd_continue`，不加 `--continue` 时无作用。该旗标只改退出码，不发 `cli_failed`、不改 Job/batch 状态与事件流。Skill 不使用它。
- Ctrl-C 返回退出码 130、SIGTERM 返回 143，两者都清理外部进程组并把当前 `running` stage 持久化为 `failed`，错误为 `interrupted by user`；改写既有 Job 的命令与 `batch-continue` 以 flock 独占执行，冲突退出码 7；独立预览中断不会生成正式工作流恢复计划，而是提示重新运行 preview。`interrupt_running_stage` 是中断处理块的第一条语句，因此它的 except 元组必须包含 `XvtError`（`job.json` 在运行期间被删除或改坏时 `load_job` 抛 `UserInputError`）：漏掉会让整个收尾落空，退出码退化成 1。批处理另有四个 `_process_item` 自身管不到的中断窗口：批次已落盘但 `batch_started` 正在发射、两个条目之间、条目置 `running` 落盘后但还没进入内层 `try`、以及最后一个条目完成后汇总落盘前。这些窗口由 `_process_batch` 的兜底 `except`（不是 `finally`，避免与内层收尾打架）统一收口：仍标 `running` 的条目降级为 `interrupted`，`batch.status` 写成 `interrupted` 并留下 `next_action`，`batch.json` 不会停在 `status=running` 且无下一步。该处理与 `_process_item` 已有的持久化天然幂等。中断状态落盘后的 JSONL/人读终态报告是 best-effort：终端或管道关闭等普通输出异常不得覆盖原取消异常，退出码仍保持 130/143。
- 人读终端输出统一经 `cli._terminal_safe` 转义控制字符，漏斗收在 `_HumanEventSink._line` / `_progress` 与 `_print_safe` 三处：叙述行里嵌的文件名（目录扫描而来）、远端标题（yt-dlp 元数据）和错误消息都可以合法地带 `\r` / ESC，足以覆盖已打印的行、清屏并伪造出一条看起来出自 XVT 的 `next:` 命令，`shlex.quote` 不设防（单引号内的控制字符照样被终端解释）。转义幂等，因此批处理条目导航里逐字段的既有调用可以照旧保留。**例外**：stdout 上的纯产物路径清单（供 xargs / 管道消费的 `print(path)`）是契约行，不转义。
- `load_user_response_json` 只服务用户提交的响应文件，`translation.load_translation_response` 是它的间接路径；读已登记 artifact 用 `translation.translations_from_response`（接受已解析载荷）。以该 helper 为底的只读谓词，except 元组里必须有 `XvtError`。非 UTF-8 的响应文件与非 UTF-8 的输入字幕同口径归 `invalid_input`（退出码 2）：`UnicodeDecodeError` 是 `ValueError` 子类但不是 `JSONDecodeError` 子类，`load_user_response_json` 与 `xvt apply` 的 except 元组都必须显式带上 `UnicodeError`，否则同一次失败会出现两个分类（`stage_failed` 记 `workflow`、顶层按 `isinstance(ValueError)` 记 `invalid_input`），`xvt apply` 那条路径还会逃逸成裸 codec 报错；`apply` 的报错额外给出 `iconv` 转码建议。
- `doctor` 的 ffmpeg 能力探测与 libass 渲染探测走 `process.run_command` 而不是裸 `subprocess.run`：以进程组方式启动并在所有退出路径（含超时）清理孙进程，同时响应全局取消事件。doctor 自行判定通过/失败，因此用 `CommandError` 捞回 `result`，超时按 `returncode == 124` 识别。
- artifact 使用 SHA-256 checksum，`verify` 会重新计算。
- `resume` 只恢复转写 checkpoint；`continue` 读取 `job.config.workflow` 完成翻译和烧录目标。远程 Job 的 `resume` 优先复用 checksum 有效的已下载媒体，没有可复用媒体时委派远程恢复路径重新获取，可能触发一次媒体下载。
- 远程端到端 Job 在检查或下载阶段失败后，可以通过 `continue` 在同一个 Job 中重试；checksum 有效的远程元数据、字幕和媒体会被复用。
- `--cookies-from-browser` 和 `--proxy` 同时作用于远程检查与下载，重试时可以重新显式提供。
- `status`、普通失败输出和 Ctrl-C 共用结构化 `RecoveryPlan`，只复用 checksum 验证通过的 artifact。
- 端到端 Job 的恢复计划通过 `plan_advance_steps` 与 `continue` 对齐，并暴露 `advance_steps`（人类可读 `steps:` 行与 JSON `recovery_plan`）。
- 恢复计划区分 `retryable`、`awaiting_input`、`manual_fix`、`unrecoverable` 和 `complete`。
- 当前 schema 的 Job 若缺少必填字段、stage 或字段类型错误，会作为可控的无效输入失败，不输出 Python traceback。cue 产物（`source-cues.json`、`translated-cues.json` 等）同样：未知字段前向兼容忽略，结构确实无法解析时报 `invalid cue artifact`（退出码 2 并发出 `cli_failed`），不再抛裸 `TypeError`。命令行给出的 `job.json` / `batch.json` 路径不存在或指向目录时同样归退出码 2 并点名路径，不再报「外部依赖或子进程故障」；Job 内**已登记** artifact 缺失不走这条路——路径来源是 Job 自己，仍按依赖或工作流问题处理。
- `polished` Job 的润色草稿失效时，只有 `translation_progress` 能零 API 成本重建草稿（fingerprint 匹配、覆盖全部批次、每个批次 cue 都有译文）才允许 `continue` 继续；否则以人工修复文案停下，不会静默重新翻译加润色。`status` 与 `continue` 共用该谓词，可重建时归 `retryable`、否则 `manual_fix`；谓词无副作用，provider 不可解析时按不可重建处理。
- Agent 降级会进入 `awaiting_input`，不会发出虚假的完成事件。
- JSONL 已覆盖工作流、stage、artifact、翻译批次、下载、转写和烧录进度。
- `workflow_completed` 和 `cli_completed` 会列出 checksum 有效的用户产物；仅生成源字幕的任务也会返回 `source.srt`。
- `batch` 会固定目录输入清单并串行创建独立 Job；单项失败不会阻断后续文件，`batch.json` 原子记录汇总状态和恢复计划。
- `batch-continue` 校验已有 Job 后只恢复未完成项，Agent 等待和人工修复仍保持显式阻断。已完成条目上只有中间 checkpoint（`normalized_audio`、`word_transcript`、批次 manifest 等）失效时保持 `completed`，只在 `item["verification"]` 与 `batch_item_completed` 事件里标注 `degraded` 并列出失效 kind（旧行为是 `failed` + 退出码 6，且引导只报告不修复，构成死循环）；目标产物失效不走这条放行——缺烧录成品停在 relink 的 `awaiting_input`，缺 `source.srt` 落到 `continue` 由 `resume` 重建，缺 `bilingual.ass` 由 `_translation_satisfied` 判成未完成后落到 `continue` 重渲染（不再被标成 `degraded` 并宣称「不影响成品」）。普通模式会在汇总行后逐条打印待处理条目及其恢复命令（取自已持久化的 `recovery_plan`，最多 10 条）。
- `--events-jsonl` 继续保持 stdout 纯 JSONL，不与普通进度文本混合。
- 命令参数、URL 用户信息、常见签名查询参数、Cookie 和鉴权信息会在事件边界脱敏。

## 支持的输入组合

| 输入 | 行为 |
| --- | --- |
| 本地视频 | ASR，按需翻译和烧录 |
| 本地视频 + 源字幕 | 跳过 ASR，保留源字幕时间轴 |
| 本地视频 + 源字幕 + 译文字幕 | 跳过 ASR 和翻译 API，合并后可烧录 |
| 本地音频 | ASR 和字幕输出；烧录时必须增加 `--burn-media` |
| YouTube URL | 按参数选择平台字幕或 Whisper |
| YouTube URL + 人工字幕请求 | 优先下载人工字幕，缺失时按配置回退自动字幕或 ASR |
| 本地目录 | `batch` 按稳定顺序创建独立 Job，失败隔离并生成可恢复汇总报告 |
| 已有 Job | `status` 查看下一步，`verify` 校验，`resume` 恢复转写，`continue` 完成端到端目标 |

纯字幕文件可以直接创建 Job；一个 Job 仍然只能生成一个目标语言。

## 端到端工作流

```mermaid
flowchart TD
    A["本地媒体或 YouTube URL"] --> B["检查输入并创建 Job"]
    B --> C{"有可用源字幕吗"}
    C -->|"有"| D["导入源字幕"]
    C -->|"没有"| E["获取或规范化音频"]
    E --> F["Whisper 词级转写"]
    F --> G["分段生成源 cue"]
    D --> H["源字幕 QA"]
    G --> H
    H --> I{"请求目标语言吗"}
    I -->|"否"| J["输出 source.srt"]
    I -->|"是"| K{"已有译文字幕吗"}
    K -->|"有"| L["映射译文到源 cue"]
    K -->|"没有"| M{"API 配置完整吗"}
    M -->|"是"| N["逐批翻译并保存进度"]
    M -->|"否"| O["生成 Agent 批次并等待回填"]
    O --> L
    N --> L
    L --> P["译文 QA 和双语 SRT/ASS"]
    P --> Q{"请求烧录吗"}
    Q -->|"否"| R["工作流完成"]
    Q -->|"是"| S["FFmpeg 烧录和媒体 QA"]
    S --> R
```

## 常用命令

完整本地流程：

```bash
uv run --no-sync xvt run "/path/to/video.mp4" \
  --output "/absolute/output" \
  --language en \
  --target-language zh-CN \
  --provider auto \
  --burn \
  --events-jsonl
```

YouTube 平台字幕优先：

```bash
uv run --no-sync xvt run "https://www.youtube.com/watch?v=VIDEO_ID" \
  --output "/absolute/output" \
  --youtube-subtitles \
  --subtitle-language en \
  --allow-auto-subtitles \
  --target-language zh-CN \
  --burn
```

已有源字幕和译文字幕：

```bash
uv run --no-sync xvt run "/path/to/video.mp4" \
  --output "/absolute/output" \
  --subtitles "/path/to/source.srt" \
  --translation-subtitles "/path/to/translated.srt" \
  --target-language zh-CN \
  --burn
```

恢复和诊断（优先 `status` → `continue`）：

```bash
uv run --no-sync xvt status "/path/to/job.json"
uv run --no-sync xvt status "/path/to/job.json" --json   # recovery_plan.advance_steps
uv run --no-sync xvt continue "/path/to/job.json" --events-jsonl
uv run --no-sync xvt verify "/path/to/job.json" --json
# advanced：仅转写 checkpoint
uv run --no-sync xvt resume "/path/to/job.json" --events-jsonl
uv run --no-sync xvt preview "/path/to/job.json" --cue 18 --duration 15
uv run --no-sync xvt batch "/path/to/input-directory" --output "/absolute/output"
uv run --no-sync xvt batch-continue "/path/to/batch.json" --events-jsonl
```

Agent 翻译回填：

```bash
uv run --no-sync xvt apply-translation "/path/to/job.json" \
  "/path/to/translation-response.json" \
  --target-language zh-CN

uv run --no-sync xvt continue "/path/to/job.json"
```

局部重译与 Agent 回填：

```bash
uv run --no-sync xvt retranslate-cues "/path/to/job.json" \
  --cue 18 --target-language zh-CN --provider agent

uv run --no-sync xvt apply-cue-retranslation "/path/to/job.json" \
  "/path/to/cue-retranslation-response.json" \
  --cue 18 --target-language zh-CN

uv run --no-sync xvt continue "/path/to/job.json"
```

## Job 模型

Stage 包含：

```text
inspect
acquire
normalize_audio
import_subtitles
transcribe
align
segment
translate
subtitle_qa
render
burn
media_qa
```

Stage 状态为 `pending`、`running`、`awaiting_input`、`succeeded`、`failed` 或 `skipped`。

典型 artifact 包含：

```text
media.json
remote.json
audio.wav
words.json
source-cues.json
source.srt
translation-batches.json
translation-progress.json
translation-response.json
translated-cues.json
translated.srt
bilingual.srt
bilingual.ass
preview.ass
preview.mp4
qa.json
qa-translated.json
burned.mp4
media-qa.json
```

`job.config.workflow` 保存目标语言、请求产物、翻译提供方、模型、API Key 环境变量名、分段预设、烧录参数和派生指纹。它不保存 API Key 或代理凭据。

批处理使用独立的 `batch.json` schema v1，不改变 Job schema。报告位于 `output/batches/<batch-id>/batch.json`，保存固定输入清单、共享工作流参数、每项 Job 文件、产物、状态、脱敏错误和恢复计划。批处理不复制媒体或字幕算法。

## 关键设计决定

1. CLI 是唯一业务内核。Skill 和未来 App 都只做调用与展示。
2. Python 继续作为当前实现语言。暂时没有迁移到 TypeScript 的收益，App 层可以使用 React 和 TypeScript。
3. 时间轴优先于翻译文本。任何翻译优化都必须保持 cue ID 和时间不变。
4. 单目标语言对应单 Job。多语言需求创建多个 Job，避免现在就改 schema 和 artifact 命名。
5. `resume` 和 `continue` 保持独立。一个处理转写 checkpoint，一个处理用户目标。
6. 平台字幕和用户字幕属于可跳过 ASR 的输入，不是 ASR 结果的附属品。
7. Agent 降级是明确的等待状态。缺少 API 配置时不能假装端到端任务已经完成。
8. 硬件编码回退只识别编码器错误。字幕文件、路径或滤镜错误必须原样失败。
9. 批处理只做串行编排；每个输入仍由独立 Job 和原有恢复计划负责状态与产物正确性。

## 已知限制

- 翻译后的目标文本若超过硬显示阈值仍由 QA 阻断；自动重切并重新翻译尚未实现。
- 新 Job 默认生成单遍语义组译文，也可显式选择 `--translation-quality polished`；Agent 主链真实 E2E 已通过，但默认切换仍需多样本质量与成本对照。
- `rebuild-source` 只适用于有媒体的 Job；纯字幕 Job 的源字幕问题仍需人工修复。
- 高级命令 `retranslate-cues` 已与主翻译共用传输/结构重试和 retry 事件语义，但仍只在全部选定 cue 翻译完成后写最终响应；中途失败会重复请求该次命令中已成功的批次，因此不会提前发送意味着已持久化的 `translation_batch_completed`。端到端主翻译的逐批 checkpoint 不受影响。
- 回填失败后的等待保持已修复（见「翻译」一节：用户模式的 `apply-cue-retranslation` / `apply-translation` / `import-translation` 校验失败不改写 stage，`cue_retranslation_waiting` 另外接受 `failed` 以兜住 API 模式重译失败与历史 Job）。仍存在的限制：stage 语义不区分「重译失败」与「等待被放弃」，放弃一次已请求的重译没有专门命令，靠重跑 `retranslate-cues` 重建 manifest，或直接开一轮整轮翻译取代它（`prepare_agent_translation` 会移走重译 manifest，且在途整轮请求优先）。用户模式的回填错误也不在 Job 上留痕（只有命令行报错，stage 不记 `error`），排查要看命令输出而不是 `job.json`。等待期间 `TRANSLATE` stage 停在 `awaiting_input` / `failed` 是表象，不影响判定。
- 目录批处理当前串行执行，不支持后台队列、目录监控或每项不同参数；请求烧录时目录只能包含视频，音频和纯字幕输入必须拆成不烧录的独立批次。
- 不支持远程执行或多租户 Web 服务。
- 没有可视化校对界面和桌面 App。
- `continue` 只适用于带 `job.config.workflow` 的端到端 Job。旧 Job 继续使用原子命令。
- 字面花括号在渲染、导出和导入三侧已闭环（导入端按实际格式分叉，SRT/VTT 保留、ASS/SSA 剥除）。仍存在的边界：SRT/VTT 正文里 `{\…}` 形态的字面文本（`{\an8}`、`{\alpha}` 一类）与真覆盖标签无法区分，仍会被剥除，`{` 与 `\` 之间有空白也一样；SRT 文本里的字面 `\N` 仍被当作换行标签转换。

## 后续任务规划

### Q1-Q6.1：字幕质量与自适应源字幕

完整方案已经过架构终审并落在 [字幕质量与自适应源字幕技术方案](docs/SUBTITLE_QUALITY_PLAN.md)。Q1–Q6.1 已按顺序完成：翻译传输可靠性 → 新 Job 自适应源字幕 → 已有 Job 源字幕重建 → 语义组翻译 → 可恢复字幕润色 → 语义校对与一次修复 → 人工修订与提示词加固。R2 CLI 收敛也已完成；polished Agent 主链真实 E2E 已完成且默认保持 standard + faithful。Q6.2 剩余的模型收敛实测需单独授权，因此先推进 R3.1：Phase 1 协议快照回归网、Phase 2 函数级 import 环消解、Phase 4a（translation + retranslate 批次循环单份化）、Phase 4b（polish 接入共享循环 + `TRANSLATION_FALLBACK` 收敛）与 Phase 4c（semantic 接入共享循环，Phase 4 收尾）均已于 2026-10-17 完成，四条链路至此共用 `run_provider_batches` 一份循环骨架；下一步是 Phase 5（语义链路编排单份化：初审 → 可修复？→ 修复 → 复审 → 确认/等待这套决策逻辑目前仍是各写各的）。

关键兼容性决定：不新增 `StageName`，Job schema 继续为 v1；润色和语义校对通过 artifact、事件和现有 stage 的细分状态表达。旧 Job 缺少新配置时保持当前平台字幕/单遍翻译行为。GUI 延后到本方案完成真实 E2E 验收之后。

**Q1：翻译传输可靠性已完成。** `RemoteDisconnected`、连接重置、分块传输中途断开（`IncompleteRead`）和 TLS 中途终止（`SSLEOFError` 等）等传输错误已进入 retryable 分类，其余 `HTTPException` 以不可重试依赖故障退出而不再穿透为 traceback，证书与握手类 `SSLError` 保持立即失败；传输重试与结构纠错使用独立预算，默认分别为 4 次和 1 次；主翻译与局部重译共用重试实现和 retry 事件语义。

**Q2：新 Job 自适应源字幕已完成。** 平台字幕只在显式 `--youtube-subtitles` 下请求；`--source-strategy auto|subtitles|asr` 写入新 Job 顶层配置。`auto` 直接采用通过 QA 的人工候选，对失败候选先对齐再回退 ASR；自动候选在翻译/烧录目标中使用 ASR，源字幕-only 且 QA 通过时可直接采用。候选 QA 和选择写为 `source_candidate_qa`、`source_selection` checksum artifact，并以 `source_selected` 事件记录；平台尾 cue 仅在超出媒体不超过 500ms 时裁剪。旧 Job 缺少策略仍走历史路径。

**Q3：已有 Job 源字幕重建已完成。** advanced `xvt rebuild-source JOB --strategy asr [--cookies-from-browser ...] [--proxy ...]` 显式重建，不由 `continue` 自动执行。它复用校验通过的词级转写、规范化音频和本地或已下载媒体；远程缺少可用 checkpoint 与媒体时才重取。命令持久化 ASR 策略与修复意图、关闭对齐，失效 `source_candidate_qa`、`source_cues`、`source_selection`、`translation_resegmentation` 及全部下游产物，并保留 `input_translation_subtitles` 以按新 cue 重新验证映射。修复中断后 `status` 保持显式重试，成功后端到端 Job 用 `continue`，旧原子 Job 用 `translate`。`status` 仅在非 ASR 源字幕 QA error 且可重建时首次给出建议，不对 ASR 自身 error、`needs_review` 或译文 QA 重复建议重建；不升级 schema 或新增 stage/事件类型，复用既有 artifact/stage、`source_selected` 和 `cli_completed`。

**Q4：语义组翻译已完成。** 翻译前由确定性代码建立语义组，句末标点或 650ms 间隔结束组；组上限为 `min(20, batch_size)` 个 cue 或 30 秒，超限依次按软标点、连接词前和最大停顿择边，单个超长 cue 单独成组。provider 以完整组装箱，批次外前后各一个完整相邻组只读；`semantic_groups` 保持为批次协议的可选加法，响应继续严格只覆盖当前 `items`。prompt v3 和 grouping version 进入 API 指纹以重置旧 API checkpoint，并写入 Agent manifest 供消费者识别；已等待输入的旧 Agent Job 仍按严格 ID 契约兼容回填，重新准备批次后才获得语义组。Q4 不新增 stage、artifact、事件或 Job schema，默认翻译风格继续为 `faithful`，局部重译使用 singleton groups。

**Q5a：已有译文可恢复润色已完成。** `polish-translation` 为现代端到端 Job 保存 `translation_draft_cues`，持久化 `translation_quality=polished`，并以 `translation_polish_batches` / `translation_polish_progress` / `translation_polished_cues` 表达 Agent/API 恢复状态。`status`、`continue` 和 `batch-continue` 共用 `polish` / `await_agent_polish` 计划；局部重译和重新导入译文会重建 draft 并失效旧润色结果。润色复用 `translate` stage 与批次事件的 `phase=polish`，不升级 Job schema。

**Q5b：新 Job 翻译质量模式已完成。** `run`、`batch`、`translate` 和 `prepare-translation` 支持 `--translation-quality standard|polished`，默认仍为 `standard + faithful`。API polished 路径按 draft -> polish 保存两阶段 checkpoint；Agent manifest 使用 `task=translate_and_polish`、task version 和可重算 fingerprint，一次回填登记 progress 与 polished 候选。Agent 无 draft 证据链可从完整 progress 确定性补齐 polished 候选，且不会被误判为 Q5a 的 draft 修复。旧 WorkflowConfig、原子 Job、batch manifest 和无 task Agent manifest 继续按 `standard`。

**Q6：语义校对与一次修复已完成。** polished 候选必须经过 `meaning_error`、`omission`、`terminology`、`awkward_join`、`source_suspect` 五类结构化 review；除 `source_suspect` 外的问题按完整语义组最多修复一次并最终复查。API 初审、修复和复审逐批 checkpoint，并使用阶段对应的结构纠错提示；Agent 使用严格绑定 Job、候选、版本和批次的 `task=semantic_review_and_repair` manifest，通过 `apply-semantic-review` 一次性校验后回填，无效响应不会破坏等待状态。初次报告含可修复问题时不能提前确认；`source_suspect` 在无关修复后继续保留；最终报告只有在初次报告、修复 checkpoint 和修复候选 checksum 均有效时才能批准。新增四类 checksum artifact，`status` / `continue` / batch recovery 区分 Agent 输入等待和人工语义确认等待。确认绑定 source、候选、报告、prompt version 与精确问题集合，且后续确定性字幕 QA 仍可阻断。专项方案见 [Q6 技术方案](docs/Q6_SEMANTIC_REVIEW_PLAN.md)。

**Q6.1a：人工译文修订已完成。** Q6 等待人工复核时生成 fingerprint 绑定的 JSON/SRT 模板；两种载体严格保留完整模板 ID 集合。`apply JOB REVIEW --continue` 可一步回填并继续，修订记录持久化 `changed_ids`，只重新复查受影响的完整语义组；不会重跑翻译、润色或自动修复。候选还由 checksum 绑定的 `translation_candidate_qa` fail-closed 门禁保护，只有目标译文可修问题进入模板。源文修改会失效译文及下游，因此拒绝 `--continue`。详见 [Q6.1 方案](docs/Q6_1_HUMAN_REVISION_PLAN.md)。

**Q6.1b：源文修订与多路径引导已完成。** `prepare-source-review JOB --cue ID` 生成绑定当前 source checksum 和精确 ID 集合的 JSON 与同 fingerprint SRT；`apply-source-review-fixes` 接受任一载体且只修改 source text，保留 cue 时间轴、words、置信度和 provenance。SRT 用固定文件名承载 fingerprint，按原时间轴还原 cue ID；改时间轴、增删 cue、注释 ID 不符、空文本或改名均被拒绝。接受结果以 fingerprint 文件保存，并在一次 Job 原子保存中切换 `source_review_revision`、source cue、`selected_kind=human_reviewed` 的选择记录、源 QA 和可用 SRT；QA 阻断时不登记 source SRT，确认后 `continue` 从人工 cue 恢复。旧翻译、润色、语义报告、译文修订、渲染和烧录全部失效，上游媒体与转写证据保留；导入译文任务明确拒绝。Q6 的 `source_suspect` 现在会自动准备源文模板，`status` 优先源文修订并同时提供译文修订和直接确认；旧等待 Job 由 `continue` 懒生成，避免在恢复命令中展开大量 cue ID。

**Q6.1c：提示词加固已完成。** API 与 Agent 统一把完整语义组作为连续话语处理，翻译与润色允许同组内协调语序、指代和连接词但不跨组移动含义；专名、产品名、代码标识符和术语没有明确证据时不得猜测修正。语义校对明确五类问题边界，解析器拒绝无变化建议、`source_suspect` 非空建议和单 cue `awkward_join`。translation/polish/review/repair prompt 当前为 v5/v3/v4/v3；旧 checkpoint 与带版本指纹的 Agent manifest 安全失效。人工修订 Agent manifest 可只复查 `changed_ids` 命中的完整语义组，相邻组只读。2026-08-08 的历史真实 API polished 回归证据保持有效；2026-10-11 Agent 主链 E2E 已完成，默认保持 standard + faithful；下一步验证 Q6.2 语义复查收敛。

**简中模型译文标点收口已完成。** 初译、一次翻译加润色、显式润色、语义修复和局部重译共用同一写入边界：消除全角 `，`、`。`，句中位置按需替换为一个半角空格以保留 `OpenAI GPT-5` 一类词边界，行尾与换行前直接移除；模型把省略号写成的连续全角句号归一为单个 `…`；问号、感叹号、分号、冒号、破折号、ASCII 标点一律保留，不折叠模型原有换行。左右同形的引号（`"`、`'`、`＂`、`＇`）按上下文推断开闭方向，英文词内撇号和数字后的尺寸符号不参与引号计数，补出的空格始终落在引号之外。源 cue 末尾为问句而译文缺少句末语义标点时补 `？`，位置镜像源文中问号相对末尾闭合符号的位置（源文 `(ready?)` 得到 `（准备好了吗？）`，源文 `Is it (a)?` 得到 `是吗（a）？`），译文闭合符号少于源文时按译文实际数量收敛。语义审查把半角空格和 cue 边界视为有效分隔，不会仅因缺少全角逗号或句号报告 `awkward_join`。处理范围严格限于显式 `zh-CN` / `zh-Hans` 等简中标签；导入译文、人工译文修订、源字幕、bare `zh`、繁中及其他目标语言保持不变。恢复中的 API checkpoint 与 Agent 回填通过相同路径，保证重放文本和 checksum 一致。

### R 系列：架构改进与工程加固

2026-08-02 的全量审查（基线 `ab28aff`）产出 [架构改进与工程加固技术方案](docs/ARCH_IMPROVEMENT_PLAN.md)，含 F1–F15 审查发现与 R1/R2/R3 分阶段任务。当前进度：

- **已完成（PR #67）**：真实 Job 复现的两个恢复语义矛盾——R2.5 字幕 QA 确认后 `translate` stage 收口（2026-08-01 polished E2E Job 复现并实证修复）、R2.4 烧录成品缺失误报"媒体 QA 未过"的文案拆分（2026-07-16/18 Job 复现）。
- **已完成（PR #68，堆叠于 #67）**：R1 全部四项——CI 增加 Python 3.11/3.13 矩阵与 `uv build`、补打并推送 `v0.1.0` / `v0.1.1` tag 且 AGENTS 记录发版流程、`tests/test_pipeline_imports.py` 守卫 pipeline 依赖方向、16 个跨模块 `_` 前缀符号公有化。R1.3 落地时发现 semantic ↔ review/revision 存在函数级惰性 import 打破加载期循环，留待 R3.1 一并消解。
- **已完成**：R2.1 `_run_command` 分发拆解——26 个命令分支提取为独立 `_cmd_<name>` 函数 + `_COMMAND_DISPATCH` 字典分发表，`_run_command` 从 622 行缩减至 4 行查表逻辑，纯搬移不改行为。
- **已完成**：R2.2 统一 `xvt apply JOB RESPONSE` 路由——task 字段优先、Job 等待步骤回退的两级路由，`--target-language` 自动解析；局部重译是显式例外：`apply-cue-retranslation` 需要 `xvt apply` 拿不到的 cue ID 与目标语言，因此无论响应带不带 `task`（协议一致的重译响应只有 `fingerprint` + `items`，靠「有有效重译 manifest 且 `translate` 在等输入」识别）都以 `invalid_input` 报错并给出可复制的手工命令；原 `apply-*` 命令全部保留为稳定入口，`status` 改为优先推荐统一入口。12 个新增路由测试（含真实 Job 零 mock 集成测试与路由表同源守卫）。
- **已完成**：R2.3 译文与源文修订 SRT 人工通道——两类 JSON 模板旁均写出同 fingerprint 的 `*-review-<fp12>.srt`（正文为 `#<cue id>` 注释行加当前文本），可用任意字幕工具编辑；对应 `apply-*-review-fixes` 按后缀识别，统一入口 `xvt apply` 按固定文件名路由。SRT 的 fingerprint 由文件名承载，cue ID 按 `(start_ms, end_ms)` 精确匹配还原，改时间轴、增删 cue、注释与时间轴不符均在解析边界被拒。JSON/SRT 汇聚为同一 `changes` 语义；旧 Job 只有 JSON 时按同一 fingerprint 补齐 SRT，`status` 的 `input:` 一并列出两种载体。
- **已完成**：R2.4 两阶段——`relink-burned-output` 仅以 checksum 有效的 v2 burn proof（绑定 fingerprint、成品 checksum 和实际源媒体内容 checksum）、媒体 QA 与候选文件 checksum 证明身份后更新成品路径；legacy fingerprint 不得直接 relink，需 `continue` 重烧生成 v2 proof；重复 `burned_video` fail closed，普通 `status` 不会为 v2 proof 额外计算源媒体内容 checksum。恢复导航在其余静态条件满足、但已登记成品缺失或 checksum 失效时返回 `awaiting_input`，首选重新关联、备选显式重烧；建议仅表示可能可关联，relink 会复核源媒体内容。`batch-continue` 将该状态作为停止态，不自动重烧。媒体 QA、proof 或 fingerprint 无效仍为 `retryable`。polished 质量另加一条限制：只有语义候选已收尾（语义校对通过且 `translated_cues` 与最终候选一致）才给 relink 导航，否则 continue 的收尾会改写译文 cue、让刚关联的成品指纹失效。详见 [R2.4 技术方案](docs/R2_4_BURNED_OUTPUT_RELINK_PLAN.md)。
- **待做**：R3.1 的 Phase 1/2/4a/4b/4c 已完成（四条链路共用 `run_provider_batches`）；Phase 5 按 review 建议降级为 O1 的两个纯搬移 helper，R3.2 建议延后或取消。Q6.2 剩余的模型收敛实测已于 2026-10-17 落槌移出近期计划（见 [路线图](ROADMAP.md) 的「暂不规划」）。polished Agent 主链 E2E 已通过，默认保持 standard + faithful。

全部改进不触碰不可破坏约束，不改变磁盘协议与旧 Job 兼容性。另：2026-08-01 已用公开素材完成 polished **API** 路径真实 E2E（语义校对零问题通过），2026-10-11 Agent 主链 polished E2E 已通过，默认决定继续保持 standard + faithful；多样本对照仍待做。R3.1/R3.2 之后再设计 Q7 翻译上下文与术语候选包：候选可参考全文、平台元数据和用户 glossary，但模型推断不得自动改写源文或成为强制术语；人工确认保持可选。Q7 完成后再进入消费现有事件、QA artifact、修订 SRT 和预览能力的可视化校对工作台。

### 已完成：CLI 阶段语义与完成摘要打磨

CLI 执行生命周期加固（Job/Batch 独占执行锁、SIGINT/SIGTERM 取消、进程组清理、持久化中断状态）与 JSONL 事件协议 v1 均已完成，详见 [路线图](ROADMAP.md) 和 [CHANGELOG](CHANGELOG.md)。真实 E2E 暴露的 P0（缺 `translated.srt`/`bilingual.srt`、`needs_review` 恢复不提示 acknowledge）已在本轮修复。Job/artifact 路径安全与输出覆盖策略已固定（见 `xvt.paths`）。这些能力已经满足未来界面消费要求，但在开发界面前先完成字幕质量闭环。

真实 YouTube 字幕翻译与烧录验收已确认功能链路、checksum 和媒体 QA 正常。2026-08-19 的 polished API E2E 完成到烧录和媒体 QA，最终 `verify` 校验 29 个 artifact、0 failed、0 warnings；人工回填后的每轮推进只做受影响语义组复查和候选 QA，没有重新翻译或重新润色。已确认及后续体验项如下：

- ~~YouTube 下载格式回退 / 403 引导（E2E P0-3）。~~ 已完成：格式串 `bv*+ba/b/18/best`；HTTP 403 提示 `continue` / 更新 yt-dlp / `--cookies-from-browser`。
- ~~下载平台字幕和下载源视频使用不同提示，避免重复显示“获取素材”。~~ 已完成（`acquire` detail）。
- ~~端到端任务不得先显示“跳过翻译/烧录”再实际执行；API 自动翻译准备批次时不得显示“等待用户输入”。~~ 已完成（计划跳过静默 + `await_input`）。
- ~~区分源字幕 QA/渲染与译文字幕 QA/渲染；`needs_review` 在 status 中勿仅显示为 `failed`（P1-7）。~~ 已完成：纯 review 持久化为 `awaiting_input`，error 才是 `failed`，stage 枚举未扩展。
- ~~翻译重试的人类可读输出补齐下一次尝试序号和等待时间。~~ 已完成。
- ~~完成摘要按最终视频、字幕和 Job 分类，并由 XVT 显示总耗时。~~ 已完成。
- ~~ASR 低 timing confidence / 确认摩擦（E2E P2）。~~ 已完成：ASR 派生 cue 的 `low_timing_confidence` 降为 warning；对齐与用户导入仍 review；硬溢出仍需确认。
- ~~Job/artifact 路径安全边界和输出覆盖策略。~~ 已完成：烧录/预览经 `xvt.paths` 拒绝覆盖源媒体、job 文件与其他 artifact；同 kind 可重写。
- ~~局部语义复查进度把稳定批次 ID 当作本轮序号，出现「批次 38/1」一类显示。~~ 已完成：事件保留 `batch` 作为稳定 ID，新增可选 `batch_index` 供人类进度显示。
- ~~`xvt apply ... --continue` 完成后夹带回填阶段的旧产物路径和 recovery 摘要。~~ 已完成：统一回填路由在继续工作流前抑制中间人类输出，只展示继续后的真实终态。
- **已完成（Q6.2 本地预检）**：`apply --check` 对人工 JSON/SRT 修订执行只读身份校验和确定性 QA，集中列出阻断的 cue、问题类型及宽度限制；正式 apply 在写入前重复预检。实际 ASS 行宽测量同步落地。Q6.2 剩余工作是下述语义复查收敛验证。
- **部分完成（Q6.2）**：历史任务的最小离线回归集已建立，见 [收敛基线](docs/Q6_2_CONVERGENCE_BASELINE.md)。未找回“可接受/无需报告却登记为问题”的留存实例；同一记录批次的历史检出差异也不能直接证明当前模型缺陷。下一步单独授权重复输入模型实验并补足源文 oracle，再评估提示词、响应纠错或审查策略。验收要求减少无效往返，同时保留真实 meaning/omission 问题及确定性 QA。YouTube 首次 403 和无变化建议被现有边界正确拦截，不列为当前正确性 Bug。

## 接手步骤

> 当前待办的首选入口是 [2026-10-17 review 发现清单](docs/REVIEW_FINDINGS_2026_10_17.md)，它比 ROADMAP 更具体、带锚点与触发条件。



1. 阅读 [AGENTS.md](AGENTS.md)、[README.md](README.md)、[架构说明](docs/ARCHITECTURE.md)、[恢复与回填契约](docs/RECOVERY.md)、[路线图](ROADMAP.md) 和 [贡献指南](CONTRIBUTING.md)。
2. 运行 `uv run --no-sync xvt --help`，确认命令面与本文一致。
3. 运行 Ruff 和全量测试，确认基线没有漂移。
4. 运行 `git status --short --branch -uall`，确认当前分支和 `origin/main` 的同步状态。
5. 从后续任务规划中选择一个小阶段，先提交方案和验收标准，再开始编码。
6. 修改完成后同步相关文档，并更新本文件的测试数量、当前限制和下一步。

基线验证命令：

```bash
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pytest -q
uv run --no-sync xvt --help
```

真实 YouTube、API 和烧录验收不应默认执行。需要运行时，先确认公开测试素材、输出目录、网络权限和用户凭据，完成后只记录结论，不记录密钥、Cookie、代理凭据或个人文件路径。
