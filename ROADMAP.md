# Roadmap

本路线图只记录尚未完成或仍在演进的能力。已实现功能见 [README](README.md) 和 [CHANGELOG](CHANGELOG.md)。

## 近期

- **A2 待立项：翻译预算按阅读单位计。** 翻译侧预算 `max_target_characters` 改按阅读单位计并在 prompt 解释口径——属协议变更（升 prompt 版本、重生成协议快照、在途 API checkpoint 重置），等有「模型因预算过紧砍掉术语」的实测样本再动。QA 侧的阅读单位口径（`textwidth.reading_units`）已先行落地。
- **译文超宽的 cue 内确定性换行**（2026-10-22 拍板，方案见 [docs/TARGET_LINE_WRAP_PLAN.md](docs/TARGET_LINE_WRAP_PLAN.md)）：译文整段超显示上限时先按禁则切成最多 2 行，切得开就通过并记 `target_wrapped` warning，切不开才 `review`；ASS 写出与 libass 测量共用同一份切分，SRT 不动，不碰 cue 的 ID / 时间 / 数量。它把「已知限制」第 1 条从死路变成有出路，来自 2026-10-22 对 BaoCut 的对标调研（经 Fable 5.1 核验）——BaoCut 实测结论是「先定自然译文、再确定性切行」，而不是让模型重译缩短。
- 2026-10-17 全面 review 的发现清单见 [docs/REVIEW_FINDINGS_2026_10_17.md](docs/REVIEW_FINDINGS_2026_10_17.md)：发起时为 1 条阻断级、10 条应修与一批可优化项，**2026-10-18 已基本清空**（含随后真实验收新发现的 P/B/C 系列）。仍未做的只剩三类，清单里逐条写了理由：① 卡在语料上的质量启发式（尾部幻觉 QA 门、译文切分的词法边界）——都只有 n=1 素材、零负样本，需要 10–15 份覆盖 ja/zh/en 且含 3–5 份负样本的真实转写才能动；② 结构性重构（**本桶已于 2026-10-20 清空**：对齐/分段的 3–4 份副本、`apply --continue` 重复做 QA 与渲染；其中「语义收口 helper」已于 2026-10-20 做完——`semantic.py` 的终态收口与问题分拣各收敛为一份模块私有 helper（`_conclude_semantic_review` / `_partition_issues`），零行为变化、协议快照零 diff；「`aligned_report` 不落盘」已于 2026-10-20 修完，对齐后 QA 报告现在登记为 `aligned_subtitle_qa` artifact，它解锁的导航缺口（强制对齐被对齐后 QA 拒绝时 `status` 推荐一条必然失败的 `continue`）同日修完；`transcription.py` 突破 2000 行后按 F13 约定的三簇拆成子包也已于 2026-10-20 做完——实际落成五个子模块（三簇 + 一个零兄弟依赖的 `shared` 叶子 + 一层入口分派），纯搬移、零行为变化）；③ 本文件「近期」列的新功能。该清单仍带锚点与触发条件，是查「某条为什么没做」的首选入口。R3.1 已完成 Phase 1/2/4a/4b/4c（四条链路共用 `run_provider_batches`）。**review 建议的两条已于 2026-10-20 拍板**：**Phase 5 关闭**（轻量替代 O1 做完后，两条语义链路在「初审 / 修复 / 复审」三步上已无逐字重复可搬，再合要先造一层抽象去覆盖约 0 行重复）；**R3.2 取消**（它最大的卖点「planner 与 executor 的人工对齐义务消失」实证不成立——本轮同类缺陷修了 9 次，每次都是「Job 处在没有自动步骤能推进的状态」，不是缓存命中问题；唯一仍值钱的「执行器改按 `advance_steps[0]` 分派」已单列为小项保留）。R3.3 的评估闸门随之改写，结论是前提仍成立、保持待评估并给了新的触发条件；理由与取舍见 [架构改进与工程加固技术方案](docs/ARCH_IMPROVEMENT_PLAN.md)。

- Cookie YouTube 视频画质比较、固定格式下载及实际媒体验证已实现，4K/8K 不设上限；受限内容的可验证登录错误分类与低清交互提示仍待评估。

- 校对体验后续：实际 ASS 行宽测量与人工译文只读预检已完成；继续评估语义疑似源文误判及下载清晰度降级提示，保留确定性 QA 与独立语义确认。

- ~~完成 CLI 执行生命周期加固：Job/Batch 独占执行、SIGINT/SIGTERM 取消、外部进程组清理和持久化中断状态。~~ 已完成：flock 全生命周期独占锁（Job/batch 自首次持久化持有至终态，含在途 `run` 与 batch 创建期）、进程组清理、命令级信号作用域（SIGTERM 143、二次信号幂等）、协作式取消，均有跨进程测试覆盖。
- ~~固定桌面端消费的 JSONL 协议版本。~~ 已完成：`EventType` 枚举 + `v=1` 信封 + `docs/EVENTS.md` 契约。
- ~~Job/artifact 路径安全边界和输出覆盖策略。~~ 已完成：`xvt.paths` 统一校验；烧录/预览可导出到 Job 外，但不得覆盖源媒体、`job.json` 与其他已登记 artifact（同 kind 可重写；含 hardlink/`samefile`）。
- 在可视化校对界面之前，按 [字幕质量与自适应源字幕技术方案](docs/SUBTITLE_QUALITY_PLAN.md) 完成以下串行阶段：
    - ~~Q1：翻译传输可靠性，补齐断连分类并区分传输重试与结构纠错预算。~~ 已完成：网络断连和响应读取错误统一为可重试传输故障；传输默认 4 次重试，结构纠错固定 1 次（后续已提升为 2 次），主翻译与局部重译的重试语义一致；兼容 API 默认批次 25 个 cue、请求超时 180 秒。
    - ~~Q2：新 Job 自适应源字幕，完成人工/自动字幕候选 QA、对齐和 ASR 降级。~~ 已完成：显式平台候选请求、`auto|subtitles|asr` 策略、候选 QA/选择 artifact 与人工候选对齐或 ASR 降级。
    - ~~Q3：已有 Job 源字幕重建，通过显式修复命令复用 checkpoint 并失效下游。~~ 已完成：`rebuild-source --strategy asr` 显式重建并保留可验证的译文输入供重新映射。
    - ~~Q4：语义组翻译，避免 provider 批次和中文表达在错误边界断开。~~ 已完成：确定性组按完整组装箱，批次外相邻组只读上下文，prompt/grouping version 保护恢复；默认保持 `faithful`。
    - ~~Q5：可恢复字幕润色。~~ 已完成：Q5a 支持旧译文显式补跑、API checkpoint、Agent 回填和工作流恢复；Q5b 接入新 Job 的 `standard|polished` 质量参数、API draft -> polish 和一次 Agent 翻译+润色。专项设计见 [Q5 可恢复字幕润色技术方案](docs/Q5_TRANSLATION_POLISH_PLAN.md)。
    - ~~Q6：语义校对、最多一次定向修复和独立人工确认。~~ 已完成：polished 候选经结构化语义校对、完整语义组一次修复与最终复查；API/Agent 均可恢复，人工确认绑定候选和报告且不能绕过确定性 QA。专项设计见 [Q6 技术方案](docs/Q6_SEMANTIC_REVIEW_PLAN.md)。
    - ~~Q6.1：人工校对交互与提示词加固。~~ 已完成：译文/源文修订、多路径引导、`apply --continue`、候选确定性 QA、`changed_ids` 范围复查及 `needs_review → awaiting_input` 状态语义；人工译文修改后不会重跑翻译、润色或自动修复。专项设计见 [Q6.1 方案](docs/Q6_1_HUMAN_REVISION_PLAN.md)。
- 按 [架构改进与工程加固技术方案](docs/ARCH_IMPROVEMENT_PLAN.md)（R 系列）推进：~~R1 工程加固（CI 打包验证与多 Python 版本、发布 tag、依赖守卫测试、共享符号公有化）~~ 已完成（PR #68）；~~真实 Job 复现的 R2.4 恢复文案与 R2.5 stage 收口~~ 已完成（PR #67）；~~R2.1 `_run_command` 分发拆解~~、~~R2.2 统一 `apply` 路由~~、~~R2.3 译文与源文修订 SRT 通道~~、~~R2.4 烧录成品重新关联（显式命令、恢复导航与批处理安全）~~ 已完成。R2.4 对缺失但静态条件满足的已登记成品返回 `awaiting_input`，首选重新关联、备选显式重烧；R3 架构演进包含统一 provider 传输、内容寻址 step 缓存、schema 演进评估和 config 单轨化。
- 后续按以下顺序推进，避免新增质量步骤后立即返工 provider、checkpoint 或界面协议：
    1. ~~使用公开素材完成 polished Agent 路径真实 E2E 与默认质量决策。~~ 2026-10-11 已完成英文人工字幕 → Agent 翻译润色与语义校对 → 1080p 整段烧录，24 个 artifact 校验通过；默认继续保持 `standard + faithful`。本次不覆盖 ASR 或有问题时的语义修复分支，多样本质量对照仍待开展。见 [验收记录](docs/POLISHED_AGENT_E2E_ACCEPTANCE.md)。
    2. ~~完成 Q6.2 剩余的语义复查收敛验证。~~ **2026-10-17 落槌：移出近期计划**，见「暂不规划」。已落地的部分（JSON/SRT 修订只读预检、实际 ASS 行宽测量、20 个历史 Job 的最小离线回归集）保持有效，见 [收敛基线](docs/Q6_2_CONVERGENCE_BASELINE.md)。
    3. 先完成 R3.1 统一 provider 传输与 R3.2 内容寻址 step 缓存，再设计 Q7 翻译上下文与术语候选包，避免为新增模型调用重复实现传输和恢复。R3.1 拆为 5 个独立可合阶段，Phase 1（协议快照回归网）/ 2（消函数级 import 环）/ 4a / 4b / 4c 均已于 2026-10-17 完成——四条链路现共用 `run_provider_batches` 一份批次循环；原 Phase 3 因实测只有 2 处 ~8 行重复而折入 4b，**Phase 5 已于 2026-10-20 关闭**，R3.1 至此收尾。**R3.2 已于 2026-10-20 取消**，所以这一条原先的「先 R3.1 → R3.2 再设计 Q7」不再成立：Q7 可直接设计，新增的模型步骤按既有 `run_provider_batches` + fingerprint checkpoint 接入。拆分与已更正的前提见 [架构改进与工程加固技术方案](docs/ARCH_IMPROVEMENT_PLAN.md)。
    4. Q7 只生成可审计的主题、专名和术语候选；用户 glossary 仍是强约束，平台元数据可作为高置信证据，模型推断不得自动改写源文或成为强制术语。人工确认保持可选，只有检测到高风险专名时才建议介入。**按三阶段推进，把协议变更隔离在最后一阶段**：**Q7a 已完成**（`xvt build-translation-context`，确定性抽取 + 可审核的 glossary 骨架导出，零模型调用、零协议影响、不进翻译 prompt 与指纹，只对拉丁字母源文有效）；**Q7b** 接模型推断腿（`run_provider_batches` + fingerprint checkpoint，API / Agent 双模式，新等待步骤要登记进 `AWAIT_STEPS` 与 `cli._STEP_ROUTES`），补上 CJK 与德语这两类确定性判据够不着的语料，仍不进翻译 prompt；**Q7c** 才把确认后的上下文接进翻译 prompt（prompt 版本升级、重生成协议快照、在途 API checkpoint 重置），届时 `translation_context` 成为翻译指纹的一部分。
    5. 增加可视化校对工作台，消费现有 JSONL 事件、`job.json`、QA artifact、人工修订 SRT 和预览能力，不复制媒体、时间轴或 QA 内核。
    6. WhisperX、Demucs 人声分离和更多 ASR 后端只作为独立质量基准候选；真实噪声音频证明收益明显后再决定是否引入依赖。

## 中期

- 根据真实批处理使用反馈评估并发上限和筛选规则；在此之前保持串行和显式输入快照。

## 暂不规划

- **Q6.2 剩余的语义复查收敛实测**（2026-10-17 落槌）。离线部分已完成并保留；剩下的「固定完整输入、重复调用当前模型测收敛」需要用户单独授权、用自己的凭据并产生真实 API 成本，还需要人工听原音补足源文 oracle。它不会自己推进，挂在待办里只会让清单长期显得有未完事项。**重新立项的条件**：用户决定授权并给出预算与公开素材；届时按 [收敛基线](docs/Q6_2_CONVERGENCE_BASELINE.md) 的「指标与后续验收」一节执行，不放宽确定性 QA、不以关键词过滤代替语义契约。
- 在单机 CLI 稳定前引入队列、服务端或多租户 Web 平台。
- 让翻译模型直接修改 cue ID、时间码或媒体处理参数。
- 默认读取浏览器 Cookie、代理或外部凭据。

## 取舍原则

- 时间轴正确性优先于自动化程度。
- 本地可恢复的 Job 优先于隐式后台状态。
- 先解决字幕可读性和 QA 闭环，再扩展 GUI 或批处理并发规模。
- 平台字幕是候选来源，不因成功下载就自动成为最终时间轴；自动降级仍必须通过确定性 QA。
- 显式 YouTube 字幕请求在 Cookie 查询缺少匹配人工字幕且无有效候选时匿名补查并匿名下载，访问方式可恢复；提取失败不能静默当成字幕不存在，已有候选保留来源分类、已有源 cue 不自动替换。
- Q1–Q6.1 质量链已实现；polished Agent 主链真实 E2E 已通过，默认仍保持 `standard + faithful`，待多样本质量、耗时与人工修订成本对照后再评估 `natural + polished`。Q2 不把候选参考文本发送给翻译提供方；未来 Q7 的模型推断术语也不得未经确认成为源文事实或强制翻译约束。
- 语义润色可以重新组织既有 cue 的文字，但不能修改 cue ID、数量、顺序和时间码。
