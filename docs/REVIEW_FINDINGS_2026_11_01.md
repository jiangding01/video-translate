# 2026-11-01 真实端到端验收发现清单

本文件是一次**真实素材**端到端验收的结论存档，供后续逐条处理。**它是待办清单，不是契约**；每条处理完就在状态栏标记，全部清空后本文件可以删除。

## 来源与可信度

基线 `main @ 0542f93`。素材是公开视频 `https://www.youtube.com/watch?v=-OH1y7CjIew`（Claude 官方频道「Coding on the go with Claude Code」，242 秒、en-US、带 `en` 人工字幕、最高 2160p）。命令：

```bash
xvt run "<URL>" --output <DIR> --target-language zh-CN --translation-quality polished --burn
# 停在 awaiting_input（语义校对报 source_suspect）→ 人工修订源文 →
xvt apply-source-review-fixes <JOB> <REVIEW>
xvt continue <JOB>
```

全程走通：下载 → ASR → 翻译 → 润色 → 语义校对 → 人工修订 → 重新翻译 → 润色 → 语义校对 → 渲染 → 烧录 → 媒体 QA。两段分别耗时 07:18 与 08:35，终态 `recovery: complete`，所有 stage `succeeded`，成品 3840×2160 / 463 MB / 242 s，烧录 proof 为 checksum 保护的 `version=2` 且绑定 `source_media_checksum` 与 `burned_video_checksum`。

可信度分三档，逐条标注：

- **✅ 已核验**：本次验收实际触发，并读代码确认了完整链条。
- **○ 已观察**：验收输出里实际出现，未追到代码根因。
- **△ 推断**：读代码得出，本次未实跑触发。

状态：`待修` / `已修（PR #NN）` / `待决策`。

## 应修

### E1 平台人工字幕被静默跳过，且 reason 与同 Job 的事实矛盾 — ✅ 已核验，**已修（本轮）**

同一个 Job 里两个 artifact 对同一事实相反陈述：`remote_info.subtitle_languages == ["en"]`（XVT 自己探测并落盘的），而 `source_selection.reason == "platform_subtitles_missing"`、`candidate_kind == "none"`、`selected_kind == "asr"`。真实原因是平台字幕为 opt-in，未传 `--youtube-subtitles` 时 `config.youtube_subtitles == False`，根本没去取。

根因在 `source_selection.py:147-150`：`candidate_kind is SourceCandidateKind.NONE` 一律给 `platform_subtitles_missing`。该模块是纯判定叶子，**收不到 `youtube_subtitles`**，所以无法区分「平台真没有字幕」与「我们没去请求」——信息在上游（`pipeline/transcription/dispatch.py:69` 的 `if youtube_subtitles:`）就丢了。

本次实际后果是一条完整因果链，全部发生：用户被告知「平台字幕缺失」→ 不会想到本可以用那份人工字幕 → 白跑 4 分钟 ASR → ASR 三处错听（见 E1 附录）→ 语义校对拦下 → 停在人工修订。**这条人工环节本可完全避免。**

ASR 错听实录（全部由语义校对正确检出）：cue 21 `reveal the result` 应为 `review the result`；cue 33 `Cloud GitHub app` 应为 `Claude GitHub app`；cue 53 `Cloud Code` 应为 `Claude Code`。一个讲 Claude Code 的官方视频，产品名被错写三次。

**落点**：让选择判定能区分两种情况。最小改法是给 `SourceCandidateKind` 增加一个「未请求」取值，或由调用方把 `youtube_subtitles` 传进 `plan_source_selection`，并新增 reason（如 `platform_subtitles_not_requested`）。文案要同时改，`reason` 是写进 artifact 的，属协议面。

### E2 `layout` 从不记录实际使用的字体，跨机复用判定会混同 — ✅ 已核验，**已修（本轮）**

`qa.py:267` 写进报告的是 `render_options.target_font`，即**用户显式指定值**；未指定就是 `null`。而真实字体在 `render.py:163` 才由 `options.target_font or font_for_language(target_language)` 推导，**推导结果从未进 `layout`**。本次验收的 `qa-translated.json` 与 `translation-candidate-qa.json` 里 `target_font` / `source_font` / `target_size` / `source_size` 全是 `null`，只有 `video_width/height` 和 `margin_vertical` 有值。

后果：同一 Job 在 macOS（`Hiragino Sans GB`）产出布局 QA，拷到 Linux（`Noto Sans CJK SC`）继续时，两边 `layout` 块逐字节相同，`burnin._reusable_rendered_layout` 据此判定「样式一致、可复用」，而实际字体与行宽都不同。AGENTS 约束 4 要求「不跨画幅或字体复用」——画幅记了，字体没有。

**已修**：`render.resolved_style_binding` 产出实际生效的字体与字号，`_styled_subtitles` 与 `qa.check_subtitles` 共用这一份（两层同源钉子），`layout["resolved_style"]` 落盘并进 `burnin` 的复用绑定。**方案阶段有一处判断被独立 review 证伪**：原以为「往 layout 加字段必然改 artifact checksum、必须重新生成协议快照 golden」，实际快照场景全是纯字幕 Job、没有画幅，`check_subtitles` 根本不构建 layout 块，golden 零 diff；已把这个覆盖面钉成显式守卫。另补两处原方案漏掉的：`pipeline/review.py` 的确认绑定也整块比较 layout（在途已确认的 `needs_review` 会失效一次，已写进 CHANGELOG 与恢复契约），以及 `_styled_subtitles` 调 `font_for_language` 时不传 `platform_name`，所以 helper 穿了一个可选参数供跨平台用例使用。原落点：这是 BaoCut 调研 B3（绑定字体**文件**身份）的更基础一层，应排在 B3 之前。改动会使旧报告的 `layout` 块结构变化，按既有口径同步升版并写明在途 Job 的布局 QA 不复用。

### E3 Q7a 多词专名被拆成碎片进 glossary 骨架 — ✅ 已核验，**已修（本轮）**

本次候选包里 `Remote Control` 是完整功能名，但 `Remote` 与 `Control` 各自也满足规则 D（只出现大写形态、至少一次非句首），于是三条都成为候选；而 `glossary_skeleton_terms` 排除 `capitalized_run`、只导单词候选，结果**骨架里有两个孤立碎片却没有整体**。带恒等映射传回 `--glossary`，就是要求译文里出现孤立的 `Remote` 和 `Control`。

回看 2026-10-31 的 mazda 样本，`Apple CarPlay` / `Android Auto` 是同一结构——只是 `Apple`、`CarPlay`、`Android`、`Auto` 四个组成部分恰好各自也是合理专名，把问题掩盖了。所以这不是本素材特有。

**已修**：原落点写的「被覆盖就不进骨架」在设计 review 阶段被真实样本否掉——同一份语料里 `Claude` 同时被 `Claude Code` 与 `Claude GitHub` 覆盖，却出现 18 次、命中平台标题，排除它等于把最核心的专名丢出骨架；mazda 样本的 `Apple` / `CarPlay` / `Android` / `Auto` 同理，且它们只在多词串中出现过，连「是否独立出现」都区分不了。形态上 `Remote`（该删）与 `Claude`（必留）没有可确定性检测的差别，差别在语义，而语义是 Q7a 不做的事。最终改为**标注而非排除**：被覆盖的条目照进骨架并带 `_covered_by`（覆盖它的短语，字母序），CLI 当场列出几条，判断交给用户。覆盖关系用全部多词候选、不按 confidence 过滤（`Claude Code` 是 medium，过滤会漏掉用户最关心的那条）。多词本身仍不进骨架，既有理由不变。

### E4 `xvt run --help` 全部参数零帮助文案 — ✅ 已核验，**已修（本轮）**

`--youtube-subtitles`、`--source-strategy`、`--allow-auto-subtitles`、`--align-subtitles`、`--subtitle-language` 等在 `--help` 里只有裸参数名，没有一个字的说明。而 AGENTS 把 `xvt --help` 与 README 并列为「用户可见行为」的事实来源。README 写得很详细，`--help` 等于没写。

叠加 E1 后果加重：用户看 `--help` 无从知道平台字幕需要显式开启，也不知道 `workflow.py:171` 要求 `--youtube-subtitles` 必须配 `--subtitle-language`（只有报错时才知道）。

**落点**：给 `build_parser()` 的参数补 `help=`。工作量中等但机械，可一次做完；补完顺带让 `--source-strategy` 的说明点出它与 `--youtube-subtitles` 的关系。

**已修**：`cli._ARG_HELP` 作为 55 条文案的唯一事实来源，复用到 180 个位置；三条守卫测试（每个参数都有非空 help、同名参数 help 一致、表里没有够不着的键）把它钉住。实测规模：193 个参数里 180 个缺 help（93%），31 个子命令全部中招，但去重后只有 55 个唯一参数名。上线守卫时**顺带查出两个此前不知道的既有分歧**——`--check` 与 `--fail-on-waiting` 在 `apply` 那组变体上多了约束（「不能与 --continue 同用」「仅在带 --continue 时生效」），一条文案覆盖不了，已连同 `--cue`（三处可重复 vs preview 一处单值）登记进 `HELP_DIVERGENCE_ALLOWED` 并写明理由。

### E5 终态把最有价值的信息藏进 JSON — ✅ 已核验，`待修`

语义校对的三条 issue message 质量很高（定位到 cue、给出上下文推理、把译文「查看结果」当作反推源文的证据），但 CLI 终态只打印「语义校对发现源文可能有误」加若干长路径。用户必须自己打开 `translation-semantic-qa.json` 才知道是哪几句、错在哪。

**落点**：`awaiting_input` 终态按 issue 摘要若干行（cue 区间 + 截断后的 message），人类可读模式打印，`--events-jsonl` 模式不变。

## 可优化

### E6 CLI 中英文混用，两处 — ✅ 已核验，`待决策`

一是外层中文、内层英文：`xvt apply --check` 对源文修订模板报 `--check only supports human translation review JSON/SRT`，而同屏的「任务失败」「恢复建议」「待编辑」都是中文。二是模型产出的语言不定：本次三条语义校对 issue 里两条英文、一条中文，同一份报告内不一致。

后者涉及 prompt 是否要求固定语言，属协议面，需与 A2 一并考虑；前者是纯文案。

### E7 高成本的源文修订没有只读预检 — ✅ 已核验，`待决策`

`apply --check` 明确只支持人工**译文**修订。而源文修订才是不可逆的那个：失效译文、润色与全部下游，必须重新付费翻译。风险更高的操作反而不能预演。AGENTS 约束 4 只对人工译文 `apply --check` 作了要求，所以这是设计范围问题而非违约。

### E8 源文修订的输出没有影响面摘要 — ○ 已观察，`待修`

`apply-source-review-fixes` 成功后只打印 5 个 artifact 路径加一条 `next`，没有「修订了 N 条源 cue、失效了 M 个下游产物」。这是高成本操作，影响面应当可见。

### E9 下载进度不可信 — ○ 已观察，`待决策`

速度在 1.8 MB/s 与 15 KB/s 之间跳变，出现过「剩余 18:52」而实际数秒后完成；分离格式的视频流与音频流都标注为「下载源视频」，进度走到 100% 后从 1% 重新开始，形似失败重试。ETA 来自 yt-dlp 瞬时速率，XVT 侧可考虑不展示 ETA 或区分两个流。

### E10 烧录进度在 99% 后回退到 0% — ○ 已观察，`待决策`

输出序列为 `99% → 0% N/A → 100%`。推测是 ffmpeg 进度解析在 flush/收尾阶段拿到空值后按 0 处理（未读代码确认）。

### E11 MLX 模型下载的 huggingface 进度条泄漏进输出 — ○ 已观察，`待决策`

`Fetching 4 files: 100%|██████████| 4/4 [00:00<00:00, 7410.43it/s]` 混在 `[进行中] xxx` 的输出里，破坏格式一致性。**需要先确认它走 stdout 还是 stderr**：若是 stdout，则违反「`--events-jsonl` 模式 stdout 只输出逐行 JSON 事件」的既有契约；若是 stderr，仅为观感问题。本次用 `2>&1` 合并了输出，无法区分。

### E12 无法限制下载与烧录画质 — △ 推断，`待决策`

没有 `--max-height` 一类参数，4 分钟视频按 2160p 下载并烧录，成品 463 MB。HANDOFF 记录「4K/8K 不设上限」是有意设计，但「只要 1080p 成品」是常见需求。若接受，落点在下载格式选择与烧录前的缩放，两处都要进烧录 fingerprint。

### E13 `--source-strategy subtitles` 不隐含请求平台字幕 — △ 推断，**已随 E1 修复**

`dispatch.py:69` 只认 `youtube_subtitles`，所以 `xvt run URL --source-strategy subtitles` 这一意图明确的命令会在**下载完整视频之后**失败，并报 `platform_subtitles_missing`（`source_selection.py:131-134` 的 FAIL 分支，与 E1 同一个 reason）。本次未实跑触发（不愿再下一次 4K）。若 E1 修复时一并处理，这条自然消解。

### E14 HANDOFF 预算守卫把文件末尾的块引用算进最后一条日期条目 — ✅ 已核验，**已修（本轮）**

归档本次条目时实测撞到：`tests/test_handoff_budget.py` 的 `_entries` 用 `BOUNDARY_RE` 切条目，边界只认「下一条日期条目」与「markdown 标题」，而 HANDOFF 日期条目列表后面固定跟着一段 `>` 引用（档案指引 + 体量约定，约 0.65KB）。它是文件级说明、不属于任何条目，却被算进最后一条，于是**任何 ≥1.4KB 的条目一旦滚成最后一条就会误报**——而该文件的条目平均 1.3KB 以上，这是结构性的，不是滚少了。

该函数的 docstring 本来就写着「条目不会越过章节标题把无关内容吃进来」，2026-10-21 已为同类误报修过一次（当时是吃到了 `## 接手步骤`），这次只是同一类问题的另一种无关内容。

**已修**：`BOUNDARY_RE` 加入 `>`，并补 `EntryBoundaryTest::test_entry_stops_at_block_quote`。撤掉该修复后，新用例与真实 HANDOFF 的单条预算检查同时变红，还原后全绿（实测）。

## 表现良好，不需改动

- **语义校对（Q6）是本次最大亮点。** 三条 `source_suspect` 全部命中真实 ASR 错听，且推理链完整（从上下文判断 `reveal` 不合理、从后文多次出现的 `Claude app`/`Claude Code` 推断 `Cloud` 是误听、把译文当作反推源文的证据）。没有它，错误的产品名会直接进成品。
- **源文修订的失效与恢复链路正确。** 修订后下游全部失效、重新翻译与润色、语义校对复查干净、一路直通烧录，`source_suspect` 不再出现。
- **4K 画幅下行宽 QA 真实测量成功**（`layout.measurement == "measured"`），未降级到保守规则。
- **烧录 proof 与媒体 QA 正确**：`version=2`，绑定源媒体与成品 checksum。
- **`awaiting_input` 的退出码语义正确**（默认 0），恢复建议给出了三条可选路径并说明了各自代价。
