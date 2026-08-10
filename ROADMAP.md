# Roadmap

本路线图只记录尚未完成或仍在演进的能力。已实现功能见 [README](README.md) 和 [CHANGELOG](CHANGELOG.md)。

## 近期

- ~~完成 CLI 执行生命周期加固：Job/Batch 独占执行、SIGINT/SIGTERM 取消、外部进程组清理和持久化中断状态。~~ 已完成：flock 全生命周期独占锁（Job/batch 自首次持久化持有至终态，含在途 `run` 与 batch 创建期）、进程组清理、命令级信号作用域（SIGTERM 143、二次信号幂等）、协作式取消，均有跨进程测试覆盖。
- ~~固定桌面端消费的 JSONL 协议版本。~~ 已完成：`EventType` 枚举 + `v=1` 信封 + `docs/EVENTS.md` 契约。
- ~~Job/artifact 路径安全边界和输出覆盖策略。~~ 已完成：`xvt.paths` 统一校验；烧录/预览可导出到 Job 外，但不得覆盖源媒体、`job.json` 与其他已登记 artifact（同 kind 可重写；含 hardlink/`samefile`）。
- 在可视化校对界面之前，按 [字幕质量与自适应源字幕技术方案](docs/SUBTITLE_QUALITY_PLAN.md) 完成以下串行阶段：
    - ~~Q1：翻译传输可靠性，补齐断连分类并区分传输重试与结构纠错预算。~~ 已完成：网络断连和响应读取错误统一为可重试传输故障；传输默认 4 次重试，结构纠错固定 1 次，主翻译与局部重译的重试语义一致；兼容 API 默认批次 25 个 cue、请求超时 180 秒。
    - ~~Q2：新 Job 自适应源字幕，完成人工/自动字幕候选 QA、对齐和 ASR 降级。~~ 已完成：显式平台候选请求、`auto|subtitles|asr` 策略、候选 QA/选择 artifact 与人工候选对齐或 ASR 降级。
    - ~~Q3：已有 Job 源字幕重建，通过显式修复命令复用 checkpoint 并失效下游。~~ 已完成：`rebuild-source --strategy asr` 显式重建并保留可验证的译文输入供重新映射。
    - ~~Q4：语义组翻译，避免 provider 批次和中文表达在错误边界断开。~~ 已完成：确定性组按完整组装箱，批次外相邻组只读上下文，prompt/grouping version 保护恢复；默认保持 `faithful`。
    - ~~Q5：可恢复字幕润色。~~ 已完成：Q5a 支持旧译文显式补跑、API checkpoint、Agent 回填和工作流恢复；Q5b 接入新 Job 的 `standard|polished` 质量参数、API draft -> polish 和一次 Agent 翻译+润色。专项设计见 [Q5 可恢复字幕润色技术方案](docs/Q5_TRANSLATION_POLISH_PLAN.md)。
    - ~~Q6：语义校对、最多一次定向修复和独立人工确认。~~ 已完成：polished 候选经结构化语义校对、完整语义组一次修复与最终复查；API/Agent 均可恢复，人工确认绑定候选和报告且不能绕过确定性 QA。专项设计见 [Q6 技术方案](docs/Q6_SEMANTIC_REVIEW_PLAN.md)。
    - ~~Q6.1：人工校对交互与提示词加固。~~ 已完成：译文修订、源文修订与 `source_suspect` 多路径引导、API/Agent 语义组表达与校对契约加固。专项设计见 [Q6.1 方案](docs/Q6_1_HUMAN_REVISION_PLAN.md)。
- 按 [架构改进与工程加固技术方案](docs/ARCH_IMPROVEMENT_PLAN.md)（R 系列）推进：~~R1 工程加固（CI 打包验证与多 Python 版本、发布 tag、依赖守卫测试、共享符号公有化）~~ 已完成（PR #68）；~~真实 Job 复现的 R2.4 恢复文案与 R2.5 stage 收口~~ 已完成（PR #67）；~~R2.1 `_run_command` 分发拆解~~、~~R2.2 统一 `apply` 路由~~、~~R2.3 译文与源文修订 SRT 通道~~ 已完成。剩余 R2 CLI 收敛为 R2.4 确认通道评估；R3 架构演进包含统一 provider 传输、内容寻址 step 缓存、schema 演进评估和 config 单轨化。
- 后续按以下顺序推进，避免新增质量步骤后立即返工 provider、checkpoint 或界面协议：
    1. 完成 R2.4 确认通道评估；R2.3 源文修订 SRT 通道已完成。
    2. 使用公开素材完成 polished Agent 路径真实 E2E，并据此决定是否调整 `standard + faithful` 默认值；polished API 路径已完成真实 E2E。
    3. 先完成 R3.1 统一 provider 传输与 R3.2 内容寻址 step 缓存，再设计 Q7 翻译上下文与术语候选包，避免为新增模型调用重复实现传输和恢复。
    4. Q7 只生成可审计的主题、专名和术语候选；用户 glossary 仍是强约束，平台元数据可作为高置信证据，模型推断不得自动改写源文或成为强制术语。人工确认保持可选，只有检测到高风险专名时才建议介入。
    5. 增加可视化校对工作台，消费现有 JSONL 事件、`job.json`、QA artifact、人工修订 SRT 和预览能力，不复制媒体、时间轴或 QA 内核。
    6. WhisperX、Demucs 人声分离和更多 ASR 后端只作为独立质量基准候选；真实噪声音频证明收益明显后再决定是否引入依赖。

## 中期

- 根据真实批处理使用反馈评估并发上限和筛选规则；在此之前保持串行和显式输入快照。

## 暂不规划

- 在单机 CLI 稳定前引入队列、服务端或多租户 Web 平台。
- 让翻译模型直接修改 cue ID、时间码或媒体处理参数。
- 默认读取浏览器 Cookie、代理或外部凭据。

## 取舍原则

- 时间轴正确性优先于自动化程度。
- 本地可恢复的 Job 优先于隐式后台状态。
- 先解决字幕可读性和 QA 闭环，再扩展 GUI 或批处理并发规模。
- 平台字幕是候选来源，不因成功下载就自动成为最终时间轴；自动降级仍必须通过确定性 QA。
- Q1–Q6.1 质量链已实现；默认仍保持 `standard + faithful`，待 polished Agent 真实 E2E 后再决定是否切换为 `natural + polished`。Q2 不把候选参考文本发送给翻译提供方；未来 Q7 的模型推断术语也不得未经确认成为源文事实或强制翻译约束。
- 语义润色可以重新组织既有 cue 的文字，但不能修改 cue ID、数量、顺序和时间码。
