# 项目交接说明

更新时间：2026-10-24（基线 `main` 的 `a1557f8`，本条改动在 `feat/download-space-precheck` 上待合并；写于 Claude Opus 5 会话）

2026-10-24 **完整视频下载前先做磁盘容量预检（B2 第一层）。** `download.py` 整条下载前此前没有任何容量估算，而 `REVIEW_FINDINGS_2026_10_17.md:386` 记过真实的「磁盘满导致 stage 永久停在 running」——失败发生在最贵的那一步且状态是坏的。现在 `_probe_video` 把所选格式的登记大小记进 `VideoSelection`，`_download_compared_video` 在选定一侧后、建临时目录前调 `_check_download_space`：不足则发新事件 `download_space_insufficient` 并以 `DependencyError`（退出码 3）失败，一个字节都不下、不留临时目录。门槛按**合并峰值**算（分离格式两份、单文件一份）；大小求和**按 `requested_formats` 走而不是 video + audio 相加**——单文件格式下那两者是同一份 dict，相加会翻倍并误拦空间充足的下载（有用例钉住）。只做证伪：拿不到大小就跳过，不为 `filesize_approx` 的估算误差再加余量（低估只是退化成现状，不产生新失败模式）。判定排在格式提取完整性护栏**之后**，`test_fail_closed_boundaries` 的 11 条边界一字未动。新事件进了 `tests/test_events.py` 的 golden（下载类事件此前没有一个在里面），文档字段表与实际载荷现在双向校验——改错文档字段名两个守卫都会红，已实测。覆盖面只到 Cookie YouTube 比较路径，剩余层见「下一步待办」第 1 条。

2026-10-24 **日语片假名词不再被展示换行从中间切开，`wrap.version` 升 2。** `_CJK_WRAP_TOKEN` 此前只让 Latin / 数字段整体不拆，片假名走 `\S` 单字一 token，`グラフィックス` 被切成 `グラフィッ|クス`；禁则表挡不住（小字假名与长音符在 `NO_LINE_START` 里，普通片假名之间照样放行）。新增 `_KATAKANA_RUN` 把片假名连续段与 Latin 段同口径整体算一个 token——**不是新启发式，是把既有规则补全到同一类对象**，`グラフィ|ックス` 与 `Open|AI` 坏在同一处。刻意排除 `゠` / `・` 与半角 `･` 三个词间分隔符（否则 `アイス・コーヒー` 切不开）；半角片假名纳入（`normalize_display_text` 不做半角转全角）；平假名与汉字不收（词法边界，要分词器，P7 ②）。**硬形态**：段放不下即 `None` 维持 review，与 Latin 单 token 同口径；退化可达但 20 条构造样本 0 命中、修好 5 条，仓库零份 ja 译文 artifact 所以那只是机制差异不是误伤率。`version` 1→2 的理由**不是**约束 4 被违反（`_reusable_rendered_layout` 复用的报告与 ASS 同源一致），而是不升则修复对在途 Job 不生效。升级影响：带 `layout` 块的报告重跑后 checksum 变，人工确认需重做。顺带补了 P7 ⑤ 那张表的交叉引用（该行在源文分段侧仍按 0/12 不做）与 `ARCHITECTURE.md` 漏改的「产出点三→四」。

2026-10-23 **YouTube 画质平局且仅 Cookie 侧提取不完整时改走干净的匿名侧（#145）。** `download._download_compared_video` 本来就有「Cookie 侧受损就回退匿名侧」的机制，但换路条件写的是**严格**更高画质，于是两侧画质相同时留在脏的 Cookie 侧、随后被「格式提取不完整」护栏整单拦下。YouTube 的 SABR 实验按账号开启后 Cookie 侧**每次**都报 `web_embedded ... formats have been skipped`、而匿名侧零警告且选中同一个 1080p，于是这类账号下**任何** YouTube 视频都卡死在这条护栏上（2026-10-23 真实端到端验收撞到，当时只能靠 yt-dlp 垫片绕过）。现在平局且仅 Cookie 侧脏时也换路，`reason` 记新取值 `anonymous_clean_extraction`。**护栏本身一个字未改**：`incomplete` 判定、标记清单与错误文案全部保留，脏的一侧永远不会被下载；匿名侧画质更低（那才是 `5ae28e8` 要防的静默降级）、匿名侧也脏、缺原音轨、音轨语言不一致四类仍然失败。真实验收（不带垫片、真实 Cookie 打真实 YouTube）：修复前报「视频格式提取不完整」，修复后以 `access=anonymous`、`reason=anonymous_clean_extraction` 拿到 1080p 原音轨，format_id 与 Cookie 侧选中的完全相同。方案见 [YouTube 下载画质方案](docs/YOUTUBE_DOWNLOAD_QUALITY_PLAN.md)。

2026-10-23 **字体缺字不再静默降级：点名到码位、cue 与腿，可见字符停烧录。** 此前 libass 报缺字只被子串匹配认出来，收成一条 `cue_id=0` 的 `layout_measurement_unavailable` warning，行宽 QA 整份退保守上限而报告照样放行，成品是一屏豆腐块。现在解析 libass 本就带着的码位与字体名（逐帧重复，去重后用 `render.FontGlyphError` 带出），按 cue 与腿产出 `font_missing_glyph`：可见字符 `review`（停 `needs_review`，`--acknowledge-subtitle-review` 可放行），零宽/控制字符与变体选择符 `warning`。缺字之外的测量失败文案与严重度一字未动；候选 QA 里缺字刻意不作为阻断项（否则语义收尾会停在「要求修订但模板里没有可改项」的死局，已加守卫测试）。`xvt doctor` 的字体检查从只探 zh-CN 扩到 zh/ja/ko/en 四种。顺带补上了 [2026-10-17 review 发现清单](docs/REVIEW_FINDINGS_2026_10_17.md) 欠的实证：五种缺字字符各真跑一次测量，确认缺字分支真的会触发。来源是 [BaoCut 增量复查](docs/reviews/baocut-rescan-2026-10-23.md) 的 B1。

2026-10-23 **人工修订模板的宽度标注补 `max_lines`（#141 的尾巴）。** `pipeline/revision.py` 的宽度标注此前只给 `limit`，而换行落地后那是「每行上限」，模板读者看到 36 会以为要把整句缩进一行。现在同一分支里补一个 `max_lines`（取 `segment.TARGET_WRAP_MAX_LINES`，不写死 2），只标在 `target` 的宽度项上——源文永不换行，给它标两行就是说谎。`candidate_qa.overflow_limits` 的正则一字未改（行数是代码常量，不从 message 解析），`recovery.py:282` 的等待原因文案零改动。已生成的模板不会被改写：`revision_fingerprint` 不含标注内容，要重新生成才带上该键。

2026-10-22 **BaoCut 对标落地三个 PR，剩余项见「下一步待办」。** 对开源剪辑软件 BaoCut（`/Users/jiangding/baocut`，Rust + Electron）做了转写/翻译链路与 CLI 两路调研并经独立核验，结论：可借鉴的是算法与协议而非其常驻 Runtime 架构；XVT 在 libass 真实行宽测量、Q6 语义校对、任务级 `recovery_plan` 三处强于它。据此落地：#140 HANDOFF 拆分归档（741 行→93 行，`tests/test_handoff_budget.py` 守卫）；#141 译文超宽先做 cue 内确定性换行（最多 2 行、禁则后检、QA 与 ASS 同源、`layout.wrap` 绑定，方案 [docs/TARGET_LINE_WRAP_PLAN.md](docs/TARGET_LINE_WRAP_PLAN.md)）；#142 ja/ko 阅读速度上限 13 + QA 阅读单位口径改细（标点不计、CJK 下 Latin 半字，只影响 `reading_speed` warning）。两处自查教训已写进对应文件注释：新守卫要照项目既有正确做法写（`test_events.py` 按标题切块）；「不该红时不红」也要有测试。

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
| 自动化测试 | 1154 个测试、335 个子测试，全部通过（2026-10-24） |
| 静态检查 | Ruff lint 和 formatter 通过 |
| 打包 | wheel 和 source distribution 已验证 |
| CI | GitHub Actions（ubuntu + macos × Python 3.11/3.13 矩阵）跑 ruff、pytest、CLI 冒烟与 `uv build` |
| Git | 功能修改使用 `feat/` 开发分支（早期为 `codex/`），Review 通过后以 PR 合并 `main` |
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

- 译文超硬显示阈值时，能按禁则切成最多 2 行的已由确定性换行自动放行（记 `target_wrapped` warning）；切不开或切完仍超的仍由 QA 阻断——`_KATAKANA_RUN` 落地后「片假名连续段整体放不下」也是切不开的一种（硬形态，刻意不退回按字切；真撞上频繁 review 时的退路写在 `segment.py` 该常量的注释里）。libass 像素驱动的二分换行与重译缩短未实现（方案见 [docs/TARGET_LINE_WRAP_PLAN.md](docs/TARGET_LINE_WRAP_PLAN.md)）。
- 新 Job 默认生成单遍语义组译文，也可显式选择 `--translation-quality polished`；Agent 主链真实 E2E 已通过，但默认切换仍需多样本质量与成本对照。
- `rebuild-source` 只适用于有媒体的 Job；纯字幕 Job 的源字幕问题仍需人工修复。
- 高级命令 `retranslate-cues` 已与主翻译共用传输/结构重试和 retry 事件语义，但仍只在全部选定 cue 翻译完成后写最终响应；中途失败会重复请求该次命令中已成功的批次，因此不会提前发送意味着已持久化的 `translation_batch_completed`。端到端主翻译的逐批 checkpoint 不受影响。
- 回填失败后的等待保持已修复（契约见 [架构说明](docs/ARCHITECTURE.md) 的「Job 与 Artifact」：用户模式的 `apply-cue-retranslation` / `apply-translation` / `import-translation` 校验失败不改写 stage，`cue_retranslation_waiting` 另外接受 `failed` 以兜住 API 模式重译失败与历史 Job）。仍存在的限制：stage 语义不区分「重译失败」与「等待被放弃」，放弃一次已请求的重译没有专门命令，靠重跑 `retranslate-cues` 重建 manifest，或直接开一轮整轮翻译取代它（`prepare_agent_translation` 会移走重译 manifest，且在途整轮请求优先）。用户模式的回填错误也不在 Job 上留痕（只有命令行报错，stage 不记 `error`），排查要看命令输出而不是 `job.json`。等待期间 `TRANSLATE` stage 停在 `awaiting_input` / `failed` 是表象，不影响判定。
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

## 下一步待办（按建议顺序，每条可独立成 PR）

接手约定：先出方案等批准再编码（AGENTS 修改流程第 1 条）；每条一个 `feat/` 分支、串行开 PR；行为变化补测试且至少一条「不该红时不红」；改完同步 README / ARCHITECTURE / RECOVERY / CHANGELOG / 本文件，并全仓 `rg` 核旧口径残留。本文件受 `tests/test_handoff_budget.py` 守卫，新条目 ≤ 2KB、总量 ≤ 200 行 / 32KB。

1. **B2 剩余层：`doctor` 余量检查与另两条下载路径**（[BaoCut 增量复查](docs/reviews/baocut-rescan-2026-10-23.md) 的 B2；第一层「下载前预检」已于 2026-10-24 落地，只覆盖 Cookie YouTube 比较路径）。① **`doctor` 的 `output_free_space` 要先拍板 `warn` 档**：`doctor.py:166` 的聚合是 `all(check["status"] == "pass")`，没有第三档状态，所以复查里写的「低于阈值给 warn 不给 fail」在现有结构里做不到——引入第三档要连带改 README 的 doctor 输出契约与现有 8 个检查项的测试期望，影响面比这个检查本身大。② 另两条下载路径（无 Cookie / 非 YouTube、`audio_only`）没有探测数据：要覆盖得在 `download_remote_media` 入口补一次 `--dump-single-json`（多一次网络往返），或给 `inspect_remote` 加 `-f` 选择器并扩 `RemoteMediaInfo`——后者经 `pipeline/common.py:47` 的 `asdict()` 进 `job.json`，属持久化契约。③ 烧录前估算收益最弱：成品与源同量级而源已在盘上，真正会爆的是下载那一步。
2. **三行布局的行距**（观感）。`render.py` 样式块两种 Style 共用 `marginv=16`，译文第二行与源文偏紧（真实抽帧见 `docs/TARGET_LINE_WRAP_PLAN.md` 验证节）。只调样式参数，不改切分；改后必须重跑 `.github/scripts/verify_layout_measurement.py` 与一次真实 `xvt preview` 抽帧肉眼确认，并注意 `layout["style_options"]` 进了复用绑定——样式一变旧报告自然不复用，属预期。
3. **A2：翻译侧预算按阅读单位计并在 prompt 解释口径**（ROADMAP 近期已立项，**协议变更**）。`translate.py:458/540` 的 `max_target_characters` 改用 `textwidth.reading_units` 口径并去掉或重审 `character_limit=18` 封顶；`translation_provider.py` 翻译 prompt v5→v6、polish v3→v4 向模型说明「标点不计、CJK 下 Latin 半字」并加 BaoCut 的双向表述（`Length is part of correctness` + 预算不压过语义）；按 AGENTS 第 8 条重生成协议快照 golden；CHANGELOG 写明在途 API checkpoint 重置。**前置条件**：先拿到「模型因预算过紧砍掉术语或因果关系」的真实样本，没有样本不动。
4. **先测量再决定的两条**（不要直接实现）：① 翻译响应「id 对但内容漂到邻句」的实际漂移率——BaoCut 用 `|源首词..源末词|` 引用标记检测，XVT 靠 id 对账抓不到；写一个只读统计脚本跑历史 Job（`output/jobs/*/artifacts/translation-*.json`），漂移率有数据再决定是否改载体。② `duration_ms` 对译文质量有无贡献——BaoCut 实测去掉时间信息省 14% 输入；A/B 实测后再决定是否从 `TranslationItem` 去掉。
5. **Q7 翻译上下文与术语候选包**：ROADMAP 近期第一顺位新功能，前置已随 R3.2 取消而解除，新模型步骤直接接 `run_provider_batches` + fingerprint checkpoint。范围边界见 ROADMAP「后续按以下顺序推进」第 4 条。

## 接手步骤

> 待办首选入口是上一节；查「某条历史问题为什么没做」看 [2026-10-17 review 发现清单](docs/REVIEW_FINDINGS_2026_10_17.md)。

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
