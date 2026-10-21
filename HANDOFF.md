# 项目交接说明

更新时间：2026-10-21

> 2026-10-20 及以前的日期条目、以及曾经逐字抄在这里的说明书五节，都已搬进 [HANDOFF 历史档案](docs/HANDOFF_ARCHIVE.md)。档案按日期倒序保留原文，**只供 `rg` / `grep` 考古，不要整份读入上下文**；与 [README](README.md) / [架构说明](docs/ARCHITECTURE.md) 不一致时以那两份为准。
>
> **体量约定**：本文件 ≤ 200 行且 ≤ 32KB，单条日期条目 ≤ 2KB，超出就把最旧的条目滚进档案。实证过程、否决记录、复现数据写进 `docs/REVIEW_FINDINGS_*.md` 或对应专项方案，这里只留结论、影响和链接。由 `tests/test_handoff_budget.py` 强制。

## 当前快照

| 项目 | 状态 |
| --- | --- |
| 包名 | `xvt-video-translate` |
| CLI | `xvt` |
| 版本 | `0.2.0`（2026-10-17 发布） |
| Python | 3.11 及以上 |
| 当前主线 | 先完成字幕质量与自适应源字幕闭环，再评估 App |
| Job schema | v1（端到端目标为可选 `config.workflow` / `WorkflowConfig`） |
| 自动化测试 | 1087 个测试、313 个子测试，全部通过（2026-10-21） |
| 静态检查 | Ruff lint 和 formatter 通过 |
| 打包 | wheel 和 source distribution 已验证 |
| CI | GitHub Actions（ubuntu + macos × Python 3.11/3.13 矩阵）跑 ruff、pytest、CLI 冒烟与 `uv build` |
| Git | 功能修改使用 `codex/` 开发分支，Review 通过后以 PR 合并 `main` |
| 默认 CLI 路径 | `run` → `status` → `continue`（批处理 `batch` / `batch-continue`） |

当前代码可以完成本地视频或 YouTube 视频的转写、翻译、双语字幕生成和可选烧录。端到端流程收敛到 `xvt run`；中断后先 `xvt status` 查看 `recovery_plan` / `advance_steps`，再 `xvt continue` 按持久化目标补齐。翻译与烧录推进与 status 共用 `plan_advance_steps`。由 `run` / `continue` 按持久化目标完成的烧录写入 checksum 保护的 `version=2` burn proof，绑定烧录 fingerprint、`burned_video` checksum 和实际源媒体内容 checksum；高级原子 `xvt burn` 不签发 workflow proof。仅有效 v2 proof 可让 `relink-burned-output` 重新关联已移动成品。若其余静态条件满足但已登记成品缺失或 checksum 失效，`status` 返回 `awaiting_input`，首选重新关联占位命令、备选显式 `continue` 重烧；这只表示成品可能可重新关联，执行时仍会校验实际源媒体内容。`batch-continue` 停在该状态，不自动重烧。旧/legacy fingerprint、媒体 QA/proof/fingerprint 无效仍为 `retryable`，只推荐重烧。

## 项目背景

这个项目要解决的是视频字幕翻译链路中最容易失控的三件事：时间轴、翻译上下文和最终显示质量。用户可以只提供视频，也可以同时提供源字幕、机翻字幕或译文字幕。系统需要识别哪些步骤可以跳过，并且始终以源 cue 的时间轴为准。

方案选择了 Python CLI 作为核心，原因是 FFmpeg、yt-dlp、Whisper 和字幕生态在 Python 中接入成本较低。Skill 只负责调用 CLI 或完成 Agent 翻译，不拥有时间轴和媒体处理逻辑。App 暂缓，未来只消费 `job.json` 和 JSONL 事件，不复制业务逻辑。

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

完整方案见 [字幕质量与自适应源字幕技术方案](docs/SUBTITLE_QUALITY_PLAN.md)。Q1–Q6.1 已按顺序全部完成：翻译传输可靠性 → 新 Job 自适应源字幕 → 已有 Job 源字幕重建 → 语义组翻译 → 可恢复字幕润色 → 语义校对与一次修复 → 人工修订与提示词加固；逐条完成记录见 [HANDOFF 历史档案](docs/HANDOFF_ARCHIVE.md)。polished Agent 主链真实 E2E 已通过，默认保持 `standard + faithful`。Q6.2 剩余的模型收敛实测需单独授权，已移出近期计划（见 [路线图](ROADMAP.md) 的「暂不规划」）。

关键兼容性决定：不新增 `StageName`，Job schema 继续为 v1；润色和语义校对通过 artifact、事件和现有 stage 的细分状态表达。旧 Job 缺少新配置时保持当前平台字幕/单遍翻译行为。GUI 延后到本方案完成真实 E2E 验收之后。

### R 系列：架构改进与工程加固

2026-08-02 的全量审查（基线 `ab28aff`）产出 [架构改进与工程加固技术方案](docs/ARCH_IMPROVEMENT_PLAN.md)，含 F1–F15 审查发现与 R1/R2/R3 分阶段任务。R1、R2 已全部完成，逐条记录见 [HANDOFF 历史档案](docs/HANDOFF_ARCHIVE.md)。

- **R3 进度**：R3.1 已收尾（Phase 1/2/4a/4b/4c 完成，四条链路共用 `run_provider_batches`；Phase 5 已关闭），R3.2 已取消，R3.3 保持待评估。结论、理由与重新立项的触发条件以 [路线图](ROADMAP.md) 和 [架构改进与工程加固技术方案](docs/ARCH_IMPROVEMENT_PLAN.md) 为准。

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
