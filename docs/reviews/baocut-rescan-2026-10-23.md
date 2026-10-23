# BaoCut 增量调研（2026-10-23）

- 调研对象：`/Users/jiangding/baocut`（Rust + Electron 开源剪辑软件），`git log --oneline --since="2026-10-21"` 的 72 个提交，diff 区间 `98f6243b^..HEAD`（HEAD = `7ad81ece`）。
- 基准：XVT `main@5ef3290`，以 [AGENTS.md](../../AGENTS.md) 的约束与模块表、[HANDOFF.md](../../HANDOFF.md) 的现状与待办为判据。
- 方法：Fable 5.1 主会话拆题与交叉核验，三个 Sonnet subagent 分簇读 diff（导出/Jobs/协议簇、字幕渲染/字体簇、Skill/规范/校对流簇），所有「值得借鉴」条目的关键 `文件:行` 由主会话用 `rg` / `sed` 二次核对；BaoCut 的 README、设计文档、提交标题一律不算证据。
- 只读调研：除本文件外未改动 XVT 与 BaoCut 任何文件。

## 0. 先说三件会影响读法的事

1. **72 个提交里只有 35 个是昨天之后新拉的。** 昨天的检出点是 `c7d3bd5d`（reflog `2026-10-22 11:14`），`c7d3bd5d..HEAD` 共 35 个提交；另外 32 个（2026-10-21 到 10-22 上午）昨天就在盘上，只是昨天的调研聚焦 `speech-doc` / `speech-worker` / `model-runtime` / `providers` / `apps/cli`，没有翻到 `export-worker`、`caption-scene`、`subtitle-render` 的新改动（已用 `rg` 核对昨天会话记录：`export-worker` 只出现在目录清单里，`preflight` 命中全部来自工具说明，与 BaoCut 无关）。本文按 72 个全覆盖，但下文「新增内容清单」标出了哪些在昨天就已存在。
2. **转写与翻译核心零改动。** `git diff --stat 98f6243b^..HEAD -- crates/speech-doc crates/speech-worker crates/model-runtime packages/providers crates/video-model apps/cli` 为空。昨天关于翻译协议（`|源首词..源末词|` 引用标记、`duration_ms`、预算口径、DP 切行）的全部结论原样有效，HANDOFF 待办 A2 / 漂移率统计 / `duration_ms` A/B / Q7 不受本轮影响。`model-worker-protocol-spec.md` 的 2 行改动只是 audio.cpp 引擎版本串。
3. **本轮识别出 XVT 一个真实短板，而且可以零依赖修。** 字幕字体缺字时 `qa.check_subtitles` 静默回退保守规则（只记 `layout_measurement_unavailable` warning），而 libass 的 stderr 其实已经把缺的码位打出来了（`fontselect: failed to find any fallback with glyph 0x%X for font: (%s, %d, %d)`，从 ffmpeg 链接的 `libass.9.dylib` 字符串表核实），XVT 在 `render.py:205` 只做了子串匹配就把它丢掉了。详见 B1。

## 1. 新增内容清单（按模块归类）

| 模块 | 提交（节选） | 改动量 | 判定 | 昨天是否已在盘上 |
| --- | --- | --- | --- | --- |
| `crates/subtitle-render`、`crates/caption-scene`、`crates/render-raster`、`crates/compositor`、`crates/frame-render` | `bd4e8a78` GPU material plans / paint effects / font sources / pagination programs；`99e98c7d` 字形遮罩与词级 paint 栈；`7d2349af` wave displacement；`98f6243b` `caption-fixed-line-fit/1`；`98887b00` `caption-page-tag/1`；`88a249cf` 中的 speech-phase | +~12k 行（含测试与 fixture） | 绝大部分是 GPU 绘制材质与风格化逐词字幕，**不相关**；其中字体来源绑定（`font_source.rs`）与行高盒（`font_line_box.rs`）见 §3 | `bd4e8a78`、`98f6243b`、`98887b00` 昨天已在；`99e98c7d`、`7d2349af` 新 |
| `crates/export-worker` | 随 `bd4e8a78` 一起：`render.rs` `frozen_fonts` 改为 sha256 核验 + 资源预算；`preflight.rs` 字体普查装入冻结字体 | +286 | 字体冻结后按内容摘要核验、改了就 `FONT_MISSING` 阻断，**思路可借（B3）** | 昨天已在 |
| `packages/jobs/src/pipelines` | `88a249cf` 转写词级改字后原文字幕跟随（`caption-cues.ts` `rewordSpeechCaption`）；`link-import.ts` / `yt-dlp.ts` 分段下载（合并进来的 `efebc0f5`） | +~460 | **XVT 已覆盖且更严**（源文修订失效下游）；分段下载**不值得**（触碰约束 1） | 昨天已在 |
| `packages/runtime-core` | `9a50c81b` / `a5576e60` 按语言层导出字幕（`export-service.ts`、`text-export.ts`）；`speech-captions.ts` 同事务同步；agent-tools `assemble-*`（合并进来的 `e1a01f1c` / `86aa22e8`） | +~2.4k | 多文档合层导出**不相关**（XVT 单 Job 单目标语言）；`edits_assemble` 是剪辑装配工具，不相关 | 部分新 |
| `packages/models` | `27fcee2a` audio.cpp `0.9.2` → `0.9.2-drama3-base-small` | +~150 | 只换版本串、包名、sha256（`audio-cpp-engine.ts:59`），无引擎参数改动；**不相关**（XVT 用 MLX Whisper / faster-whisper） | 新 |
| `crates/video-engine` | `443977ab` `patchDocument` 只在带键数组条目上把 `id` 当身份 | +~70 | 文档 patch 层修复，**不相关** | 新 |
| `skills/baocut/**` | `67246af8` 下载前查磁盘；`b7e7bafd` 按段音高检查（`voice.mjs`）；`444807f9` 构建期字体缺字告警（`lib.mjs` `fontCmap`、`build.mjs` `checkGlyphs`）；`387264fd` 剪点/静音开头自查；`0b35b832` sound-bite 单独一层；新增 `clip-montage` skill；多条 waveform/backdrop 规则 | +~900 | **B1（字体缺字）与 B2（磁盘预检）来自这里**；其余为制作类规则，不相关 | `444807f9`、`0b35b832`、`82c3c8f6` 昨天已在；其余新 |
| `docs/spec`、`docs/design`、`docs/architecture` | `video-format-spec.md` +244（`audio_envelope`、`speakerLabel`、`production-plan`、`script` 版本链与 `restoredFrom`）；`command-protocol-spec.md` 新增 `pipelines list/start/retry`、`documentIds[]` | 文档 | 规范层，用于定位代码；`production-plan` 的「摘要 + ID + contentHash 增量」与 XVT checksum 恢复同构（**已覆盖**） | 部分新 |
| `packages/ui`、`designs/baocut`、`apps/desktop`、`.github/workflows`、发版提交 | lite-editor 校对视图（`be838391`、`521a9def`、`47acf103`、`6ecdaa14`、`2c6c78d3`…）、markup 工具栏、原型、Electron 44.9.3、3.10.0/3.10.1 发版与 update feed | 53 + 34 + 10 个文件 | **不相关**：UI / Electron / 发版 / 签名。校对 chips 只读是 UI 决策不是协议约束（`LiteLine.translation` 只是展示字段，`packages/ui/src/model/lite-segments.ts:50`） | 新 |

## 2. 值得借鉴项（按建议顺序，每条可独立成 PR）

### B1. 字幕字体缺字：从「静默降级」改成「按字符定位、按 cue 降级」

- **BaoCut 机制**：`skills/baocut/built-in-skills/production-scripts/scripts/lib.mjs:261 fontCmap(file)` 手写解析 sfnt 表目录找 `cmap`，子表优先 (3,10)→(0,4)→(3,1)→(0,3)，只认 format 4 / 12，字形号 0（`.notdef`）不算有；`.ttc`、WOFF/WOFF2、无 cmap 的直接 `{skip: 原因}`（`lib.mjs:265-281`）。`build.mjs:119 checkGlyphs` 用状态机扫场景源码里的字符串字面量，取全部字体 cmap 并集覆盖不到的码位，`build.mjs:126` 输出 `warn <id>: fonts lack 字 U+XXXX… — the bake will fall back to pixels`，**只告警不阻断**（注释写明字符串可能是不上画布的数据）。零第三方依赖。
- **它解决的问题**：在渲染之前给出「具体哪些字符没有字形」，而不是渲染完才发现糊成方块。
- **XVT 现状**：`render.py:205` 对 libass stderr 做子串匹配 `"failed to find any fallback with glyph"` 后 `raise ValueError("subtitle font cannot render every glyph")`；`qa.py:222-228` 捕获后给整份报告打 `measurement = "unavailable"`，**全部 cue** 回退保守显示单位上限，只留一条 `layout_measurement_unavailable` warning，文案是 `rendered width unavailable; conservative display-unit limits applied`——不说哪个字、不说哪条 cue。`doctor.py:74 _check_ass_font` 只拿 `font_for_language("zh-CN")` 真渲染一帧，目标语言是 ja / ko 时没覆盖。CI 的 `.github/scripts/verify_layout_measurement.py` 只保证 zh / ja / en 各一句能量出来，不保证真实译文里的每个字符。
- **XVT 落点（不照搬 cmap，用更便宜的同效机制）**：
  1. `render.measure_bilingual_line_widths`（`render.py:138`）：用 `re.findall(r"fallback with glyph 0x([0-9A-Fa-f]+)", result.stderr)` 收集缺字码位，抛一个带 `missing_codepoints` 的专用异常（或 `ValueError` 子类），不要只抛一句话。libass 这条消息格式自 0.13 起未变，但仍属对 stderr 的契约，匹配不到时回退现有泛化文案。
  2. `qa.check_subtitles`（`qa.py:222`）：warning 文案带上 `U+XXXX` 清单与受影响 cue id；进一步可把降级范围从「整份报告」缩到「含缺字字符的 cue」——其余 cue 仍用测量值。这一步要先决定：缺字 cue 在 ASS 里实际会被 libass 用系统回退字体画出来还是画方块，前者按保守规则放行可接受，后者应该 `review`。建议先只改文案与按 cue 粒度，放行/阻断策略单独拍板。
  3. `doctor._check_ass_font`：目标语言可选参数化（`xvt doctor --target-language ja`），或固定探 zh / ja / ko / en 四种。
  4. 不建议引入 cmap 解析：macOS 的 Hiragino / PingFang 都是 `.ttc`（BaoCut 对 `.ttc` 直接 skip），要么拉 fontTools 依赖，要么空跑；而 libass stderr 已经给出同一份信息，且它量的就是最终烧录用的那条路径（约束 4 「以实际 ASS 字体测量为准」）。
- **为什么现在做**：#141 的换行与 #142 的阅读速度都建立在「测量可用」上；测量一旦静默失效，这两个 PR 的判定全部退回保守口径且用户看不出来。这是本轮唯一识别出的 XVT 真实短板，与约束 4 的 fail-closed 精神不一致。
- **代价与风险**：改动集中在 `render.py` / `qa.py` / `doctor.py` 三处 + 测试；`layout` 块若新增字段会进 `_reusable_rendered_layout` 的绑定比较（`pipeline/burnin.py:216` 比 `style_options`），需确认新字段不进绑定或刻意进绑定并写进 RECOVERY。按 cue 降级会改变已有「整份报告 unavailable」的语义，`tests/test_qa.py` 相关打桩要跟着改。协议快照（`tests/fixtures/provider-protocol-snapshot.json`）不涉及。

### B2. 下载与烧录前的磁盘容量预检

- **BaoCut 机制**：`skills/baocut/SKILL.md:170` 一条 Agent 规则：下载外部视频或生成大临时文件前，先按时长与码率估大小（1080p 约 1–3 GB/小时），再用 `df -h <目录>`（Windows `Get-PSDrive`）看目标盘余量，优先非系统盘，余量不到预估两倍先问用户；交付总结列出可清理的中间文件（被时间线引用的不列）。只是规则，没有代码。
- **它解决的问题**：长视频下载 / 烘焙到一半盘满，错误发生在最贵的那一步。
- **XVT 现状**：`doctor.py:137 _check_output_directory` 只探可写；`download.py` 整条下载前没有容量估算，`--dump-single-json` 的探测结果里 `filesize` / `filesize_approx` 现在没有被用来做任何判断。`REVIEW_FINDINGS_2026_10_17.md:386` 记过一次「磁盘满导致 stage 永久停在 running」的修复，说明这类失败真实发生过。
- **XVT 落点**：
  1. `doctor.py` 新增 `output_free_space` 检查：`shutil.disk_usage(output)`，低于固定阈值（如 2 GiB）给 `warn`，不 `fail`。
  2. `download.py` 的媒体下载入口：从探测 JSON 取所选 format 的 `filesize` 或 `filesize_approx`，与目标目录 `disk_usage().free` 比较，不足时发 `download_space_insufficient` 事件并以明确错误失败（fail closed，失败早于下载）；探测拿不到大小时跳过，不猜。
  3. 烧录前（`pipeline/burnin.py`）可用 ffprobe 的源文件大小做下限估算（烧录成品通常与源同量级），同样只做「明显不够就早失败」。
  4. XVT 的 Skill 文档加一条同义规则（下载前看余量、临时目录放哪）。
- **为什么现在做**：代价极低（标准库 `shutil.disk_usage`），而且 XVT 已有真实踩坑记录。
- **代价与风险**：阈值是经验值，估算只做「证伪」（明显不够才拦），不能当「证实」；跨平台 `disk_usage` 在网络卷上可能返回不准确的值，所以只拦不够、不拦充足。约束 5：事件里只写字节数，不写路径之外的任何敏感信息（路径本身已在事件脱敏边界内）。

### B3.（低优先级）布局 QA 与烧录 proof 绑定实际字体文件身份

- **BaoCut 机制**：`crates/export-worker/src/render.rs:267 frozen_fonts`：导出时把输入已冻结的字体文件逐个读一次，字节数或 sha256 与冻结时不同即以 `FONT_MISSING` 失败（`render.rs:257` 注释：「成片不悄悄换成别的字体」），并设 1024 份 / 256 MiB 的资源预算（`render.rs:268`、`:302`）；`crates/render-raster/src/font-source.rs:98` 对 `caption-font-source/1`（sha256 + faceIndex）找不到即 `FONT_SOURCE_UNREGISTERED_OR_FACE_MISSING` 硬失败，不回退同名字体。
- **它解决的问题**：导出可复现——机器后来升级或替换了字体，成片不会悄悄变样。
- **XVT 现状**：`qa.py` 的 `layout` 块记 `style_options`（字体**家族名**）、画幅与 `wrap` 版本；`pipeline/burnin.py:216 _reusable_rendered_layout` 比对这些后复用渲染腿的布局 QA。字体文件在两次运行之间被系统升级（同名、不同度量）时，旧报告仍被判可复用；v2 proof 也不含字体身份。
- **XVT 落点**：`render.measure_bilingual_line_widths` 测量时顺带用 `fc-match -f '%{file}' <family>` 解析实际文件并算 `checksums.file_sha256`，写进 `layout["fonts"]`；`_reusable_rendered_layout` 把它纳入绑定。只做绑定（不一致就重测重渲），不做阻断。按约束 9 只认内容 checksum，不记 mtime。
- **为什么现在还不急**：真实触发条件是「系统字体升级且度量变化」，频率低；收益是少一次错误复用，成本是引入对 `fc-match` 的依赖（darwin 上 homebrew fontconfig 提供，但不是 ffmpeg-full 必带），还要处理 `.ttc` 多 face 同文件的情况。建议排在 B1 之后观察，B1 落地后若出现「换字体后旧报告复用导致行宽误判」的实例再立项。
- **代价与风险**：新增绑定字段会让旧报告一律不复用一次（属预期，#141 已有同类先例）；`fc-match` 缺失时必须退回「不绑定」而不是失败。

## 3. 不值得借鉴项与理由

| 条目 | BaoCut 位置 | 结论 | 理由 |
| --- | --- | --- | --- |
| 转写词级改字后原文字幕跟随（`rewordSpeechCaption`） | `packages/jobs/src/pipelines/caption-cues.ts:722`；`packages/runtime-core/src/agent-tools/speech-captions.ts:46 syncSpeechCaptions` | **XVT 已覆盖且更严** | 它只在「cue 文本 == 旧词拼接」时覆盖，对不上记 `kept`，译文走另一条同步链，**不失效下游**。XVT 的 `prepare-source-review` / `apply-source-review-fixes` 保留 ID、时间轴、words、provenance 并强制失效译文及全部下游，更符合「译文不能静默过期」。XVT 的 cue 文本也不由 words 派生，没有要同步的对象。唯一可记一笔的是「只覆盖未手改过的文本」这条防覆盖规则，XVT 将来若做词级编辑可参考。 |
| yt-dlp 分段下载 | `packages/jobs/src/pipelines/yt-dlp.ts:89`（`--download-sections *start-end --force-keyframes-at-cuts`）；`link-import.ts:95 MAX_SECTIONS = 20`，逐段 ffprobe 校验，`end` 不超时长 | **不值得** | XVT 没有「只转写 / 烧录某区间」需求；引入区间会让 cue 时间轴相对原视频偏移，触碰约束 1 与 v2 proof 的源媒体身份语义。备忘：`--force-keyframes-at-cuts` 以重编码换精确切点，日后若做区间功能必须带上。 |
| 按语言层、多文档合层导出字幕 | `packages/runtime-core/src/exports/export-service.ts:259`（`documentIds`）、`:1778 chooseSecondaries`；`text-export.ts attachSecondary` 按 cue 中点归句 | **不相关** | XVT 一个 Job 一个目标语言（约束 8），译文与源 cue 按 ID 一一对应，不需要「按中点配对」这类启发式；双语 SRT/ASS 已有。 |
| `caption-fixed-line-fit/1`、`caption-fixed-line-wrap/1`、pagination programs、`caption-page-tag/1`、speech-phase | `crates/subtitle-render/src/scene/word_line_layout.rs:380 fit_scales`；`crates/caption-scene/src/model.rs:215 Pagination`、`:265 FixedLineFit`；`page_window.rs:1`（显示窗口不改词时间） | **不相关** | fixed-line-fit 是把**窄行放大到最宽行等宽**的海报式排版（封顶 4096 px），不是超宽缩字；分页是按词数预设版式的逐词弹出字幕。XVT 是双语翻译字幕，按真实宽度换行（#141）更贴合。换行 / CJK 断行主会话已另行处理，不在此重复。 |
| 字体来源 sha256 绑定 + 行高盒计入回退字体度量 + shaping/hinting 开关 | `crates/caption-scene/src/model.rs:565 FontSource`；`crates/subtitle-render/src/scene/font_source.rs:6`；`font_line_box.rs:5-70`；`font_shaping.rs` | **不相关（机制），哲学已吸收进 B1/B3** | 依赖宿主注册字体字节，XVT 走 fontconfig + libass，行高、shaping 都由 libass 决定不可控。 |
| GPU 材质 / paint / 噪声位移 / 图片填充 / 字形遮罩 / 说话人标签 / `audio_envelope` 声波 | `distance_paint.rs`、`image_paint.rs`、`material_plan.rs`、`text_paint.rs`、`noise_displace.rs`、`paint_style_bindings.rs`、`raster.rs speaker_label_line`、`video-format-spec.md` | **不相关** | 纯视觉效果与音乐视频制作。 |
| audio.cpp 引擎升级 | `packages/models/src/audio-cpp-engine.ts:59` | **不相关** | 只换版本串、包名、大小、sha256；无识别参数改动；XVT 用 Whisper。 |
| `patchDocument` 的 `id` 身份判定修复 | `crates/video-engine/src/document_edits.rs` | **不相关** | XVT 没有文档 patch 层。 |
| `pipelines list/start/retry` 命令协议、`production-plan` contentHash 增量重跑 | `docs/spec/command-protocol-spec.md`、`video-format-spec.md:~1455` | **XVT 已覆盖** | `status` 的 `advance_steps` / `recovery_plan` 与 checksum 恢复同构，且 XVT 按约束 9 只认内容 checksum、元数据只证伪，更严。 |
| `script` 文档版本链与 `restoredFrom` 回退 | `docs/spec/video-format-spec.md:2682` | **不值得（现在）** | XVT 的人工修订是模板 + `revision_fingerprint` 一次性回填，没有「回到某一版」的需求；artifact 失效闭包已能解释产物来历。有多轮人工修订历史需求时再看。 |
| Skill 规则：剪点前后抽帧查黑帧 / 冻帧、静音开头可看懂、按段音高检查（`voice.mjs pitchOf`：40 ms 帧、RMS 门限、60–400 Hz 自相关、中位数、偏差 >5% 重配最多 3 次、`--check` 只读）、sound-bite 单独一层、clip-montage、waveform 规则 | `skills/baocut/SKILL.md`、`production-scripts/scripts/voice.mjs`、`overlay.mjs` | **不相关** | 配音 / 剪辑 / 制作类。「Agent 先自查再交人审」「`--check` 只读模式退出码 1」XVT 已有等价物（QA gate + `awaiting_input`、`apply --check` 只读）。 |
| lite-editor 校对视图、chips 只读、proofread segment、原文 chip、Translate 路由、Electron 44.9.3、3.10.x 发版与 update feed、`designs/` 原型、markup 工具栏 | `packages/ui/**`、`designs/baocut/**`、`apps/desktop/**`、`.github/workflows/*` | **不相关** | 纯 UI / Electron / 发版 / 签名。校对侧栏「译文只读」是 UI 决策（`packages/ui/src/model/lite-segments.ts:50 LiteLine.translation` 展示字段），没有协议约束，也没有向模型工具传新参数。 |

## 4. 与昨天（2026-10-22）结论的差异

- **不变**：总结论仍是「可借鉴的是算法与协议，不是常驻 Runtime 架构」；XVT 在 libass 真实行宽测量、Q6 语义校对、任务级 `recovery_plan` 三处仍强于它；本轮未发现任何翻译协议 / prompt / 预算 / 分段算法的变化，昨天的待办（A2、漂移率统计、`duration_ms` A/B、Q7）顺序与前置条件不变。
- **新增判断**：BaoCut 这 72 个提交的主战场是风格化字幕渲染（GPU 材质、分页程序、字体来源绑定）和 lite-editor 校对 UI，与 XVT 的翻译字幕主链几乎正交。唯一与 XVT 相交且 XVT 更弱的点是**「字体缺字 / 测量不可用」的处理哲学**：BaoCut 在渲染侧硬失败（`FONT_MISSING` / `FONT_SOURCE_UNREGISTERED_OR_FACE_MISSING`）、在构建侧按字符告警（`fontCmap`），XVT 则静默降级并只留一条不带定位信息的 warning。这与昨天「libass 真实行宽测量是 XVT 强项」不矛盾——强项成立的前提是测量真的跑起来了，本轮补的是前提失效时的可见性。
- **修正一处昨天的覆盖面**：昨天的调研没有读 `export-worker` 与 `skills/**/production-scripts/scripts`（已核对会话记录），B1 / B3 的 BaoCut 侧机制是这次才看到的，但其中 `444807f9`、`bd4e8a78` 两个提交昨天就在盘上，不是今天新拉的。
- **对 HANDOFF 待办的建议**（本文不改 HANDOFF，交主会话决定）：B1 可排在现有待办第 2 条「三行布局行距」之后、A2 之前——它不改协议、不改换行，只补可见性；B2 可随时作为独立小 PR；B3 挂为「观察项」，不立项。

## 5. 核验与未核实项

- 主会话二次核对过的 `文件:行`：`lib.mjs:260-284`、`build.mjs:50/119/126/154`、`caption-cues.ts:720-752`、`render.rs:257-320/411-431`、`yt-dlp.ts:89`、`link-import.ts:95/188/604`、`SKILL.md:170`、`video-format-spec.md:2682`；XVT 侧 `render.py:138/205`、`qa.py:205-228`、`doctor.py:74-144`、`pipeline/burnin.py:216`、`download.py` yt-dlp 参数表；libass 消息格式取自 `/usr/local/opt/libass/lib/libass.9.dylib` 字符串表。
- subagent 自报未核实、但都落在「不相关」桶里不影响结论的：`packages/ui` 里默认分辨率取源尺寸的 TS 实现、`syncTranslationCaptions` 内部实现、`14301318` 的具体 diff、`dynamic-caption-format-design.md` 声称的 `caption-paint-mask/1` 在代码中的落点。
