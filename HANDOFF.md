# 项目交接说明

更新时间：2026-11-01（HEAD `0542f93`，`main`，工作树干净；写于 Claude Opus 5 会话）

2026-11-01 **布局 QA 绑定实际生效的字体与字号（E2）；方案阶段一处前提被独立 review 证伪。** `layout` 此前只记用户显式覆盖，真实字体由 `font_for_language` 按平台推导却从未落盘，于是 macOS 产出的报告拷到 Linux 后 `style_options` 逐字节相同、字体却是 Hiragino 对 Noto，复用判定把两种环境混同（约束 4 的画幅记了、字体没记）。新增 `render.resolved_style_binding` 作为唯一推导，**qa 报告与 ASS 渲染共用它**（各推一遍会让报告记的不是测量时真用的样式，比不记更糟；对象身份与行为两层钉子，两个方向的漂移各被抓住）。**原方案说「加字段必然改 checksum、必须重生成协议快照 golden」是错的**：快照场景全是纯字幕 Job、无画幅，`check_subtitles` 不构建 layout 块，golden 零 diff，已把该覆盖面钉成显式守卫。另补两处原方案漏的：`review.py` 的确认绑定也整块比较 layout（在途已确认的 `needs_review` 会失效一次，属预期，同 wrap v2）；`_styled_subtitles` 不传 `platform_name`，helper 因此穿了可选参数供跨平台用例。残留缺口：两边显式指定同名字体时仍照常复用，字体**文件**身份属下一层，本次不做。

2026-11-01 **真实素材端到端验收跑通，产出 13 条发现清单。** 用公开视频（Claude 官方频道，242 秒、en-US、带 en 人工字幕、2160p）走完 `run` → 语义校对停在 `awaiting_input` → 人工修订源文 → `continue` → 烧录，两段 07:18 + 08:35，终态 `complete`，成品 3840×2160 / 463 MB，烧录 proof `version=2`。逐条结论见 [2026-11-01 验收发现清单](docs/REVIEW_FINDINGS_2026_11_01.md)。**最严重的一条是一条完整因果链**：平台人工字幕因 opt-in 未被请求，而 `source_selection` 把它记成 `platform_subtitles_missing`（与同 Job 的 `remote_info.subtitle_languages == ["en"]` 直接矛盾）→ 白跑 ASR → ASR 把产品名错听三次（两处 `Cloud`→`Claude`）→ 语义校对拦下 → 停在人工环节，而这一整段本可避免。**Q6 语义校对是本次最大亮点**：三条 `source_suspect` 全部命中真实错听，推理链完整（含拿译文反推源文），没有它错误产品名会直接进成品。

2026-10-31 **`xvt build-translation-context`：确定性术语候选包与可审核的 glossary 骨架（Q7a）。** Q7 拆三阶段，协议变更隔离在最后一阶段：Q7a 零模型调用、不进翻译 prompt 与指纹、不改 stage、不接 `plan_advance_steps`（协议快照零 diff），Q7b 接模型推断腿，Q7c 才把上下文接进 prompt。**主判据是实测挑的，不是设计出来的**：规则 D「只出现大写形态且至少一次非句首」在一份真实英文人工字幕（52 cue）上精度与召回各 6/6；两条更直觉的替代各漏 4/6——「出现 ≥2 次才收」漏掉 `Apple` / `CarPlay` / `Android` / `Auto`（各只出现一次，恰是最该统一译名的产品名），「只收形态可辨的」同样漏 4。句边界靠句末标点在**跨 cue 拼成的文本流**上判（cue 常切在句中），`>>` 与 `[...]` 在 token 化前剥除。缺标点或缺大小写双形态时记 `extractor_degraded` 并放弃专名候选、退出码仍 0，所以**只对拉丁字母源文有效**（CJK / 德语 / `auto` 全走降级；CJK 要分词器，归 Q7b）。骨架只导高置信单词、按 casefold 去重、多词短语永不进——`load_translation_policy` 判重复时 casefold 并直接抛，而短语恒等映射会踩 error 级 `glossary_violation`（无 `--acknowledge` 出口）。自查两处：早一版只看连字符，把 `low-slung` / `two-seater` 收成术语并导进骨架（恒等映射＝要求译文出现英文原词），现要求 token 含大写；判定顺序原先让形态规则先跑，`MX-5` 被错标成 `code_identifier`，改成专名优先。五个坏实现各注入一次，对应用例都变红。它在 `downstream_of(SOURCE_CUES)` 里，重分段 / 源文修订 / `rebuild-source` 会一并失效它。骨架导出接了 `paths` 的覆盖保护，盖不掉已登记 artifact。

2026-10-24 **无 Cookie / 非 YouTube 的完整视频下载也做容量预检；B2 剩余层就此收口。** 上一轮的预检只挂在 Cookie YouTube 画质比较路径（靠那条路现成的 `_probe_video` 数据），而无 Cookie / 非 YouTube 的完整视频走直接下载路径、同样 GB 级却零探测。现在新增 `_probe_download_size` 自己探一次，**刻意不复用 `_probe_video`**（后者为画质比较做严格字段校验，非 YouTube 站点过不了；容量预检只要一个大小，不该因元数据形状不同就把下载弄挂），捕获一切失败降级成「未知」跳过预检；探测 flag 照**下载命令**构造（`_probe_video` 在无 Cookie 时总加 `--ignore-config`，直接路径不是），否则量的不是要下的，有用例钉住。`_check_download_space` 入参从 `VideoSelection` 解耦成 `_DownloadSize`，判定一字未改，不新增事件。**剩下两项评估后都不做**：`audio_only` 是 `bestaudio/best`、几十 MB 量级，不值一次网络往返；烧录前估算**只在硬件编码路径有可信依据**（`-b:v` 是我们给的目标码率，可按 码率 × 时长 算），软件编码走 `-crf 18`、码率随内容变，而「用源文件大小当门槛」不成立——源既非上界也非下界（ProRes 源重编码后小得多 → 误拦；已压缩源可能变大 → 漏报），且非 macOS 一律走 CRF、正好没有保护。自查：`_probe_download_size` 的 `except` 初稿把 `TypeError` 也捕了，结果**吞掉了测试桩不接 `timeout` 这个调用约定错误**——6 条受影响的既有用例当时全是绿的，预检被静默跳过而我以为没有影响面。收窄 `except` 后它们如实变红，逐条适配。

2026-10-24 **CI 固定 uv 版本并收紧 permissions；「传 GITHUB_TOKEN」被查证为无效改动。** #148 的 macos/3.11 挂在 `Install uv`（`Connect Timeout`），日志里那句「No (valid) GitHub token provided」把它说成了认证问题——读 `setup-uv` 源码后确认那是 `getLatestVersion` 的 **catch-all** 分支文案（任何错误都打它），而 `github-token` 的默认值本来就是 `${{ github.token }}`、与 `secrets.GITHUB_TOKEN` 同值，显式再传零效果。真正的失败点是「不固定版本就要先查一次 latest release」那次 API 调用，所以改成 `version: "0.12.1"`（本机实测版本，让本地验过的就是 CI 跑的；已核该 tag 真实存在且含 arm64 macOS 与 x86_64 Linux 资产，pin 一个不存在的版本会让四个矩阵项全挂）。顺带显式声明 `permissions: contents: read`（与本次故障无关的独立收紧，缓存读写走 `ACTIONS_RUNTIME_TOKEN`、不受它约束）。**两条都不保证 CI 不再抖**，只是去掉一个本不必要的网络依赖。教训：别照着错误日志的字面归因动手——那句话是 catch-all 打的，我第一次就被它带偏、提过一个无效方案。

2026-10-24 **`xvt doctor` 有了 `warn` 档，顺带修掉「非 macOS 上整体永远 fail」（B2 剩余层①）。** 8 个检查项此前只有 `pass` / `fail` 两档、聚合是 `all(status == "pass")`，而 `h264_videotoolbox` 是 Apple 独有的——非 macOS 上 `hardware_encoder` 必然缺失、整体永远 `fail`、退出码永远 3，整份报告没有信号价值；而 `burn.py` 本来就回退软件编码，缺它是慢不是跑不了。现在三档聚合（**fail 优先于 warn**）、`hardware_encoder` 缺失记 `warn`、退出码只在 `fail` 时为 3。`missing_status` 只覆盖「ffmpeg 正常返回但没有这个 token」，ffmpeg 缺失与探测超时是**判不出来**，照旧 fail。`--output` 新增 `output_free_space`（低于 2 GiB 记 warn、**永不 fail**，`OSError` 时这一项整个不出现）：阈值是经验值，没有资格拦东西，真闸门是下载前那道按实际大小的预检；可写性与余量共用一份路径解析。其余 6 项与 `asr` 的 fail 语义一字未动。三个坏实现各自注入过（聚合反序、OSError 记 warn、hardware 退回 fail），对应用例都变红。本机实跑 `xvt doctor --output` 为 10 项全 PASS、`output_free_space: 245.6 GiB`（与 `df` 的 246Gi 一致）、退出码 0。

2026-10-24 **完整视频下载前先做磁盘容量预检（B2 第一层）。** `download.py` 整条下载前此前没有任何容量估算，而 `REVIEW_FINDINGS_2026_10_17.md:386` 记过真实的「磁盘满导致 stage 永久停在 running」——失败发生在最贵的那一步且状态是坏的。现在 `_probe_video` 把所选格式的登记大小记进 `VideoSelection`，`_download_compared_video` 在选定一侧后、建临时目录前调 `_check_download_space`：不足则发新事件 `download_space_insufficient` 并以 `DependencyError`（退出码 3）失败，一个字节都不下、不留临时目录。门槛按**合并峰值**算（分离格式两份、单文件一份）；大小求和**按 `requested_formats` 走而不是 video + audio 相加**——单文件格式下那两者是同一份 dict，相加会翻倍并误拦空间充足的下载（有用例钉住）。只做证伪：拿不到大小就跳过，不为 `filesize_approx` 的估算误差再加余量（低估只是退化成现状，不产生新失败模式）。判定排在格式提取完整性护栏**之后**，`test_fail_closed_boundaries` 的 11 条边界一字未动。新事件进了 `tests/test_events.py` 的 golden（下载类事件此前没有一个在里面），文档字段表与实际载荷现在双向校验——改错文档字段名两个守卫都会红，已实测。覆盖面只到 Cookie YouTube 比较路径，剩余层见「下一步待办」第 1 条。

2026-10-24 **日语片假名词不再被展示换行从中间切开，`wrap.version` 升 2。** `_CJK_WRAP_TOKEN` 此前只让 Latin / 数字段整体不拆，片假名走 `\S` 单字一 token，`グラフィックス` 被切成 `グラフィッ|クス`；禁则表挡不住（小字假名与长音符在 `NO_LINE_START` 里，普通片假名之间照样放行）。新增 `_KATAKANA_RUN` 把片假名连续段与 Latin 段同口径整体算一个 token——**不是新启发式，是把既有规则补全到同一类对象**，`グラフィ|ックス` 与 `Open|AI` 坏在同一处。刻意排除 `゠` / `・` 与半角 `･` 三个词间分隔符（否则 `アイス・コーヒー` 切不开）；半角片假名纳入（`normalize_display_text` 不做半角转全角）；平假名与汉字不收（词法边界，要分词器，P7 ②）。**硬形态**：段放不下即 `None` 维持 review，与 Latin 单 token 同口径；退化可达但 20 条构造样本 0 命中、修好 5 条，仓库零份 ja 译文 artifact 所以那只是机制差异不是误伤率。`version` 1→2 的理由**不是**约束 4 被违反（`_reusable_rendered_layout` 复用的报告与 ASS 同源一致），而是不升则修复对在途 Job 不生效。升级影响：带 `layout` 块的报告重跑后 checksum 变，人工确认需重做。顺带补了 P7 ⑤ 那张表的交叉引用（该行在源文分段侧仍按 0/12 不做）与 `ARCHITECTURE.md` 漏改的「产出点三→四」。

2026-10-23 **YouTube 画质平局且仅 Cookie 侧提取不完整时改走干净的匿名侧（#145）。** `download._download_compared_video` 本来就有「Cookie 侧受损就回退匿名侧」的机制，但换路条件写的是**严格**更高画质，于是两侧画质相同时留在脏的 Cookie 侧、随后被「格式提取不完整」护栏整单拦下。YouTube 的 SABR 实验按账号开启后 Cookie 侧**每次**都报 `web_embedded ... formats have been skipped`、而匿名侧零警告且选中同一个 1080p，于是这类账号下**任何** YouTube 视频都卡死在这条护栏上（2026-10-23 真实端到端验收撞到，当时只能靠 yt-dlp 垫片绕过）。现在平局且仅 Cookie 侧脏时也换路，`reason` 记新取值 `anonymous_clean_extraction`。**护栏本身一个字未改**：`incomplete` 判定、标记清单与错误文案全部保留，脏的一侧永远不会被下载；匿名侧画质更低（那才是 `5ae28e8` 要防的静默降级）、匿名侧也脏、缺原音轨、音轨语言不一致四类仍然失败。真实验收（不带垫片、真实 Cookie 打真实 YouTube）：修复前报「视频格式提取不完整」，修复后以 `access=anonymous`、`reason=anonymous_clean_extraction` 拿到 1080p 原音轨，format_id 与 Cookie 侧选中的完全相同。方案见 [YouTube 下载画质方案](docs/YOUTUBE_DOWNLOAD_QUALITY_PLAN.md)。

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
| 自动化测试 | 1195 个测试、343 个子测试，全部通过（2026-10-31） |
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
- `build-translation-context` 的确定性判据只对「句中普通名词小写」的拉丁语言成立：CJK 没有大小写形态、德语所有名词大写、源语言为 `auto` 时一律降级，只剩形态类候选（型号、产品名、缩写、代码标识符）。CJK 专名抽取要词法边界（分词器），与译文切分的词法边界是同一个卡点，归 Q7b 的模型腿。另外 `H.264` 这类「字母+点+数字」型号名抓不到（点后首字符是数字，不符合任何形态规则）。
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

接手约定：先出方案等批准再编码（AGENTS 修改流程第 1 条）；每条一个 `feat/` 分支、串行开 PR；行为变化补测试且至少一条「不该红时不红」；改完同步 README / ARCHITECTURE / RECOVERY / CHANGELOG / 本文件，并全仓 `rg` 核旧口径残留。本文件受 `tests/test_handoff_budget.py` 守卫，新条目 ≤ 2KB、总量 ≤ 200 行 / 32KB。**头部「更新时间」那行一律写 `（HEAD <父提交>，main，工作树干净；写于 … 会话）`，即便改动要走 `feat/` 分支与 PR，也不要写「本条改动在 `feat/xxx` 上待合并」**：那是一个会变的未来状态，分支合并并删除后它当场过时，而没人会回头改——2026-10-23 至 24 这样连留了四条，每条都要事后补一次提交。父提交 hash 描述的是「这份文件所讲的代码基线」，是提交那一刻的事实，不会过时。

1. **三行布局的行距**（观感，**落点已被实测推翻，重写如下；建议排在 E4/E5 之后**）。原落点「两种 Style 共用 `marginv=16`，只调样式参数」是在错误的机制理解上写的：双语不是两个事件，而是 `render.py` 里 `f"{target}\\N{{\\rSource}}{source}"` 的**单事件内换行**，`marginv` 只管整块距底边多远、`Source` 样式的 `marginv` 根本不参与定位。2026-11-01 用验收 Job 实渲四组证实：`marginv` 16→40 只让字幕整体上移、三行相对间距**一模一样**；缩小 `source_size` 反而让那道缝更窄（方向相反）。**真正能动它的只有在 `\N` 处插覆盖标签**（`\N{\fs<gap>}\h\N{\rSource}` 插一个小字号空行）。实渲标定：gap≈0.2×`target_size` 缝≈译文行距、0.3× 分组清晰、0.5× 过头像独立字幕；**用户 2026-11-01 表态不在意分组效果，取 0.2×**（4K 下 target_size=20 即 4），按比例算而非硬编码，但需再渲一组 1080p 确认小画幅成立。**连带代价**：要同步验证测量侧（`measure_bilingual_line_widths` 走另一处事件构造 `render.py:252`，插空行应不改行宽但须实测而非推断），且会让所有布局 QA 报告**再**失效一次——E2 刚失效过一次，建议等它的重测重渲在真实使用中跑过一轮再做。候选帧见会话临时目录（已失效，需要时按上述参数重渲）。
2. **E4 / E5：`xvt run --help` 零帮助文案；终态把 issue message 藏在 JSON 里**（同清单 E4、E5，`待修`）。前者机械但量大，补完顺带点出 `--youtube-subtitles` 与 `--source-strategy` 的关系、以及它必须配 `--subtitle-language`；后者是在 `awaiting_input` 终态按 issue 摘要几行（cue 区间 + 截断 message），`--events-jsonl` 模式不变。其余可优化项（中英混用、源文修订无 `--check`、进度显示、画质限制等）留在清单里按需取用。
3. **A2：翻译侧预算按阅读单位计并在 prompt 解释口径**（ROADMAP 近期已立项，**协议变更**）。`translate.py:458/540` 的 `max_target_characters` 改用 `textwidth.reading_units` 口径并去掉或重审 `character_limit=18` 封顶；`translation_provider.py` 翻译 prompt v5→v6、polish v3→v4 向模型说明「标点不计、CJK 下 Latin 半字」并加 BaoCut 的双向表述（`Length is part of correctness` + 预算不压过语义）；按 AGENTS 第 8 条重生成协议快照 golden；CHANGELOG 写明在途 API checkpoint 重置。**前置条件**：先拿到「模型因预算过紧砍掉术语或因果关系」的真实样本，没有样本不动。
4. **先测量再决定的两条**（不要直接实现）：① 翻译响应「id 对但内容漂到邻句」的实际漂移率——BaoCut 用 `|源首词..源末词|` 引用标记检测，XVT 靠 id 对账抓不到；写一个只读统计脚本跑历史 Job（**按 `job.json` 递归找，不要写死 `output/jobs/*`**：Job 目录是 `<output_root>/jobs/<id>`，`output_root` 由 `--output` 决定、可以在仓库外——2026-11-01 就因为照那个不存在的 glob 查，误判过「本机零历史 Job」）。漂移率有数据再决定是否改载体。② `duration_ms` 对译文质量有无贡献——BaoCut 实测去掉时间信息省 14% 输入；A/B 实测后再决定是否从 `TranslationItem` 去掉。
5. **Q7b：术语候选的模型推断腿（待真实需求触发）**。前置判读已完成（n=2 真实英语素材）：规则 D 召回两份都满（6/6、6/6）、**精度 6/8**，唯一误收落在一句读不通的 ASR 转写上；漏项全是**语义性**的——`Fable 5.1`（版本号不以字母开头，token 正则够不着）与 `medium` / `low` effort 档位名（普通词构成的产品术语，确定性判据必然排除）。**缺口是精度与语义、不是召回**，那正是模型腿的主场。对照的「CJK 分词器」路线已评估并否掉，见 [CJK 分词器评估](docs/reviews/cjk-tokenizer-evaluation-2026-11-01.md)：换行只要词边界、专名要命名实体标签，是两种能力两个依赖——中文侧唯一轻量选项 jieba 实测把 `舒适性` 标成人名且 2020 年起停维护，日文侧可靠标签要背 44–250MB 词典；该文 §6.2/§6.3 另记了换行腿（budoux 只做日文）的触发条件与 `TARGET_WRAP_VERSION` 绑定坑。**本条触发条件**：出现真实 CJK 源 Job 且需要 glossary 候选。届时新等待步骤必须先登记进 `workflow.AWAIT_STEPS`，再在 `cli._STEP_ROUTES` 与 `_UNROUTABLE_AWAIT_STEPS` 之一表态（`AwaitStepsCoverageTest` 用 AST 强制）。Q7c（接进翻译 prompt）照旧排最后，属协议变更。
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
