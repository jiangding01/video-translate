# BaoCut 增量调研（2026-11-01）

- 调研对象：`/Users/jiangding/baocut`（Rust + Electron 开源剪辑软件），diff 区间 `7ad81ece..HEAD`（HEAD = `5d6a71ec`），共 201 个提交、2101 个文件、+110234 −5923。
- 基准：XVT `main@bcb422b`，以 [AGENTS.md](../../AGENTS.md) 的约束与模块表、[HANDOFF.md](../../HANDOFF.md) 的待办（尤其 A2）、[上一轮调研](baocut-rescan-2026-10-23.md)（基线即 `7ad81ece`）与 [CJK 分词器评估](cjk-tokenizer-evaluation-2026-11-01.md) 为判据。
- 方法：Fable 5 统筹拆题与交叉核验，三个 Sonnet subagent 分簇读 diff（CLI/恢复簇、断行/cue 簇、字体簇）；翻译管线（28 行）与 providers 关键 diff 由统筹本人直接读。「值得借鉴」条目的关键 `文件:行` 由统筹用 `sed`/`rg` 二次核对。标注约定沿用分词器评估：**[实测-本人]** / **[实测-sub]** / **[代码]** / **[推断]**。
- 只读调研：除本文件外未改动 XVT 与 BaoCut 任何文件。BaoCut 的 README、设计文档、提交标题一律不算证据。

## 0. 先说四件会影响读法的事

1. **「对标面动了」是误报的一半。** 本轮最重要的探针信号（`packages/providers` + `apps/cli` 在上一轮为空、这一轮 +4735）读下来：providers 的 628 行是 Codex 接入文本生成（agent-text-generator.ts / agent-turn.ts）、图像生成修复和新模型登记（`openai-text.ts` 的 6 行就是登记 GPT-6.1 Terra），**与翻译协议无关** [实测-本人]；apps/cli 的实质是转写 checkpoint、进度协议与提交幂等（见 §1、§2）。
2. **A2 没有拿到实质输入。** 本区间翻译管线全部改动是 28 行：给 `translate` / `translate-subtitles` 加 `effort`（推理强度）参数并透传、冻结进流程参数（`packages/jobs/src/pipelines/translation-batches.ts:157-242`、`translate.ts`、`translate-subtitles.ts`）[实测-本人，读了全量 diff]。长度预算、prompt 措辞、`duration_ms`、批次口径零改动；全区间 diff 按 `duration_ms|max_target|budget|Length is part|charsPerSecond` 扫过，命中全是 GPU 渲染的资源预算 [实测-本人]。A2 的前置条件（真实样本）维持不变。
3. **本区间没有引入 CJK 分词器，断行判定零改动。** 新增依赖核查：被改的清单文件只有两个 package.json（版本号）、`bindings/editor-wasm/Cargo.toml`（workspace 内 crate）、`crates/model-runtime/Cargo.toml`（serde_json 加 feature），无任何 segmentation/tokenizer 依赖 [实测-sub]；中文词内断行实现 `cjk_line_breaks`（`crates/subtitle-render/src/raster.rs:718`，调 `han_word_interior`）区间内一行未动 [实测-sub，本人复核]。但 BaoCut **既有**（`0898dbba`，上一轮基线之前）的 ICU 路线是我们分词器评估的一个现成外部对照，上一轮刻意没展开（当时换行由主会话另行处理），这次补记在 §4，**不推翻**我们的否决。
4. **B3（字体身份绑定）无进展。** `git diff 7ad81ece..HEAD -- crates/export-worker/src/render.rs crates/export-worker/src/preflight.rs crates/render-raster/src/font-source.rs` 为空 [实测-本人]。区间内字体改动全在软路径且方向相反（§3）。

## 1. 新增内容清单（按模块归类，已过滤噪声）

| 模块 | 提交（节选） | 判定 |
| --- | --- | --- |
| `packages/jobs` 翻译管线 | `ea381fb3` effort 透传 | 只加推理强度参数，预算/prompt 零改动；**与 A2 无关** [实测-本人] |
| `packages/providers` | `1d00f8c7` Codex 文本 agent provider；`ea381fb3`；`300357cd` 图像响应兼容；`9fe28357` 登记 GPT-6.1 Terra | 多 provider 接入与模型登记，**不相关**（XVT 走 OpenAI-compatible 单协议） |
| `crates/model-runtime` + `packages/jobs` | `65250482` / `2e38af84` per-input 转写 checkpoint；`f12eefd2` 单 writer；`0ca15a6d` commandId 幂等；`a0161303` / `6e94f2c7` 分阶段计时 | **可借鉴一处**（C2：复用键绑定模型身份），其余已覆盖或不适用（§2、§3） |
| `packages/jobs/src/pipelines/caption-cues.ts` + `packages/ui/src/model/speech-cues.ts` | `347270c3` 成对标点守护 + 短 cue 合并 | **可借鉴一半**（C1：配对栈谓词）；短 cue 合并与译文现算切条触碰约束 1/3（§3） |
| `crates/model-runtime/src/speech/text_prep.rs` | `751bc4ff` 对齐前拆连写 em-dash | 机制干净但 XVT 无对应路径（§3） |
| `apps/cli` | `a5807772` 等待时嵌套 step 进度；`95ef90e7` 终态回填实际发布文件名；`908040aa` audio.cpp 模型管理 | 进度协议形态可记一笔；退出码表/`next` 字段 XVT 已覆盖（§3） |
| `crates/subtitle-render`、`crates/caption-scene`、`crates/render-raster` | `3af0d72b` tag run 隔离、`ece8c671` rolling line slots、scan-warp / radial-warp / 噪声位移等约 +14.8k | 风格化逐词字幕与 GPU 材质，**不相关**；断行候选判定未变 [实测-sub] |
| 字体 | `41b15eba` 缺 face 提前记录（族名级、告警）；`1d5ac143` 系统中文黑体缺席静默回退白名单；`85de4517` fonts.mjs 字体子集后按字符 cmap 复查 | B3 无进展；`1d5ac143` 与 fail-closed 方向相反（§3）；fonts.mjs 是 B1 思路在它家技能脚本侧的延伸（warn-only） |
| `packages/models`、transcribe GPU 探测 | audio.cpp 0.9.3、HTDemucs/Mel-Band 分离、`c91a8d95` Whisper GGML 避开弱 GPU | **不相关**（XVT 用 MLX/faster-whisper，CPU int8） |
| agents / bot-mode / shorts / image-tools / onboarding / 发版 / skills 制作类 | 约占区间大半（HEAD 即 bot-mode 设计文档） | **不相关**：桌面 UI、常驻 Runtime、剪辑制作，见 AGENTS「项目定位」 |

## 2. 值得借鉴项

### C1.（低-中）成对标点的「配对栈 + 整对宽度门槛」守护进译文换行

- **BaoCut 机制** [实测-sub，关键行本人核对]：`packages/jobs/src/pipelines/caption-cues.ts:160-174` 的 `PAIRS` 共 13 对（弯双/单引号、「」『』《》〈〉、中西括号、直双引号按出现次序两两配）；**直单引号刻意不配**（兼作撇号，`Woolf’s` 的右弯单引号无左配对时也当撇号，注释 `:156-158`）。`pairedSpans`（`:179-193`）用配对栈扫描，闭合时 `findLastIndex` 取最外层、丢弃未闭合的内层；孤立闭符号忽略。守护有**宽度门槛**：`pairInteriorGaps`（`:196`）只保护「整段含两端符号不超过 `maxChars`」的 span——**整对放不进一行就放弃守护，照常在内部切**，测试断言了这一点（`packages/ui/src/model/translation-cues.test.ts`）。
- **它解决的问题**：`「……」` / `"..."` 被换行拆成两半，开闭符号隔行。
- **XVT 现状** [代码]：`segment.forbidden_break`（segment.py:131，全仓唯一禁则谓词）管的是行首行尾禁则（闭括号不上行首、开括号不落行尾），这能保证断点不紧贴符号，但**不保证整对同行**——一对引号中间的普通字符缝今天是合法断点。
- **XVT 落点**：`segment.wrap_target_lines` 的 DP 一步：按配对栈算出「放得进一行的成对标点 span」，span 内的缝从 DP 候选边界里剔除（不是新增切分出口，`forbidden_break` 的事后复核照旧）；整对超宽时不剔除。随改动：`TARGET_WRAP_VERSION` 升版 + `target_wrap_binding` 绑定（照分词器评估 §6.3 与 2026-10-24 片假名条目的先例，在途 Job 布局 QA 不复用属预期）。
- **约束自查**：纯展示层字符串处理，不碰 cue ID/时间轴（约束 1）；确定性代码（约束 3）；守不住仍返回 `None` 维持 review（约束 4）；零依赖。
- **优先级建议**：低-中。规则成熟可直接抄（配对表、撇号例外、宽度门槛三条都想清楚了），但目前没有「引号被拆」的真实 review 样本；可挂与换行相关的下一次迭代顺手做，或等坏例出现。

### C2.（低）转写产物复用判定绑定 ASR 引擎与模型身份

- **BaoCut 机制** [实测-sub，key 构成本人核对 `checkpoint.rs:63` 起]：per-input 转写 checkpoint 的复用键是一份 JSON 的 sha256，包含格式版本、Worker 版本、模型包（bundleId、backend、各组件 family/revision）、**解码后音频样本的 sha256**（`audio_digest`，`crates/model-runtime/src/checkpoint.rs:122-132`，连 ffmpeg 解码差异都能证伪）、language/diarize 等参数与环境旋钮；Worker 可执行文件的「大小 + mtime」也进 key，但注释写明只用来让开发构建证伪，不证实——与我们约束 9 的「元数据只证伪」同口径。单 writer 用同一句柄 `try_lock` 独占（`:337-341`），拿不到锁就本次不用 checkpoint 整个重跑、不碰文件。checkpoint 只省时间不定结果，续跑产物与一次跑完逐字节相同（有测试，`tests/transcribe_job.rs:1320/1571`）。
- **XVT 现状** [实测-本人]：`word_transcript` 的复用（`resume.py:278/416`）只认 artifact 内容 checksum；`Transcript` 落盘记 `engine`（mlx/faster）但不记模型名（`transcribe.py:163`，faster 腿的 `model_name` 用后即弃 `:182`）；模型目前不可由用户配置（cli 无相关旗标）。风险场景：`DEFAULT_MODEL` 随 XVT 版本升级、或同一 Job 目录在 mlx↔faster 机器间迁移后 `resume`/`rebuild-source` 静默复用旧 words——checksum 证实的是「文件没坏」，证实不了「还是当前配置会产出的那份」。
- **XVT 落点**：`models.Transcript` 增加 model 字段（mlx 腿记其模型标识，faster 腿记 `model_name`），`resume` / `rebuild-source` 复用 words/audio checkpoint 时与当前 engine/model 比对，不符按失效处理（或先只发 warning，分两步走）。这与 XVT 既有的 fingerprint 风格（`revision_fingerprint`、批次指纹）同构，不与约束 9 冲突（参数指纹用于证伪，内容 checksum 的证实语义不变）。
- **优先级建议**：低。模型不可配时触发条件罕见；若日后开放 `--whisper-model` 一类旗标，这条升为必做前置。
- **代价**：`Transcript` schema 加字段要兼容旧 words.json（缺字段视为未知、只警不拦），测试补一个「engine 不符不复用」的用例。

## 3. 不值得借鉴项与理由

| 条目 | BaoCut 位置 | 结论 | 理由 |
| --- | --- | --- | --- |
| 短 cue 合并（`mergeShortCues`：时长 < 1s 且同句同 speaker 合并，两边不行就挪词）与译文现算切条（`translationCues` 把译文按宽度线性分配时间） | `caption-cues.ts:36/527-690` [实测-sub] | **不值得** | 它的 cue 是词流派生物、可重算；XVT 的 cue 是固定输入，时间轴只由 ASR/用户源字幕确定（约束 1），译文按 ID 一一对应（约束 3）。整条路径方向相反。可记一笔：它合并/挪词只重分组词、词时间不动、不跨句——若 XVT 将来做**源侧**分段参数迭代可参考其排序键设计（seamStrength：句末 2 / 从句标点 1 / 无 0）。 |
| 对齐前拆连写 em-dash（只认 U+2014/2015、两侧都有字母数字才切、破折号留左词） | `crates/model-runtime/src/speech/text_prep.rs:146/164`，真实样本注释在测试 `:337` [实测-本人核对] | **不适用** | 那是 Qwen3-ASR 的书写习惯（`innovative—it's` 连写导致对齐器两词一份时间）。XVT 的词由 Whisper 词级时间戳直接给出，没有「文本再 token 化去强制对齐」的路径；`align.py` 做 cue 级模糊对齐且 `normalize_text` 把标点整类剥掉（align.py:30-35）[实测-本人]。若未来接文本型 ASR 输出再回头看。 |
| commandId 幂等账本（重启后重复提交返回原 Job、不比参数、不自动重试） | `packages/jobs/src/job-ledger.ts:22`、`job-manager.ts:2869` [实测-sub] | **不适用** | 那是常驻 Runtime 的 RPC 重提交问题。XVT 的 `run` 显式建新 Job、`continue JOB` 显式点名，加上 Job 目录 flock（locking.py），没有「同一命令可能提交两次」的窗口。 |
| 退出码表与 `next` 字段（`ok=0/failed=1/actionRequired=2/runtimeUnavailable=3/invalidArguments=4`，失败信封带建议命令） | `apps/cli/src/envelope.ts:20-31` [实测-本人核对] | **已覆盖** | XVT 的 `errors.ExitCode` 同样是单一事实来源且语义更细（等待态 10、批处理 6 优先于 10）；`next` ≈ `status` 的 `primary_command`，XVT 还多了「不够格就不推荐」的资格判定。 |
| 等待时进度协议（progress JSONL 走 stderr、`step{}` 嵌套子任务、去抖键 = 阶段\|整百分比；stdout 只留终态信封） | `apps/cli/src/catalog/progress.ts:47-67` [实测-sub] | **不值得（架构差异），备忘一条** | XVT 的 JSONL 事件是持久化恢复依据，`TRANSCRIPTION_PROGRESS` / `BURN_PROGRESS` 本就落盘，流/盘分离对 XVT 是另一套架构。可记一笔：「阶段+整百分比」去抖键是便宜的写放大控制，若将来发现进度事件刷大 events.jsonl 可采用。 |
| 终态后用实际发布文件名回填 `result.files`（重名自动加序号场景） | `apps/cli/src/catalog/run.ts:268-291` [实测-sub] | **不适用** | XVT 走 `paths.py` 覆盖保护：重名直接拒绝而不是悄悄改名，不存在「回执名 ≠ 实际名」。 |
| 缺字体白名单静默回退（八款系统中文黑体缺席时不告警、回退内置 Noto Sans SC；宋体刻意不在名单——「换成黑体看得出来，照报」） | `crates/render-raster/src/fonts.rs:328-344`、`render_plan.rs:1372-1381` [实测-本人核对] | **不借鉴，作 B1 对照** | 方向与约束 4 的 fail-closed 相反：BaoCut 字幕主路径按**名字**引用字体时缺了就静默回退（本区间还又放宽一档），只有按 **sha256 身份**引用才硬失败。XVT 的 B1（按字符定位缺字、libass stderr 解析）落地后将强于它的族名级告警。它家唯一字符级检查在技能脚本 `fonts.mjs:82-84`（字体子集后 cmap 复查，warn-only），是上一轮 `build.mjs checkGlyphs` 的延伸，B1 已吸收该思路。 |
| 缺 face 提前记录（渲染计划编译期按族名+字重+斜体收集，不等排字） | `crates/subtitle-render/src/render_plan.rs:1368-1381` [实测-sub] | **不适用** | 服务于「预览期向宿主请求本机字体」的桌面场景；XVT 烧录前 doctor + 真渲染探测已覆盖同一时机需求。 |
| Worker 握手超时改判 crashed 重试、失败 remedy 按 reason 分流、audio.cpp 升级与分离模型、GPU 探测、agents/bot-mode/shorts/image-tools/onboarding/发版 | 各处 | **不相关** | 常驻 Runtime 与桌面功能；XVT 无 Worker 进程模型。remedy 分流可取的只有「错误带结构化 reason」这一原则，XVT 的 `error_category` 已有。 |

## 4. 与上一轮（2026-10-23）结论的差异，及对分词器否决的外部对照

- **不变**：总结论仍是「可借鉴的是算法与协议，不是常驻 Runtime 架构」；XVT 在 libass 真实行宽测量（前提可见性由 B1 补）、Q6 语义校对、任务级 `recovery_plan` 三处仍强于它；B1/B2 的落地判断不受影响。
- **A2**：上一轮说「转写与翻译核心零改动」，本轮核心**动了但与 A2 无关**——全部是 effort 参数透传与模型登记，预算口径、prompt、`duration_ms` 仍零改动 [实测-本人]。A2 的前置条件（先拿模型因预算砍术语的真实样本）维持原样，本轮没有提供任何该方向的新证据。
- **B3**：无进展，维持观察项不立项 [实测-本人，三个关键文件区间 diff 为空]。补一笔方向信息：本区间 `1d5ac143` 在「按名字引用」的软路径上反而放宽（白名单静默回退），它家的格局定型为「名字引用宽松回退、身份引用硬失败」双层并存——B3 若将来立项，对应的是后一层。
- **对分词器否决的外部对照（不推翻，补两条记录）**：本区间 BaoCut 没有在分词/断行上动手（§0.3），否决的「需求侧为零」前提不受影响。但调研中确认了它**既有**路线（`0898dbba`，上一轮基线前，上一轮刻意未展开）与我们评估的两点差异，值得记进案底：
  1. **形态差异**：BaoCut 把中文词内断行做成换行 DP 里的**软惩罚**而不是硬禁则——`raster.rs:715-722` 注释写明断在词内加 `40 × 字号²`（封顶 5e5）、排在禁则与孤字之后、「宁可断在词里，也不把句读推到行首」、词典不认识的词照样可能拆开 [实测-本人]。这恰好绕开了我们评估 §5.2 担心的负收益路径（「模型认为不可断→无解→review 变多」）：软成本永不产生无解。**若**有一天中文换行真要词边界，正确形态是软惩罚进 `_partition_words` 的成本函数，而不是扩 `forbidden_break`。
  2. **候选遗漏**：我们的评估把中文轻量选项写成「只有 jieba（不过关）」，没有测 ICU cjdict 路线（BaoCut Rust 侧 `icu_segmenter` auto 模式、TS 侧 `Intl.Segmenter`，同一份词典）[代码]。Python 侧对应 PyICU（系统 ICU 绑定，非纯 Python），依赖形态比 budoux 重、比 Sudachi 轻；其 zh 切分质量已由主会话补测（Node `Intl.Segmenter`，ICU 78.3）：**接近逐字、整体比 budoux zh 更差**，详见[分词器评估](cjk-tokenizer-evaluation-2026-11-01.md) §8.2 [实测-主会话]。这不改变「现在不做」的结论（需求仍为零），只是评估文档的候选表少了一行，触发条件成真时应把 cjdict 纳入届时的对比。

## 5. 核验与未核实项

- 统筹本人直接实测/核对过的：翻译管线全量 diff（28 行）与 `openai-text.ts` diff；全区间 diff 的预算/分词器关键词扫描；`caption-cues.ts:160-196`（PAIRS/配对栈/宽度门槛）、`:36`（minCueDisplaySec）；`text_prep.rs:146/164` 与测试 `:337`；`checkpoint.rs:63/122-132/337-341`；`envelope.ts:20-31`；`fonts.rs:328-344`；`render_plan.rs:1368-1381`；`fonts.mjs:82-84`；`raster.rs:715-722`；export-worker 三文件区间 diff 为空；`0898dbba` 为上一轮基线祖先（`git merge-base --is-ancestor`）；XVT 侧 `transcribe.py:163/182`、`align.py:30-35`、`resume.py:278/416`、HANDOFF A2 原文、`events.py` 进度事件类型。
- 采信 subagent、未逐一亲核的：caption-cues 与 speech-cues 两份 TS 实现的逐行等价性（subagent 自报只核了改动量接近）；checkpoint 7 天 GC 与删除时机的行号（job-manager.ts:2716/2875 一带）；commandId 账本淘汰策略（subagent 标注未核实，文档声称在 §7.9，代码未追到）；`pipelines.retry` 远端节点 timing 转发细节；Latin 文本在白名单字体缺席时是否也回退 Noto Sans SC。
- 明确未核实/标注推断的：PyICU 的中文切分质量（只记为「未测候选」）；BaoCut 单测在 HEAD 是否全通过（没跑它的测试套件）；`mergeShortCues` 排序键的全部分支行为（只核了阈值与「词时间不动」两条主断言）。
