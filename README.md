# XVT Video Translate

[![CI](https://github.com/jiangding01/video-translate/actions/workflows/ci.yml/badge.svg)](https://github.com/jiangding01/video-translate/actions/workflows/ci.yml)

XVT 是一个本地优先的视频转写、双语字幕翻译与烧录 CLI。它以稳定的时间轴为核心，支持本地媒体、已有字幕和 YouTube URL，输出 `SRT`、`ASS`，并可选烧录为新视频。

## 能力概览

- 本地视频、音频、YouTube URL 输入
- MLX Whisper 或 faster-whisper 转写，保留词级时间戳；超宽 ASR 片段会优先按语义边界切分；faster-whisper 显示真实完成比例，MLX 显示诚实的运行时长
- 导入 SRT、VTT、ASS、SSA 源字幕，默认保留原时间轴；输入字幕必须是 UTF-8（容忍 BOM）
- YouTube 平台字幕仅在显式请求时下载；新 Job 以候选 QA 和 `--source-strategy auto|subtitles|asr` 选择最终时间轴
- OpenAI-compatible Chat Completions 翻译，或 Agent 翻译批次协议；确定性语义组避免在 provider 批次边界切开句意
- 翻译断点续跑、配置指纹、429/5xx 退避和请求超时
- 已有端到端 Job 可显式补跑可恢复译文润色，支持 API checkpoint 与 Agent 回填
- 对模型生成的简体中文字幕进行保守标点收口：消除全角中文逗号和句号（句中按需替换为半角空格以保留词边界），连续句号归一为单个省略号，保留问号、分号、冒号、破折号和原有换行；导入译文、人工修订、源字幕及非简中不改写
- 原文、译文、双语 SRT，及独立中英样式的双语 ASS
- 按 cue 或时间范围生成短片段预览，复用正式 ASS 样式和烧录参数
- `conservative`、`balanced`、`compact` 三种 ASR 字幕分段预设，默认保持既有平衡行为
- 字幕 QA、媒体 QA、FFmpeg 硬件或软件烧录
- `job.json`、checksum 校验、恢复和 JSONL 结构化事件
- 单命令端到端工作流，以及基于持久化目标的 `xvt continue`
- 目录批处理、独立 Job、失败隔离和可恢复的 `batch.json` 汇总报告

时间轴只由 ASR 或源字幕决定。翻译阶段只能替换文本，不能改动 cue ID 或时间码。

## 下一阶段规划

在开始可视化校对界面之前，项目已完成 Q1–Q6.1 字幕质量闭环和 R2.4 烧录成品重新关联：翻译传输可靠性、源字幕自适应选择、既有 Job 的 ASR 源重建、语义组翻译、可恢复润色、语义校对与一次定向修复，以及人工修订和提示词加固。polished API 路径已通过公开素材真实 E2E；polished Agent 主链也已于 2026-10-11 完成真实 E2E，默认保持 `standard + faithful`；下一步依次推进 Q6.2 语义复查收敛与多样本质量对照、R3 provider/checkpoint 收敛，再评估 Q7 翻译上下文与术语候选包，最后进入可视化校对工作台。完整顺序见 [路线图](ROADMAP.md)，字幕质量设计见 [字幕质量与自适应源字幕技术方案](docs/SUBTITLE_QUALITY_PLAN.md)。

该文档同时记录已完成和待实现阶段；当前可用命令和行为仍以本 README 其他章节及 `xvt --help` 为准。

## 默认命令路径

日常优先记住三条命令；中断后先看 `status`，再执行它提示的下一步：

```text
xvt run INPUT --output DIR [--target-language LANG] [--translation-quality standard|polished] [--burn]
xvt status JOB.json
xvt continue JOB.json
```

Agent 等待输入时：

```text
xvt apply JOB.json RESPONSE
xvt apply JOB.json RESPONSE --continue  # 回填后在同一进程继续端到端任务
```

`xvt apply` 的路由规则：人工修订 JSON 模板（`source-review-*.json` / `translation-review-*.json`，CLI 生成、用户编辑后原样提交）携带 `task` 字段，按该字段路由；人工修订 SRT（`source-review-<fp12>.srt` / `translation-review-<fp12>.srt`）按固定文件名识别为对应修订通道；Agent 翻译/润色/语义校对响应不含 `task` 字段，按 Job 当前等待的步骤路由——这要求 Job 是 `run`/`batch` 创建的端到端 Job（有持久化 `config.workflow` 目标）。原子 Job（例如 advanced 用法中单独 `xvt translate` 创建的等待）不受统一入口支持，请直接使用对应的 `apply-*` 命令。局部重译（`retranslate-cues`）的响应不由 `xvt apply` 路由——`apply-cue-retranslation` 需要 `xvt apply` 拿不到的 cue ID 与目标语言：无论响应带不带 `task: retranslate_cues`，`xvt apply` 都会以退出码 2 报错并给出可复制的 `xvt apply-cue-retranslation` 命令形状（cue ID 与目标语言可从 `artifacts/cue-retranslation-batches.json` 的 `batches[].items[].id` 与 `target_language` 读出）。增加 `--continue` 可在回填成功后立即继续同一端到端任务；源文修订不接受该参数，因为它会失效译文及下游产物，回填后请先查看 `status`。各 `apply-*` 命令仍是稳定入口，行为不变。

目录批处理：

```text
xvt batch DIR --output DIR ...
xvt batch-continue BATCH.json
```

`resume`、`translate`、`burn`、`preview`、`resegment-translation` 等是**原子/修复**入口（`xvt --help` 中标为 `[advanced]`）。端到端 Job 不要用 `resume` 代替 `continue`：前者只恢复转写 checkpoint，后者完成 `job.config.workflow` 里的目标。

## 完整命令与参数参考

人工译文修订可先运行 `xvt apply JOB REVIEW --check`（或显式 `apply-translation-review-fixes ... --check`）。预检校验 JSON/SRT 身份、时间轴、译文完整性、术语和行宽，返回结构化 QA；有效退出 0，无效退出 2。它不写 Job、产物或锁文件，不调用翻译 API，也不执行语义复查；`--check` 不能与 `--continue` 合用。正式提交共享同一预检，仍有译文 QA 阻断项时拒绝保存，修好后再运行 `xvt apply JOB REVIEW --continue`。

译文的行宽 QA 在已知画幅时使用 FFmpeg/libass，按最终 ASS 字体、字号和描边测量。超过建议字数但实际放得下时只给 warning；实际超出可用宽度时仍需处理。中文 36 显示单位仍是建议值。无画幅或测量失败时保留原有保守限制，测量失败另有 warning。语义校对、时间轴和术语规则保持有效；不要为满足字数建议删掉原文信息。预览与烧录也检查所选媒体、字体；换画幅或字体后不能复用旧布局确认。原子 `burn` / `preview` 的临时布局检查不覆盖正式 QA，检查失败后仍可按原目标 `continue`；`run` / `continue` 保存工作流目标的布局 QA。

<!-- cli-reference:start -->

以下内容覆盖当前 CLI 的全部命令和参数。运行 `xvt --help` 查看命令列表，运行 `xvt COMMAND --help` 查看某条命令接受的参数；这里补充参数用途、默认值和容易混淆的行为。方括号表示可选项，`...` 表示可重复参数。

### 全局参数

| 参数 | 说明 |
| --- | --- |
| `--version` | 输出 XVT 版本并退出。版本号不能证明工作区代码是否最新；开发环境还应检查 Git commit 和 `xvt.__file__`。 |

### 公共参数词典

同名参数在各命令中的含义一致；每条命令实际接受的参数以其后面的命令清单为准。

| 参数 | 说明 |
| --- | --- |
| `--output PATH` | 输出目录或输出文件，具体含义由命令决定。`run`、`batch` 必填且表示输出目录；`doctor` 可用它检查目标目录；`preview`、`burn` 中表示媒体输出文件。 |
| `--json` | 输出适合程序读取的单个 JSON 文档。 |
| `--events-jsonl` | stdout 只输出 v1 JSONL 事件；人类可读进度不混入 stdout。 |
| `--engine mlx\|faster` | ASR 引擎，默认 `mlx`。选中的后端没有安装时回退到另一个：会打印 `[回退] ASR 引擎：…`、发出 `transcription_fallback` 事件，并把**实际生效的引擎**写回 `job.config.engine`（请求的引擎只留在该事件里）。 |
| `--language LANG` | 源语言代码，例如 `en`。ASR 不传时自动检测；导入字幕通常应显式提供，未传时以 `auto` 保存。 |
| `--subtitles PATH` | 显式提供源字幕；默认保留其 cue ID 和时间轴并跳过 ASR。 |
| `--source-strategy auto\|subtitles\|asr` | 源字幕选择策略，默认 `auto`。`subtitles` 强制采用候选字幕，`asr` 强制语音转写；详见下方“源字幕选择速查”。 |
| `--align-subtitles` | 对显式源字幕或平台字幕额外运行 ASR，并按语音结果修正整体偏移或漂移。需与 `--subtitles` 同用，不能与 `--source-strategy asr` 同用。对齐结果出现 error 级字幕 QA 问题时直接失败，不会静默回退。 |
| `--min-alignment-confidence FLOAT` | 对齐结果的最低置信度，默认 `0.55`。 |
| `--allow-low-confidence-alignment` | 显式接受低于阈值的对齐结果；只放行低置信度，不放行对齐后 error 级 QA 问题，且仍需通过后续字幕 QA。 |
| `--segmentation-preset conservative\|balanced\|compact` | ASR cue 分段预设，默认 `balanced`；不改变导入字幕的既有时间轴。 |
| `--youtube-subtitles` | 对 YouTube 输入请求平台字幕候选；不传时不会下载平台字幕。 |
| `--subtitle-language LANG` | 希望下载的平台字幕语言，例如 `en`。下载目录中同时存在多个语言或多个变体的字幕文件时，按该语言优先选取。 |
| `--allow-auto-subtitles` | 人工字幕不可用时允许下载 YouTube 自动字幕候选。 |
| `--cookies-from-browser BROWSER` | 让 yt-dlp 从指定浏览器读取登录 Cookie，例如 `chrome`；只记录浏览器名称，不持久化 Cookie 内容。 |
| `--proxy URL` | 为远程检查与下载显式指定代理；凭据会在事件和错误边界脱敏。 |
| `--translation-subtitles PATH` | 导入已有译文字幕，跳过翻译提供方并映射到源 cue 时间轴；不能与 `--glossary` 同时使用。 |
| `--target-language LANG` | 目标语言代码，例如 `zh-CN`。 |
| `--translation-quality standard\|polished` | 翻译质量链，默认 `standard`；`polished` 增加润色、语义校对和最多一次定向修复。 |
| `--provider auto\|openai-compatible\|agent` | 翻译提供方，默认 `auto`：配置完整时调用兼容 API，否则生成 Agent 批次。Job 已固化 Base URL 与模型、当前终端只缺 API Key 时，降级原因会指名缺的是哪个环境变量。 |
| `--base-url URL` | OpenAI-compatible API 根地址；XVT 请求 `<base-url>/chat/completions`。不传时读 `XVT_TRANSLATION_BASE_URL`；端到端 Job 在首个翻译批次成功后把生效值写回 `job.config.workflow`，此后 `continue` / `status` 不再受该环境变量漂移影响。**不得带用户名密码或 query 串**（`https://user:pw@host/v1`、`https://host/v1?api-version=x`）：这类地址会被直接拒绝，凭据请放进 API Key 环境变量。 |
| `--model NAME` | 翻译模型名称。不传时读 `XVT_TRANSLATION_MODEL`；写回规则与 `--base-url` 相同。换模型请重新 `run`，或对已有 Job 执行 `xvt translate JOB --model NEW`。 |
| `--api-key-env NAME` | 保存 API Key 的环境变量名，默认 `XVT_TRANSLATION_API_KEY`；参数值是变量名，不是密钥。 |
| `--batch-size N` | 每个翻译请求最多包含的 cue 数，默认 `25`；语义组不会被批次边界拆开。 |
| `--max-retries N` | 翻译传输失败后的额外重试次数，默认 `4`（连同首次请求最多 5 次传输尝试）；退避为带抖动的指数退避（每次在上一档退避的一半到全额之间随机取值，上限 30 秒），服务端给出 `Retry-After` 时按其指定时刻等待、不加抖动；结构性无效响应另有独立纠错预算。 |
| `--timeout-seconds SECONDS` | 单次翻译 API 请求超时，默认 `180` 秒。 |
| `--glossary PATH` | JSON 术语表；命中源 cue 的术语会进入请求并由确定性 QA 检查。不能与 `--translation-subtitles` 同时使用（术语校验的前提是译文由 XVT 按术语表生成）。 |
| `--translation-style faithful\|natural\|concise` | 翻译风格，默认 `faithful`。 |
| `--burn` | 在端到端工作流中生成双语 ASS、烧录视频并执行媒体 QA。 |
| `--burn-media PATH` | 为本地纯音频 Job 显式提供烧录所需的视频。 |
| `--burn-output PATH` | 指定 `run` 的最终烧录文件；必须是安全的绝对输出路径。 |
| `--media PATH` | 为 `preview` 或原子 `burn` 指定视频媒体。 |
| `--software` | 显式强制使用软件编码。`run` / `burn` 不传时优先硬件编码并在明确不支持时回退；`preview` 不传时优先复用 Job 保存的烧录设置。 |
| `--target-font NAME` | 目标语言字幕字体。 |
| `--source-font NAME` | 源语言字幕字体。 |

### 源字幕选择速查

| 需求 | 推荐参数 | 行为 |
| --- | --- | --- |
| 让 XVT 自动选择 | `--source-strategy auto` | 人工平台字幕没有 error 级问题时走快速路径（`needs_review` 仍会在正式 QA 等待确认）；error 级问题先尝试对齐再回退 ASR；翻译或烧录任务中的自动字幕只作参考并最终采用 ASR。 |
| 无论质量如何都使用下载字幕 | `--source-strategy subtitles --youtube-subtitles` | 强制候选作为时间轴；正式 QA 的 `failed` 或 `needs_review` 仍会阻断任务。 |
| 不信任平台字幕，强制听写 | `--source-strategy asr` | 使用媒体音频重新生成源 cue；可同时请求下载字幕作为候选证据，但不把它作为最终时间轴。 |
| 修复旧 Job 的源时间轴 | `rebuild-source JOB --strategy asr` | 显式重建已有媒体 Job，失效旧翻译及下游，不要新建 Job。 |

### 日常命令

<details>
<summary><code>xvt run</code>：创建 Job 并执行端到端工作流</summary>

```text
xvt run INPUT --output DIR [源字幕参数] [翻译参数] [烧录参数] [--fail-on-waiting]
```

- 位置参数：`input`，本地视频、音频、字幕文件或远程 URL。
- 必填参数：`--output`。
- 源字幕与 ASR：`--engine`、`--language`、`--subtitles`、`--source-strategy`、`--align-subtitles`、`--min-alignment-confidence`、`--allow-low-confidence-alignment`、`--segmentation-preset`。
- YouTube 访问：`--youtube-subtitles`、`--subtitle-language`、`--allow-auto-subtitles`、`--cookies-from-browser`、`--proxy`。
- 翻译：`--translation-subtitles`、`--target-language`、`--translation-quality`、`--provider`、`--base-url`、`--model`、`--api-key-env`、`--batch-size`、`--max-retries`、`--timeout-seconds`、`--glossary`、`--translation-style`。
- 烧录与输出：`--burn`、`--burn-media`、`--burn-output`、`--software`、`--target-font`、`--source-font`、`--events-jsonl`。
- 退出码：`--fail-on-waiting`。停在等待态时默认返回 0（`awaiting_input` 是 Agent 协议的正常步骤，这是承诺不是疏漏）；需要脚本区分「完成」与「等待回填」时加上它，等待态返回退出码 10；旗标只改退出码，不发 `cli_failed`，Job / batch 状态与事件流完全不变。

</details>

<details>
<summary><code>xvt status</code>：查看 Job 状态和下一步</summary>

```text
xvt status JOB_FILE [--json]
```

- 位置参数：`job_file`。
- 可选参数：`--json`。优先使用本命令提供的 `recovery_plan` / `advance_steps`，不要猜测该运行 `resume` 还是 `continue`。若烧录目标的其他静态条件和 v2 proof 均有效、但已登记成品缺失或 checksum 失效，状态为 `awaiting_input`：首选命令是 `relink-burned-output`，备选是显式 `continue` 重烧；这只表示成品**可能**可重新关联，命令仍会重新校验实际源媒体内容。
- 校验强度：`status` 对产物做与 `continue` 完全相同的全量 checksum 校验，不是「轻量版 `verify`」；同一次调用内对同一个文件只哈希一次，因此哈希量与 `verify` 相同。两者的差别在输出粒度（`status` 给恢复计划，`verify` 给逐产物 pass/warn/fail）和退出码（`status` 恒为 0，`verify` 有 fail 时为 5）。

</details>

<details>
<summary><code>xvt continue</code>：继续端到端目标</summary>

```text
xvt continue JOB_FILE [--cookies-from-browser BROWSER] [--proxy URL]
  [--acknowledge-subtitle-review] [--acknowledge-translation-review]
  [--events-jsonl] [--fail-on-waiting]
```

- 位置参数：`job_file`。
- 远程重试：`--cookies-from-browser`、`--proxy`。
- 人工确认：`--acknowledge-subtitle-review` 只确认当前 `needs_review` 字幕 QA；`--acknowledge-translation-review` 只确认当前译文语义报告。两者都不能绕过 error 级确定性 QA。
- 事件：`--events-jsonl`。
- 退出码：`--fail-on-waiting`。停在等待态时默认返回 0（`awaiting_input` 是 Agent 协议的正常步骤，这是承诺不是疏漏）；需要脚本区分「完成」与「等待回填」时加上它，等待态返回退出码 10；旗标只改退出码，不发 `cli_failed`，Job / batch 状态与事件流完全不变。

</details>

<details>
<summary><code>xvt apply</code>：自动路由 Agent 或人工修订响应</summary>

```text
xvt apply JOB_FILE RESPONSE_FILE [--check | --continue] [--events-jsonl] [--fail-on-waiting]
```

- 位置参数：`job_file`、`response_file`。
- `--check` 仅预检人工译文 JSON/SRT，不保存、不调用模型；有效返回 0，无效返回 2，不能与 `--continue` 合用。
- 可选参数：`--continue` 在回填成功后继续同一端到端工作流；`--events-jsonl` 输出纯 JSONL 事件。源文修订会失效译文及下游产物，因此不接受 `--continue`。
- 退出码：`--fail-on-waiting`。停在等待态时默认返回 0；加上它之后，`--continue` 的续跑停在 `awaiting_input` 时返回退出码 10；旗标只改退出码，不发 `cli_failed`，Job 状态与事件流完全不变。不加 `--continue` 时该旗标没有作用——单纯的回填不会停在等待态。
- 仅用于有持久化工作流目标且能从响应或等待状态确定路由的 Job；无法安全判断时会报错而不会猜测。

</details>

<details>
<summary><code>xvt batch</code>：串行处理目录输入</summary>

```text
xvt batch INPUT_DIRECTORY --output DIR [--recursive] [转写/翻译/烧录参数]
  [--fail-on-waiting]
```

- 位置参数：`input_directory`；必填参数：`--output`。
- 扫描：`--recursive`。
- 转写：`--engine`、`--language`、`--source-strategy`、`--segmentation-preset`。
- 翻译：`--target-language`、`--translation-quality`、`--provider`、`--base-url`、`--model`、`--api-key-env`、`--batch-size`、`--max-retries`、`--timeout-seconds`、`--glossary`、`--translation-style`。
- 烧录与输出：`--burn`、`--software`、`--target-font`、`--source-font`、`--events-jsonl`。
- 退出码：`--fail-on-waiting`。停在等待态时默认返回 0；加上它之后，「无失败条目、有等待条目」返回退出码 10；有失败条目仍返回 6（**6 优先于 10**，失败压过等待）。

</details>

<details>
<summary><code>xvt batch-continue</code>：继续已有批处理</summary>

```text
xvt batch-continue BATCH_FILE [--events-jsonl] [--fail-on-waiting]
```

- 位置参数：`batch_file`。
- 可选参数：`--events-jsonl`。遇到 `awaiting_input` 会停止该项，不会自动重烧可能已移动、替换的烧录成品，也不会把「等待 Agent 回填」的条目记为已完成；等待项包括 Agent 翻译/润色/语义校对回填、人工译文确认、烧录成品重新关联，以及局部重译（`retranslate-cues`）回填。先按 Job 的 `status` 处理。
- 已完成条目的**中间**产物（`normalized_audio`、`word_transcript`、批次 manifest 等）失效不再改写成失败：条目仍是 `completed`，只在 `item["verification"]` 与 `batch_item_completed` 事件里标注 `degraded` 并列出失效 kind，需要逐项审计时运行 `xvt verify JOB`。交付物失效不适用此放行——缺烧录成品仍停在 relink 引导，缺 `bilingual.ass` 或 `source.srt` 会落到 `continue` 重建（前者由 `render` 零 API 成本重新渲染，后者由 `resume` 重建）。
- 普通模式（不加 `--events-jsonl`）会在汇总行之后逐条打印需要处理的条目：状态、`job.json` 路径、恢复建议与可复制的下一步命令（最多展开 10 条，其余提示改用 `--events-jsonl` 看全量）。所有打进终端的人读行都会转义控制字符（`\r`、ESC 等显示为 `\r` / `\x1b`，中文等正常 Unicode 不变），避免扫描到的文件名、远端视频标题或错误消息覆盖已打印的行、清屏并伪造出一条 `next:` 命令；这是全 CLI 的统一行为，不限于批处理条目导航。反过来，**stdout 上的纯产物路径清单不转义**——那是给 `xargs` / 管道消费的契约，转义会改掉真实路径；`--events-jsonl` 的事件流同样保留原文。
- 退出码：`--fail-on-waiting`。停在等待态时默认返回 0；加上它之后，「无失败条目、有等待条目」返回退出码 10；有失败条目仍返回 6（**6 优先于 10**，失败压过等待）。

</details>

<details>
<summary><code>xvt verify</code>：重新校验 Job artifact</summary>

```text
xvt verify JOB_FILE [--json]
```

- 位置参数：`job_file`。
- 可选参数：`--json`。本命令重新计算 checksum，但不修改工作流目标。

</details>

<details>
<summary><code>xvt relink-burned-output</code>：重新关联已移动的烧录成品</summary>

```text
xvt relink-burned-output JOB_FILE EXISTING_VIDEO [--events-jsonl]
```

- 位置参数：`job_file`、`existing_video`。
- 只接受 checksum 有效的 `version=2` burn proof：它绑定当前烧录 fingerprint、原 `burned_video` checksum 和实际源媒体内容 checksum。原成品须已通过媒体 QA，候选视频的 SHA-256 还必须与原 artifact checksum 完全相同，才会更新登记路径。
- 旧/legacy fingerprint 仍可按既有轻量完成语义读取，但不能用于重新关联；请先运行 `continue` 重烧一次以生成 v2 proof。重复登记的 `burned_video` 一律拒绝，不会猜测应关联哪一项。
- 普通 `status` 不会为 v2 proof 额外计算源媒体内容 checksum；该 checksum 只在烧录落证和重新关联校验时计算。
- 高级原子命令 `xvt burn` 允许使用临时媒体、字体和编码参数，因此不签发 workflow v2 proof；只有 `run` / `continue` 按持久化目标完成的烧录可供重新关联。
- 命令不复制、移动、删除或重新编码视频；失败时不修改 Job。`--events-jsonl` 输出 artifact 路径切换和 CLI 终态事件。

</details>

<details>
<summary><code>xvt relink-source-media</code>：源媒体搬家后重指持久化路径（advanced）</summary>

```text
xvt relink-source-media JOB_FILE NEW_MEDIA [--events-jsonl]
```

- 位置参数：`job_file`、`new_media`。
- 用途：**源媒体**（不是烧录成品）永久换了位置、内容没变时，把持久化的 `config.workflow.burn_media` 指到新路径。状态机本来就认内容：路径变了但内容 checksum 仍等于 v2 proof 登记那份时，烧录派生指纹照旧成立，因此命令执行完 Job 通常直接回到 `complete`，**零重烧**。此前达成同样效果只能手改 `job.json`。
- 只认内容 checksum：新文件的 SHA-256 必须等于 v2 proof 里的 `source_media_checksum`，否则以退出码 2 拒绝。没有 checksum 有效的 `version=2` burn proof 时同样拒绝——没有基线就证明不了是同一份媒体（legacy fingerprint 不给这条出口，与 `relink-burned-output` 同口径）。
- 只写 `config.workflow.burn_media` 一个字段：不重烧、不改 artifact 登记、不改 stage，失败时 Job 一个字节都不改。因为没有产物变动，`--events-jsonl` 只输出 CLI 终态事件（`cli_completed`，带 `media` 字段给出新路径），不发 artifact 事件。
- 存量 v2 proof（缺可选字段 `source_media_identity`，即该 Job 上一次烧录早于该字段引入）仍会被接受并写入新路径，但指纹回退拿不到授权，`status` 会要求重烧一次（重烧后新 proof 即补上该字段）。
- 远程 Job 不支持：媒体是 Job 内登记的下载产物，不是文件系统上的用户文件，「搬家」不适用；下载产物缺失时直接 `continue` 重新获取。
- `status` 已经接入这条命令：源媒体不在登记路径、且 Job 有 checksum 有效的 v2 proof 时，恢复计划的首选命令就是 `xvt relink-source-media JOB NEW_MEDIA_PATH`（新路径是占位符，替换成媒体现在的位置即可）；只有 legacy fingerprint 的旧 Job 不提示它（对它们这条命令会失败），仍只提示把文件原样放回原路径。

</details>

<details>
<summary><code>xvt doctor</code>：检查本机运行依赖</summary>

```text
xvt doctor [--output PATH] [--json]
```

- `--output`：额外检查目标输出位置。
- `--json`：输出机器可读诊断结果。

</details>

### 高级、修复与原子命令

<details>
<summary><code>xvt inspect</code>：检查本地媒体或远程 URL</summary>

```text
xvt inspect INPUT [--json]
```

- 位置参数：`input`。
- 可选参数：`--json`。

</details>

<details>
<summary><code>xvt resume</code>：只恢复转写 checkpoint</summary>

```text
xvt resume JOB_FILE [--events-jsonl]
```

- 位置参数：`job_file`。
- 可选参数：`--events-jsonl`。它不完成持久化的翻译或烧录目标；端到端 Job 通常应使用 `continue`。
- 远程 Job：优先复用 checksum 有效的已下载媒体（`source_video` / `downloaded_media`）；没有可复用媒体时委派远程恢复路径重新获取，因此**可能触发一次媒体下载**，并使用 Job 已持久化的 Cookie 配置。

</details>

<details>
<summary><code>xvt rebuild-source</code>：用 ASR 重建既有 Job 的源 cue</summary>

```text
xvt rebuild-source JOB_FILE --strategy asr
  [--cookies-from-browser BROWSER] [--proxy URL] [--events-jsonl]
```

- 位置参数：`job_file`；必填参数：`--strategy asr`。
- 远程访问：`--cookies-from-browser`、`--proxy`。
- 事件：`--events-jsonl`。

</details>

<details>
<summary><code>xvt prepare-translation</code>：生成确定性的 Agent 翻译批次</summary>

```text
xvt prepare-translation JOB_FILE --source-language LANG --target-language LANG
  [--translation-quality standard|polished] [--batch-size N]
  [--glossary PATH] [--translation-style STYLE] [--events-jsonl]
```

- 位置参数：`job_file`。
- 必填参数：`--source-language`、`--target-language`。
- 策略：`--translation-quality`、`--batch-size`、`--glossary`、`--translation-style`。
- 策略回退：在端到端 Job（有持久化 `config.workflow`）上不传 `--glossary` / `--translation-style` / `--batch-size` 时，逐字段回退到 Job 已固化的术语表、风格与批次大小，与 `continue` / `status` 看到同一套策略；显式传入的字段以显式值为准。没有 `config.workflow` 的原子 Job 仍取各自默认值（风格 `faithful`、批次 `25`）。

</details>

<details>
<summary><code>xvt apply-translation</code>：回填 Agent 翻译响应</summary>

```text
xvt apply-translation JOB_FILE RESPONSE_FILE --target-language LANG [--events-jsonl]
```

- 位置参数：`job_file`、`response_file`。
- 必填参数：`--target-language`。
- 可选参数：`--events-jsonl`。
- manifest 绑定：`prepare-translation` 生成的标准 manifest 记录 `source_cues_checksum`；源 cue 在 prepare 之后发生变化时，旧响应会被拒绝，需重新 `prepare-translation` 并翻译新 manifest。已登记的 manifest 若 checksum 失效、文件缺失或内容损坏，同样拒绝回填；从未登记过 manifest 的极旧 Job 以及修复前生成、没有该字段的旧 manifest 仍按原样接受。

</details>

<details>
<summary><code>xvt import-translation</code>：导入已有译文字幕</summary>

```text
xvt import-translation JOB_FILE SUBTITLE_FILE --target-language LANG [--events-jsonl]
```

- 位置参数：`job_file`、`subtitle_file`。
- 必填参数：`--target-language`。
- 可选参数：`--events-jsonl`。

</details>

<details>
<summary><code>xvt drop-imported-translation</code>：放弃导入译文，回到重新翻译（advanced）</summary>

```text
xvt drop-imported-translation JOB_FILE [--events-jsonl]
```

- 位置参数：`job_file`。
- 可选参数：`--events-jsonl`。
- 用途：Job 的译文来自 `--translation-subtitles` / `import-translation`，而你决定**不再使用这份译文**（例如 `rebuild-source --strategy asr` 换掉源时间轴之后它已经套不回去）时，用这条命令显式放弃它。此前只能跑原子 `xvt translate` 绕过去：那样能产出新译文，但错位的导入译文副本与持久化目标里的 `translation_subtitles` 原样留着，下一次 `rebuild-source` 会在同一处复发同一个死循环，Job 的持久化目标也仍写着「用导入译文」。
- 同时清两处来源，缺一不可：移除 `input_translation_subtitles` 的 artifact 登记，并清掉 `config.workflow.translation_subtitles`。只清一处时，工作流会从另一处把它捡回来。
- 带失效闭包：按 artifact 依赖图失效 `input_translation_subtitles` 的全部下游（`translation_mapping`、`translated_cues`、`translated_srt`、`bilingual_*`、整条润色与语义链路、`preview_*`、`burned_video`、`media_qa`、`burn_fingerprint` 等 21 个 kind），复位 `translate` / `subtitle_qa` / `render` / `burn` / `media_qa` 五个 stage 并清除烧录派生指纹。不带闭包的话译文仍然 checksum 有效，`continue` 会直接判已完成，产物依旧来自被放弃的那份译文。
- **保留文件，不删字节**：Job 内的副本（`artifacts/input-translation.srt`）与用户原始译文文件都留在原处，只是不再被 Job 登记或引用。被放弃的 kind、路径与内容 checksum 会写进 `artifact_invalidated` 与 `translation_import_dropped` 事件。
- **放弃不等于完成**：命令结束后译文需要重新产出，输出里直接给出下一步。`--provider auto` 且当前终端没有 API 配置时，随后的 `continue` 会停在 Agent 等待（`awaiting_input`），按提示回填即可。
- 原子 Job（没有 `config.workflow`）同样支持，那里只有 artifact 一个来源；之后用 `xvt translate` 重新翻译。放弃之后 `prepare-source-review` 与 `resegment-translation` 的「正在复用导入译文」闸门随之解除。
- 两处来源都不存在时以退出码 2 拒绝，不会静默成功。
- 覆盖保护的影响：清掉 `config.workflow.translation_subtitles` 之后，那份用户原始译文文件不再属于烧录/预览的覆盖保护集（Job 确实已经不再把它当输入）。仍需要保护时，不要把 `--output` 指向它。

</details>

<details>
<summary><code>xvt translate</code>：原子翻译入口</summary>

```text
xvt translate JOB_FILE --target-language LANG [翻译参数] [--events-jsonl]
```

- 位置参数：`job_file`；必填参数：`--target-language`。
- 翻译：`--translation-quality`、`--provider`、`--base-url`、`--model`、`--api-key-env`、`--batch-size`、`--max-retries`、`--timeout-seconds`、`--glossary`、`--translation-style`。
- 提供方身份：在端到端 Job（有持久化 `config.workflow`）上不传 `--base-url` / `--model` 时回退到 Job 已固化的值而不是环境变量，与 `continue` / `status` 看到同一套身份；显式传入则覆盖并在首个批次成功后写回 Job。API Key 仍只读环境变量。
- 策略回退：`--glossary` / `--translation-style` / `--batch-size` 同样在端到端 Job 上逐字段回退 Job 固化值（不回退会让译文丢掉术语表，而译文 QA 仍按 Job 策略判 `glossary_violation`；策略与批次大小都进翻译指纹，漂移会清零批次进度并重付一次翻译费）。原子 Job 取各自默认值。
- 事件：`--events-jsonl`。

</details>

<details>
<summary><code>xvt polish-translation</code>：为已有译文补跑可恢复润色</summary>

```text
xvt polish-translation JOB_FILE [--provider PROVIDER] [--base-url URL]
  [--model NAME] [--api-key-env NAME] [--batch-size N]
  [--max-retries N] [--timeout-seconds SECONDS] [--events-jsonl]
```

- 位置参数：`job_file`。
- 翻译提供方覆盖：`--provider`、`--base-url`、`--model`、`--api-key-env`、`--batch-size`、`--max-retries`、`--timeout-seconds`；不传时优先复用 Job 配置。
- 事件：`--events-jsonl`。
- 落到 Agent 等待时直接打印下一步命令（`apply-polish-translation` → `continue`），不必再跑一次 `status`。
- 不适用于 `task=translate_and_polish` 的 Agent `polished` Job（润色已在初译那一步完成），这类 Job 上以退出码 2 拒绝并说明原因。

</details>

<details>
<summary><code>xvt apply-polish-translation</code>：回填 Agent 润色响应</summary>

```text
xvt apply-polish-translation JOB_FILE RESPONSE_FILE [--events-jsonl]
```

- 位置参数：`job_file`、`response_file`。
- 可选参数：`--events-jsonl`。

</details>

<details>
<summary><code>xvt apply-semantic-review</code>：回填 Agent 语义校对与一次修复响应</summary>

```text
xvt apply-semantic-review JOB_FILE RESPONSE_FILE [--events-jsonl]
```

- 位置参数：`job_file`、`response_file`。
- 可选参数：`--events-jsonl`。

</details>

<details>
<summary><code>xvt apply-translation-review-fixes</code>：应用人工译文修订</summary>

```text
xvt apply-translation-review-fixes JOB_FILE RESPONSE_FILE [--check] [--events-jsonl]
```

- 位置参数：`job_file`、`response_file`；支持生成的 JSON 模板或固定命名的修订 SRT。
- 可选参数：`--check` 只预检不保存；`--events-jsonl` 输出纯 JSONL。

</details>

<details>
<summary><code>xvt prepare-source-review</code>：生成源 cue 修订模板</summary>

```text
xvt prepare-source-review JOB_FILE --cue ID [--cue ID ...] [--events-jsonl]
```

- 位置参数：`job_file`。
- 必填可重复参数：`--cue`。
- 可选参数：`--events-jsonl`。命令同时生成同 fingerprint 的 JSON 与 SRT；JSON 供 Agent/脚本编辑，SRT 供字幕工具编辑。

</details>

<details>
<summary><code>xvt apply-source-review-fixes</code>：应用人工源文修订</summary>

```text
xvt apply-source-review-fixes JOB_FILE RESPONSE_FILE [--events-jsonl]
```

- 位置参数：`job_file`、`response_file`；支持生成的 JSON 模板或固定命名的源文修订 SRT。
- 可选参数：`--events-jsonl`。修改源文后会重跑源字幕 QA，并失效旧翻译及全部下游产物；回填后先运行 `xvt status JOB_FILE`，再按恢复计划继续。

</details>

<details>
<summary><code>xvt acknowledge-translation-review</code>：单独确认当前语义复核报告</summary>

```text
xvt acknowledge-translation-review JOB_FILE [--events-jsonl]
```

- 位置参数：`job_file`。
- 可选参数：`--events-jsonl`。日常路径也可使用 `continue --acknowledge-translation-review`。

</details>

<details>
<summary><code>xvt resegment-translation</code>：重切超宽 ASR cue</summary>

```text
xvt resegment-translation JOB_FILE [--events-jsonl]
```

- 位置参数：`job_file`。
- 可选参数：`--events-jsonl`。只支持具有词级时间戳的 ASR cue；会失效旧翻译及下游。

</details>

<details>
<summary><code>xvt retranslate-cues</code>：局部重译指定 cue</summary>

```text
xvt retranslate-cues JOB_FILE --cue ID [--cue ID ...] --target-language LANG
  [--provider PROVIDER] [--base-url URL] [--model NAME]
  [--api-key-env NAME] [--max-retries N] [--timeout-seconds SECONDS]
  [--events-jsonl]
```

- 位置参数：`job_file`。
- 必填参数：可重复的 `--cue`、`--target-language`。
- 翻译提供方：`--provider`、`--base-url`、`--model`、`--api-key-env`、`--max-retries`、`--timeout-seconds`。
- 事件：`--events-jsonl`。
- Agent 模式下 Job 会进入等待：`status` 归为 `awaiting_input`，并给出带具体 cue ID 与目标语言的可复制 `apply-cue-retranslation` 命令；未回填前 `continue` 不会宣告完成，`batch-continue` 也会停在该条目上。

</details>

<details>
<summary><code>xvt apply-cue-retranslation</code>：回填局部重译结果</summary>

```text
xvt apply-cue-retranslation JOB_FILE RESPONSE_FILE --cue ID [--cue ID ...]
  --target-language LANG [--events-jsonl]
```

- 位置参数：`job_file`、`response_file`。
- 必填参数：可重复的 `--cue`、`--target-language`。
- 可选参数：`--events-jsonl`。

</details>

<details>
<summary><code>xvt preview</code>：生成短片段双语预览</summary>

```text
xvt preview JOB_FILE [--cue ID | --start SECONDS] [--duration SECONDS]
  [--media PATH] [--output PATH] [--software]
  [--target-font NAME] [--source-font NAME] [--events-jsonl]
```

- 位置参数：`job_file`。
- 范围：`--cue` 与 `--start` 互斥；`--duration` 默认 `15` 秒。
- 媒体与输出：`--media`、`--output`、`--software`、`--target-font`、`--source-font`。
- 事件：`--events-jsonl`。
- 前置条件：需要 checksum 有效的译文 cue 与译文 QA 报告，并通过与 `burn` 相同的 QA 门；`needs_review` 须已在端到端 Job 上确认。缺产物或 QA 未放行时以退出码 4（`workflow`）失败。

</details>

<details>
<summary><code>xvt burn</code>：原子烧录入口</summary>

```text
xvt burn JOB_FILE [--media PATH] [--output PATH] [--software]
  [--cookies-from-browser BROWSER] [--proxy URL]
  [--target-font NAME] [--source-font NAME] [--events-jsonl]
```

- 位置参数：`job_file`。
- 媒体与输出：`--media`、`--output`、`--software`、`--target-font`、`--source-font`。
- 远程访问：`--cookies-from-browser`、`--proxy`。
- 事件：`--events-jsonl`。
- 前置条件：需要 checksum 有效的译文 cue 与译文 QA 报告，并通过 fail-closed 的译文 QA 门；`needs_review` 须已在端到端 Job 上经 `continue --acknowledge-subtitle-review` 确认。QA 门在远程媒体下载之前执行。

</details>

<!-- cli-reference:end -->

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- FFmpeg，烧录时需包含 libass，建议同时具备 VideoToolbox 或 libx264
- yt-dlp，仅 YouTube 输入需要
- macOS Apple Silicon 使用 MLX Whisper 时安装 `asr-mlx` extra

安装开发环境：

```bash
uv sync --extra dev --extra asr-mlx
```

作为本地 CLI 安装：

```bash
uv tool install '.[asr-mlx]'
xvt doctor
```

检查运行时依赖：

```bash
uv run --no-sync xvt doctor
uv run --no-sync xvt doctor --json
```

`doctor` 会检查 FFmpeg、ffprobe、yt-dlp、libass、默认字幕字体的一帧实渲染、VideoToolbox、libx264 和已安装的 ASR 引擎。ffmpeg 能力探测（30 秒）与字体渲染检查（60 秒）都有超时上限，任一探测超时按该检查项失败处理，不会让命令无限期挂住；探测与其余外部命令走同一条执行路径，超时或 Ctrl-C 时整个进程组（含 ffmpeg 自己派生的子进程）都会被清理，不会留下后台进程继续占用 CPU。

## 快速开始

### 1. 单命令完成翻译与烧录

本地视频可以直接串联转写、翻译、双语字幕和烧录：

```bash
uv run --no-sync xvt run "/path/to/video.mp4" \
  --output "/absolute/path/to/output" \
  --language en \
  --target-language zh-CN \
  --provider auto \
  --glossary "/path/to/glossary.json" \
  --translation-style natural \
  --segmentation-preset balanced \
  --burn \
  --burn-output "/path/to/bilingual.mp4"
```

已有源字幕时增加 `--subtitles source.srt`，会跳过 ASR；同时已有译文字幕时再增加 `--translation-subtitles translated.srt`，会继续跳过翻译 API。`--provider auto` 在兼容 API 配置完整时自动翻译，否则生成 Agent 批次并将工作流置为 `awaiting_input`，不会误报全部完成。

YouTube 全流程示例：

```bash
uv run --no-sync xvt run "https://www.youtube.com/watch?v=VIDEO_ID" \
  --output "/absolute/path/to/output" \
  --youtube-subtitles \
  --subtitle-language en \
  --allow-auto-subtitles \
  --target-language zh-CN \
  --burn
```

请求烧录时，远程视频会在前置阶段下载一次；ASR 和后续烧录复用同一文件。

普通运行无需额外参数即可在终端看到阶段进度。阶段切换会立即显示，下载、faster-whisper 转写和烧录会显示百分比、速度或剩余时间，MLX Whisper 会显示已运行时长，翻译会显示当前批次。进度写入 stderr，最终 Job 和产物路径仍写入 stdout，现有脚本可以继续读取路径。

### 2. 只创建源字幕作业

```bash
uv run --no-sync xvt run "/path/to/video.mp4" \
  --output "/absolute/path/to/output" \
  --engine mlx \
  --language en \
  --events-jsonl
```

完成后，任务目录会包含 `job.json`、`artifacts/source-cues.json` 和 `artifacts/source.srt`。

`--segmentation-preset conservative` 使用更长的停顿和时长阈值，适合更完整的句子；默认 `balanced` 保持既有分段；`compact` 使用更短的阈值，适合需要更频繁换行的短视频。三个预设都遵守中日文的行首行尾禁则（禁則処理）：cue 不会以闭合括号、句读、小字假名、长音符或叠字记号开头，也不会以起始括号结尾。禁则只管**排版**，不保证切在词的边界上——日文逐字转写下仍可能切在一个词的中间。预设仅作用于 ASR 分段：直接导入源字幕会保留原时间轴；`--align-subtitles` 跳过分段预设，并仅在用户明确请求时按 ASR 对齐修正时间轴。

已有源字幕时可跳过 ASR，并保留其时间轴：

```bash
uv run --no-sync xvt run "/path/to/video.mp4" \
  --output "/absolute/path/to/output" \
  --subtitles "/path/to/source.srt" \
  --language en
```

只有确认字幕存在整体偏移或漂移时，才使用 `--align-subtitles`。该模式会额外运行 Whisper 并写入 `artifacts/alignment.json` 与 `artifacts/aligned-subtitle-qa.json`（对齐后字幕 QA 报告，两份都是 checksum 登记的产物）。对齐后 QA 出 error 级问题时强制对齐会失败，那份报告逐条列出是哪些 cue 的哪些 issue，不必重跑一遍去猜；`--allow-low-confidence-alignment` 不放行这类问题。

强制对齐是 fail-closed 的：对齐结果置信度低于阈值时失败（可用 `--allow-low-confidence-alignment` 显式放行），对齐后的字幕 QA 出现 error 级问题时同样失败，且 `--allow-low-confidence-alignment` 不放行这一类问题。后一种失败请读 `aligned-subtitle-qa.json`：它在算出之后、任何判定之前就落盘登记，所以**失败的 Job 上也一定有**，逐条给出是哪个 cue 的哪个 issue 码。此时去掉 `--align-subtitles` 会**保留源字幕原有时间轴**（不是回退到 ASR 分段）；确实需要按语音重建时间轴时，请改用 `--source-strategy asr`（不能与 `--align-subtitles` 同用），或先修复源字幕本身。

### 3. 纯字幕文件工作流

源字幕可以直接作为输入创建 Job，无需提供视频或音频：

```bash
uv run --no-sync xvt run "/path/to/source.srt" \
  --output "/absolute/path/to/output" \
  --language en \
  --target-language zh-CN \
  --provider auto
```

支持 SRT、VTT、ASS 和 SSA。该模式保留用户时间轴，输出 `source.srt`、`translated.srt` 和 `bilingual.srt`，并照常支持 Agent 翻译、`continue`、`verify` 和术语表 QA。由于没有媒体尺寸，不生成 ASS，也不支持 `--align-subtitles`、烧录、预览或 `resegment-translation`。

### 4. 目录批处理

目录中的本地视频、音频和字幕文件可以按稳定顺序串行处理，每个输入创建独立 Job：

```bash
uv run --no-sync xvt batch "/path/to/input-directory" \
  --output "/absolute/path/to/output" \
  --target-language zh-CN \
  --provider auto \
  --burn
```

默认只扫描目录第一层；使用 `--recursive` 才递归扫描。批处理支持常见视频、音频及 SRT、VTT、ASS、SSA，忽略隐藏文件和输出目录。使用 `--burn` 时目录只能包含视频；音频和纯字幕输入应拆成不烧录的独立批次。单项失败不会阻断后续文件，执行状态会原子写入 `batches/<batch-id>/batch.json`，其中包含每个输入的 Job 文件、产物、错误和恢复计划。

批次刚创建、首个条目尚未开始时收到中断，也会把 `batch.json` 写成 `interrupted` 并留下 `next_action`。中断、部分失败或 Agent 等待后，继续原批次：

```bash
uv run --no-sync xvt batch-continue "/path/to/batch.json"
```

`batch-continue` 会校验已有 Job，只跳过**目标产物**和 checksum 均有效的已完成项；可重试项调用原有 `continue` 工作流，需要人工修复或 Agent 回填的项保留恢复指令。已完成条目上只有中间 checkpoint（`normalized_audio`、`word_transcript`、批次 manifest 等）失效时，条目保持 `completed` 并带上 `verification: {"status": "degraded", "invalid_artifacts": [...]}` 标注——这类文件对成品毫无价值，清理磁盘删掉它们不应该让一个已经产出成品的条目变成永远走不出去的失败（旧行为是 `failed` + 退出码 6，且引导给的 `xvt verify` 只报告不修复，再跑一次仍是 6）。交付物失效不走这条放行：烧录成品缺失仍停在 `awaiting_input` 的 relink 引导；`bilingual.ass` 已在「译文目标是否满足」的判定内，缺它的条目根本不会被判成已完成，而是落到 `continue` 由 `render` 从现有译文 cue 零 API 成本重新渲染；`source.srt` 缺失会落到 `continue` 由 `resume` 真正重建。普通模式下汇总行之后会逐条打印需要处理的条目及其恢复命令，不必为了看到 relink 一类的出口改用 JSONL。批处理当前串行执行，不监控目录变化，也不提供队列或后台并发。

### 5. 单独翻译字幕

配置任意兼容 Chat Completions 的服务：

```bash
export XVT_TRANSLATION_BASE_URL="https://provider.example.com/v1"
export XVT_TRANSLATION_MODEL="model-name"
export XVT_TRANSLATION_API_KEY="secret"

uv run --no-sync xvt translate "/path/to/job.json" \
  --target-language zh-CN \
  --glossary "/path/to/glossary.json" \
  --translation-style natural
```

DeepSeek 示例：

```bash
export XVT_TRANSLATION_BASE_URL="https://api.deepseek.com"
export XVT_TRANSLATION_MODEL="deepseek-v4-flash"
export XVT_TRANSLATION_API_KEY="secret"
```

`--provider auto` 仅在 Base URL、模型和 API Key 都存在时调用 API。配置不完整时会安全降级为 Agent 模式，生成 `artifacts/translation-batches.json`。显式传入 `--provider openai-compatible` 时，缺失配置会直接失败。Job 已固化 Base URL 与模型、当前终端只缺 API Key 时，降级原因会点名 Job 钉住的提供方身份与缺失的环境变量（例如 `this job is pinned to openai-compatible (model=…, base_url=…) but the current shell lacks $XVT_TRANSLATION_API_KEY`），不会静默换成 Agent 批次。

Base URL 带用户名密码（`https://user:pw@host/v1`）或 query 串时，只要这条 Base URL 真的会被用来发请求（即 Base URL、模型与 API Key 都齐备），`auto` 与 `openai-compatible` 都会直接报错。报错只点名主机名，不回显凭据。配置不齐全时 `auto` 仍按既有契约降级成 Agent 模式——那条 Base URL 既不发请求也不落盘。原因：这类地址在发请求时抛的是 `nonnumeric port: 'pw@host'`，不带协议前缀，脱敏规则够不着它，而 Base URL 还会随 `job.config.workflow` 落盘。

API Key 只从环境变量读取，不会写入命令行、事件或 `job.json`。stage 失败原因在写进 `job.json` 之前统一脱敏，`xvt status --json` 输出的失败消息与 JSONL 事件里的是同一份文本。翻译按批次保存到 `artifacts/translation-progress.json`，只有输入、模型、端点和提示词版本完全一致时才复用进度。429 会遵循 `Retry-After`，网络错误、5xx、分块响应中途断开和 TLS 连接中途终止会退避重试；证书校验失败等信任链问题立即失败。

端到端 Job 的 Base URL 与模型在**首个翻译批次成功后**（或全部批次都命中 checkpoint 时）写回 `job.config.workflow`，此后 `continue` 与 `status` 都按 Job 内的值解析提供方与进度指纹，不再受 `XVT_TRANSLATION_BASE_URL` / `XVT_TRANSLATION_MODEL` 在另一个终端里的漂移影响；首批就失败时不写回，环境变量里的过期模型名不会先被钉进 Job（`continue` 没有 `--model`，写进去就没有入口再改）。API Key 永不落盘。要换模型：重新 `run`，或对已有 Job 执行 `xvt translate JOB --target-language LANG --model NEW`（显式值覆盖并写回），或直接编辑 `job.json` 里的 `config.workflow.base_url` / `model`。

提供方返回 HTTP 错误时，错误消息为 `translation provider returned HTTP <code> [x-request-id=…] (model=…, base_url=…): <响应体>`：追踪 ID（`x-request-id` 或 `x-ds-trace-id`）只在响应头带有时出现；生效的模型与端点只在不可重试的状态码（4xx 配置类错误）上附带，可重试的 429 / 5xx 不附带；响应体折成一行、剥离控制字符、截断到 500 字符，并经过与事件同一套脱敏。

兼容 API 默认每批最多翻译 25 个 cue，请求超时时间为 180 秒；可用 `--batch-size` 和 `--timeout-seconds` 针对单个新任务调整。已创建的 Job 继续使用其持久化配置。

`translate` 和 `prepare-translation` 支持结构化术语表及 `faithful`（默认）、`natural`、`concise` 三种风格。Q4 不改变默认风格：新 Job 仍使用 `faithful`，除非显式传入 `--translation-style`。术语表是 JSON 文件；命中当前批次源 cue 的术语才会写入 API 请求或 Agent 批次，避免无关提示膨胀：

```json
{
  "terms": [
    {"source": "OpenAI", "target": "OpenAI"},
    {"source": "Whisper", "target": "Whisper"}
  ]
}
```

`translate`、`prepare-translation` 和端到端 `run` 都支持该策略。`--glossary` 与 `--translation-subtitles` 互斥：导入译文时 XVT 没有按术语表翻译过，把用户自写译文里的术语偏差判成 error 级 `glossary_violation` 只会造成无出口的死锁，因此该组合在 `run` 入口就以退出码 2 拒绝，Job 不会创建。确实需要按术语表校对导入的译文时，先 `import-translation`，再用 `retranslate-cues` 逐条重译需要修正的 cue（该命令复用 Job 保存的策略快照）。`--translation-style` 不受此限制：导入译文的 Job 同样保存风格快照，风格不影响导入本身，但会对后续 `retranslate-cues` / `polish-translation` 生效。端到端 Job 会保存解析后的策略快照而不是术语表路径，因此 `continue` 即使在原术语表文件移动、修改或删除后，仍使用首次运行时的相同术语和风格。策略也会进入 API 翻译指纹；修改风格或术语表后不会复用旧的批次进度。译文 QA 会确定性检查命中源 cue 的术语是否包含要求的目标词；违规会以 `glossary_violation` 写入 `qa-translated.json`，阻断渲染和烧录。阻断的 error 级问题**全部**是 `glossary_violation` 时，`status` 会直接给出 `xvt retranslate-cues JOB --cue <违规 cue> --target-language LANG`——`glossary_violation` 是 error 级，`--acknowledge-subtitle-review` 只覆盖 review 级，没有这条出口该状态就是死路。它复用 Job 保存的策略快照，因此重译会按同一份术语表进行；`standard` Job 上只改写指定 cue，其余译文与全部时间轴保持不变，`polished` Job 上回填结果会成为新的翻译草稿并重启润色与语义校对（真实模型成本）。若判断是术语表本身需要调整，另一条路是改术语表后重跑翻译。报告里混有其他 error（时间轴、缺译文、显示宽度等）时不给该出口，仍需先修字幕。

Q4 在翻译前按已有 cue 确定性建立语义组：句末标点、相邻 cue 间隔达到 650ms、20 个 cue 或 30 秒会形成边界；达到上限时优先在句末、软标点、连接词前或最大停顿处分界。provider 批次默认最多容纳 25 个 cue，但只以完整语义组装箱，不会从组中切开；可通过 `--batch-size` 调整。批次中的 `semantic_groups` 仅记录各组的 cue ID，时间轴、cue 数量和顺序始终不变。

新 Job 可显式选择翻译质量，默认仍为 `standard`：

```bash
uv run --no-sync xvt run INPUT --output OUTPUT \
  --target-language zh-CN \
  --translation-quality polished
```

`run`、`batch`、`translate` 和 `prepare-translation` 都支持 `standard|polished`。API 模式的 `polished` 先保存完整翻译草稿，再逐批润色；Agent 模式生成带 `task=translate_and_polish` 和 fingerprint 的单个 `translation-batches.json`。润色候选随后必须经过 Q6 语义校对；API 对可修复问题按完整语义组最多修复一次并复查，Agent 生成 `task=semantic_review_and_repair` manifest，使用 `apply-semantic-review JOB RESPONSE` 回填。旧 Job 或旧 batch 缺质量字段时继续按 `standard`。`--translation-subtitles` 不能与新 Job 的 `polished` 同时使用，应先导入译文再执行 `polish-translation`。

Q6.1c 要求 API 和 Agent 都把每个语义组作为连续话语处理：先理解完整组，再在不改变 cue ID 和时间轴的前提下生成自然、连贯的目标语言文本。翻译和润色优先使用直接、口语化的字幕表达，避免模型无依据添加破折号、公式化对比、空泛连接词或宣传式措辞；原文确有的对比、强调、列举、打断、语气和信息必须保留。专名、产品名和代码标识符没有术语表证据时不得猜测修正；语义校对只有在上下文矛盾、明显误听或可识别专名异常时才报告 `source_suspect`，源文修改继续由人工修订入口完成。

模型生成的显式简中译文（`zh-CN`、`zh-Hans` 及 `zh-Hans-*` 区域后缀形式）写入时还会经过保守的确定性标点收口：消除全角 `，`、`。`，其中句中位置按需替换为一个半角空格以保留 `OpenAI GPT-5` 一类词边界，行尾与换行前直接移除；模型把省略号写成的连续全角句号归一为单个 `…`；`？`、`！`、`；`、`：`、`——`、ASCII URL/版本号/代码标点及模型原有换行一律保留。左右同形引号按上下文推断开闭，但英文词内撇号和数字后的尺寸符号不参与引号计数。源 cue 末尾为问句而译文没有句末语义标点时补 `？`，位置镜像源文中问号相对末尾闭合符号的位置：源文 `(ready?)` 得到 `（准备好了吗？）`，源文 `Is it (a)?` 得到 `是吗（a）？`；译文闭合符号少于源文时按译文实际数量收敛。语义审查把半角空格和 cue 边界视为有效分隔，不会仅因缺少全角逗号或句号报告 `awkward_join`。导入译文、人工译文修订、源字幕、bare `zh`、繁中及其他目标语言保持原样。

语义复查或候选译文确定性 QA 仍有可修复问题时，XVT 会生成绑定当前候选、语义报告和 `translation_candidate_qa` 的 JSON/SRT 修订模板。两种载体都必须完整保留模板 cue ID 集合，并可用 `xvt apply JOB REVIEW --continue` 一步回填并继续。系统记录实际变化的 `changed_ids`，只重新检查包含这些 ID 的完整语义组，相邻组仅作只读上下文，未触及问题保持原结论；不会重新翻译、润色或再次自动修复，候选 QA 会完整重跑。存在 `source_suspect` 时还会生成源文模板；源文修改会失效译文及下游，因此不支持 `apply --continue`，`status` 会明确提示高成本路径与备选方案。详见 [Q6.1 方案](docs/Q6_1_HUMAN_REVISION_PLAN.md)。

源字幕存在误听或专名错误时，可显式生成源文修订模板。该路径只修改指定 cue 的源文字段，立即重新执行确定性源字幕 QA，并使旧翻译、润色、语义报告、渲染和烧录结果失效；ID、时间轴、词级时间戳和 provenance 保持不变：

```bash
uv run --no-sync xvt prepare-source-review JOB --cue 13 --cue 14
# 编辑命令输出的 source-review-*.json 中 items[].text；或用字幕工具编辑同名 .srt
uv run --no-sync xvt apply-source-review-fixes JOB REVIEW.json
# SRT 通道等价：保留 #<cue id> 注释、时间轴和文件名，只改源文
uv run --no-sync xvt apply-source-review-fixes JOB source-review-<fp12>.srt
# 源文变化会使旧译文失效，先确认新的恢复计划
uv run --no-sync xvt status JOB
# 按 status 输出的 next / before 命令继续
```

两种载体都绑定相同 source checksum 和精确 cue ID 集合，并汇聚到同一回填流程；改时间轴、增删 cue、注释与时间轴不符或修改 fingerprint 文件名都会被拒绝。源文修订与 `--translation-subtitles` 导入译文工作流不能组合，因为旧译文不再可信；准备和回填命令都会提前拒绝，`status` 也不会展示不可执行的源文路径。QA 的 `failed` 或 `needs_review` 仍会阻断翻译，修订命令不会绕过门禁；纯 `needs_review` 持久化为 `awaiting_input` 并返回确认/修订命令，只有 error 级问题记为 `failed`。

已有端到端 Job 具备 checksum 有效的 `translated_cues` 时，可显式补跑 Q5a 润色（**Agent 模式的 `polished` Job 除外**，见本节末尾）：

```bash
uv run --no-sync xvt polish-translation "/path/to/job.json"
```

命令先保存不可变草稿，再将工作流质量目标设为 `polished` 并失效旧译文下游。API 模式逐批写入 `translation-polish-progress.json`，中断后执行 `xvt continue JOB` 只续跑未完成批次；配置不完整的 `auto` 模式生成 `translation-polish-batches.json`，完成 Agent 响应后执行：

```bash
uv run --no-sync xvt apply-polish-translation "/path/to/job.json" RESPONSE.json
uv run --no-sync xvt continue "/path/to/job.json"
```

润色响应必须回传当前 `translation-polish-batches.json` 的 `fingerprint`、精确覆盖全部请求 ID，并且只能替换译文文本；旧响应不能用于新的 draft。`continue` 随后进入语义校对。如果语义报告仍有问题，`status` 会优先给出可编辑模板与回填命令：

```bash
uv run --no-sync xvt apply "/path/to/job.json" "/path/to/review.json" --continue
# 或用字幕工具编辑 review/ 下同名的 .srt 后回填（两条通道等价）：
uv run --no-sync xvt apply "/path/to/job.json" "/path/to/translation-review-<fp12>.srt" --continue
# 或在确认当前问题可接受时：
uv run --no-sync xvt continue "/path/to/job.json" --acknowledge-translation-review
```

`review/translation-review-<fp12>.json` 的字段如下。**回填只读取 `items[].id` 与 `items[].text`**，其余字段都是帮你判断「哪条必改、要缩到多少、原文是什么」的只读标注，改了不会生效，也不影响 fingerprint 校验；顶层的 `task` / `revision_fingerprint` / `scope` 必须原样保留：

| 字段 | 说明 |
| --- | --- |
| `task` / `scope` / `revision_fingerprint` / `report` / `qa_report` | 模板身份与来源报告路径；`revision_fingerprint` 绑定当前候选与报告，内容变化后旧模板失效 |
| `issues` / `qa_issues` | 本次触发修订的语义问题与候选译文 QA 阻断项全集（只读） |
| `items[].id` | cue ID，回填时必须完整保留模板的 ID 集合，且顺序为 ID 升序 |
| `items[].source_text` | 该 cue 的源文（只读标注） |
| `items[].must_fix` | 该 cue 是否被候选译文 QA 阻断；`true` 表示必须改到 QA 通过才能继续（只读标注） |
| `items[].qa` | 该 cue 的阻断项列表，每项含 `code`、`message`；行宽超限时额外给出 `display_width`、`limit`、`width_unit`，单位为 `ASS pixels` 或回退时的 `display units`（只读标注） |
| `items[].text` | 当前译文，**唯一会被回填读取的可编辑字段** |

同名的 `.srt` 通道只含 `#<cue id>` 注释行与译文正文，不带任何标注——那些注释行是解析锚点，不能改动。需要看标注时对照 JSON 模板即可，两条通道等价。

确认绑定当前候选、完整语义处理 artifact 链和报告 checksum；内容变化后自动失效。初次报告包含可修复问题时不能提前确认，修复完成后仍会保留未改动源文对应的 `source_suspect`。确定性 QA 失败仍会阻断渲染与烧录。Q5a 的显式补跑仍只支持带工作流配置的端到端 Job；原子 `translate --translation-quality polished` 通过重跑同一命令恢复 API draft、润色和语义校对 checkpoint。

`translation-draft-cues.json` 是恢复润色的必要检查点。它缺失或 checksum 失效时，`status` 会要求人工恢复原始译文草稿，不会把已润色的最终译文再次复制为 draft。

**Agent 模式的 `polished` Job 不在上面两段的适用范围内**：`task=translate_and_polish` 的 manifest 把初译与润色合在同一次回填里完成，这类 Job 从来不写 `translation-draft-cues.json`，因此既不能用 `polish-translation` 补跑 Q5a 润色，也不存在「草稿失效需要恢复」这件事。在这类 Job 上执行 `polish-translation` 会以退出码 2 直接拒绝，并说明润色是在哪一步完成的；要改译文请走 `retranslate-cues`（逐条重译）或重跑一轮完整翻译。`polish-translation` 仍适用于 API 模式的 `polished` Job，以及先 `standard` 翻译、之后才决定补润色的端到端 Job。

对 QA 报告中定位出的单个或多个 cue，可使用 `retranslate-cues`。它只发送指定 cue，带相邻源 cue 作为只读上下文，复用 Job 已保存的术语策略（端到端 Job 以 workflow 的策略快照为准，与译文 QA 的术语校验同源；原子 Job 读顶层配置）；回填后保留其他译文与全部时间轴，重新执行完整译文 QA，并使旧 ASS、烧录视频和媒体 QA 失效：

```bash
uv run --no-sync xvt retranslate-cues "/path/to/job.json" \
  --cue 18 --cue 24 \
  --target-language zh-CN
```

Agent 模式会生成绑定当前源文、译文、策略和提示词版本的 `cue-retranslation-batches.json`；响应必须复制其中的 fingerprint，并且只回填这些 cue ID。旧响应或输入变化前生成的响应会被拒绝：

```bash
uv run --no-sync xvt apply-cue-retranslation "/path/to/job.json" \
  "/path/to/cue-retranslation-response.json" \
  --cue 18 --cue 24 \
  --target-language zh-CN
```

Agent 模式请求重译后，Job 一直停在等待态直到回填：`status` 报 `awaiting_input` 并列出 manifest 路径与拼好的回填命令，`continue` 不会宣告完成、也不会继续渲染或烧录，`batch-continue` 把该条目记为 `awaiting_input` 而不是已完成。回填失败（fingerprint 对不上、`--cue` 或 `--target-language` 写错、顺序不一致等）不会让这次等待作废：这些校验全是只读的，`TRANSLATE` 保持 `awaiting_input`，盘上的 manifest 原样保留，改对参数再回填一次即可推进。`TRANSLATE` 因别的原因落到 `failed` 时（例如 API 模式的 `retranslate-cues` 已经写出 manifest、随后调用提供方失败，或旧版本 xvt 留下的 Job）这次等待同样挂得住：只要 manifest 与当前源文、译文仍然对得上，`status` 与 `continue` 就继续提示回填，不会改口报完成，也不会重复给出你刚跑过的 `retranslate-cues`——那份 manifest 可以直接拿去走 Agent 回填。要放弃一次重译，重新运行一轮完整翻译（`xvt translate JOB --target-language LANG`）即可取代它：准备新批次时会把这份重译 manifest 移出登记，Job 转为等待新的整轮回填（新的整轮请求登记更晚，因此优先于这次重译等待，也优先于上一次译文 QA 留下的阻断）。`xvt continue` 本身会停在这次重译等待上，不会替你放弃；仍然可以手工删除 `artifacts/cue-retranslation-batches.json`，删除后 `status` 恢复原有分类。

拼回填命令时 `--cue` 的顺序必须与 manifest 里的批次顺序一致（也就是当初 `retranslate-cues` 的 `--cue` 顺序）：`apply-cue-retranslation` 会用传入的顺序重新构造 manifest 并做全等比对，顺序不同会被拒绝。`status` 给出的命令已经按 manifest 顺序拼好，直接复制即可。

回填的实际代价按质量档不同：`standard` Job 上只有选中的 cue 被改写，其余译文与全部时间轴保持不变，随后重跑确定性译文 QA 与渲染；`polished` Job 上回填结果会成为**新的翻译草稿**（`translation-draft-cues.json`），旧的润色候选、语义校对结论与最终译文一并失效，需要重新走一遍润色与语义校对——这条腿会真实调用模型，不是零成本。

若端到端 Job 请求烧录，QA 通过并生成字幕后再执行 `xvt continue JOB`。局部修复不支持改变目标语言，也不接受重复或不存在的 cue ID。

不使用 API 时，可走 Agent 协议：

```bash
uv run --no-sync xvt prepare-translation "/path/to/job.json" \
  --source-language en \
  --target-language zh-CN \
  --glossary "/path/to/glossary.json" \
  --translation-style natural

uv run --no-sync xvt apply-translation "/path/to/job.json" \
  "/path/to/translation-response.json" \
  --target-language zh-CN
```

响应格式见 [翻译协议](skills/video-translate/references/translation-protocol.md)。

manifest 与源 cue 绑定：`prepare-translation` 之后若源 cue 发生变化（例如执行了源文修订或 `rebuild-source`），旧响应会被拒绝，必须重新 `prepare-translation` 再翻译新 manifest。已登记的 manifest 文件被改动或损坏（checksum 失效）时同样拒绝回填。

响应写错不会毁掉这次等待：`apply-translation`（统一入口 `xvt apply` 同理）与 `import-translation` 的读取、结构校验、ID 覆盖比对与字幕映射全是只读动作，都在触碰 Job 状态之前完成——文件读不出、不是 UTF-8、ID 覆盖不全、源 cue 已变或字幕对不上时命令以退出码 2 失败，但 `TRANSLATE` 原地保持 `awaiting_input`，`status` / `continue` / `batch-continue` 继续提示回填，换对文件再回填一次即可推进，不需要（也不该）重新跑一轮翻译。局部重译的 `apply-cue-retranslation` 同此约定。

如果已有机翻字幕，可直接导入：

```bash
uv run --no-sync xvt import-translation "/path/to/job.json" \
  "/path/to/machine-translated.srt" \
  --target-language zh-CN
```

源字幕时间轴始终优先。数量不一致的译文只有在时间覆盖完整时才能合并。

### 6. 只使用 YouTube 字幕

```bash
uv run --no-sync xvt run "https://www.youtube.com/watch?v=VIDEO_ID" \
  --output "/absolute/path/to/output" \
  --youtube-subtitles \
  --subtitle-language en \
  --allow-auto-subtitles
```

XVT 仅在传入 `--youtube-subtitles` 时请求平台候选字幕，且只有传入 `--allow-auto-subtitles` 才会请求自动字幕。新 Job 默认 `--source-strategy auto`：人工候选 QA 通过直接采用，失败时先尝试对齐再回退 ASR；自动候选在翻译或烧录任务中最终使用 ASR，在只生成源字幕且 QA 通过时可直接采用。`subtitles` 强制候选为时间轴，`asr` 强制媒体转写（不能用于纯字幕输入，也不能与 `--align-subtitles` 同用）。候选 QA 与最终选择会写入 `source-candidate-qa.json`、`source-selection.json`。需要登录态或代理时，必须由用户显式传入 `--cookies-from-browser` 或 `--proxy`。

显式请求 YouTube 平台字幕并传入 `--cookies-from-browser` 时，若登录态查询未找到匹配的人工字幕，且没有有效的已下载字幕或用户源字幕，XVT 会补做一次匿名查询。找到同一视频的匹配人工字幕后，字幕也匿名下载；该路径忽略 yt-dlp 本机配置以避免重新带入 Cookie，但保留显式代理，媒体下载仍使用原访问参数。补查失败会报错，不把提取失败当成字幕不存在。匿名来源记录在 `remote.json` 的 `subtitles_anonymous` 与 `remote_subtitles_selected` 事件的 `anonymous` 字段中。此行为同样适用于 `polished`；已有候选保留原来源分类，已生成源 cue 的 Job 不会自动更换源字幕。

### YouTube 视频下载画质

完整 YouTube 视频下载显式使用 `bv*+ba/b`、`lang,res` 排序，不设 1080p 上限。传入浏览器 Cookie 时比较原访问方式与忽略本机配置的匿名方式，保留显式代理；优先原音轨，再比较短边分辨率，同等条件保留原访问方式。固定获选格式 ID 下载后，以 ffprobe 核对尺寸、音视频流和时长；格式提取不完整或比较失败时停止，不悄悄改选低清。匿名查询失败也会保守停止，受限视频可使用本地媒体入口。

成功下载可生成 `source.download.json` 与可选 `media_download_selection` artifact，记录尺寸、访问方式、音轨身份与 checksum，不保存 Cookie 或签名 URL。该摘要不决定媒体有效性。升级后旧 Job 仍复用 checksum 有效的低清媒体，不自动重下载；已有译文需要高清重烧时使用 `xvt burn JOB --media /path/to/HD.mp4 --output /path/to/output.mp4`。

### 7. 重建既有 Job 的源字幕

当媒体 Job 的源字幕 QA 出现 error 时，可显式用 ASR 重建时间轴：

```bash
uv run --no-sync xvt rebuild-source "/path/to/job.json" \
  --strategy asr \
  --cookies-from-browser "firefox" \
  --proxy "http://127.0.0.1:7890"
```

这是 advanced 原子命令，`continue` 不会自动执行。它优先复用校验通过的词级转写、规范化音频和本地或已下载媒体；只有远程 Job 缺少这些可复用内容时，才按显式 Cookie/代理重新获取。命令持久化 ASR 策略与修复意图并关闭对齐，失效旧候选 QA、source cue、选择、重分段及全部下游产物，但保留 Job 内 checksum 有效的输入译文副本，以便原始外部译文文件已移动时仍可按新时间轴重新映射。**保留不等于还能用**：新的源时间轴可能与那份译文完全对不上，命令收尾会当场判定并说明——对不上时它不再提示 `xvt continue`（那条命令必然失败），而是给出 `xvt import-translation JOB <对齐新时间轴的译文> --target-language ...`，`xvt status` 同步归为需要人工处理并给出同一条出口。修复中断后 `status` 会继续给出显式重试命令；成功后端到端 Job 运行 `xvt continue JOB`，旧原子 Job 运行 `xvt translate JOB --target-language ...`。`status` 仅在非 ASR 源字幕 QA 为 error 且可重建时首次建议此命令；ASR 自身 QA error、`needs_review` 和译文 QA 不会重复建议重建。

### 8. 生成短片段预览

在烧录整段视频前，可以围绕指定 cue 快速检查字体、位置、换行和同步：

```bash
uv run --no-sync xvt preview "/path/to/job.json" \
  --cue 18 \
  --duration 15
```

也可以显式指定起点和输出路径：

```bash
uv run --no-sync xvt preview "/path/to/job.json" \
  --start 90 \
  --duration 15 \
  --output "/absolute/path/to/preview.mp4"
```

不传 `--cue` 或 `--start` 时，默认从第一条有效译文 cue 附近生成 15 秒预览。预览使用与正式烧录相同的字体、分辨率自适应码率和硬件回退逻辑，但不会修改正式 `burn`、`media_qa` stage，也不会覆盖 `bilingual.ass` 或 `burned.mp4`。本地纯音频 Job 以及没有有效 `source_video` 的远程 Job 必须通过 `--media` 提供视频。

预览与烧录共用同一道 fail-closed 译文 QA 门（见下文“译文 QA 门”）。缺少 checksum 有效的译文 cue 或译文 QA 报告时，命令以退出码 4（`workflow` 类别）失败——这是 Job 状态问题，不是参数错误。

### 9. 单独烧录双语字幕

```bash
uv run --no-sync xvt burn "/path/to/job.json" \
  --media "/path/to/video.mp4" \
  --output "/path/to/bilingual.mp4"
```

默认优先 VideoToolbox，仅在硬件编码器不可用或繁忙时回退 libx264。使用 `--software` 可直接指定软件编码。远程作业未传 `--media` 时会下载视频流；本地音频作业则必须通过 `--media` 提供视频。

烧录和预览把 `bilingual.ass` / `preview.ass` 作为 `-vf ass=filename=...` 传给 FFmpeg，字幕路径按 filtergraph 的两层规则转义，因此 Job 目录（由 `--output` 决定）路径中的单引号、逗号、分号、方括号和冒号都不影响烧录，例如 `--output "/Users/me/Tom's Videos"` 可正常工作。

#### 译文 QA 门

原子 `xvt burn` 和 `xvt preview` 与端到端链路共用同一道 fail-closed 的译文字幕 QA 门，不能用来绕过 QA：

- 必须同时存在 checksum 有效的译文 cue 和译文 QA 报告；任一缺失或 checksum 失效都会被拒绝。
- 报告为 `pass` / `warning` 时放行；出现 error 级问题或报告结构异常一律拒绝。
- 报告为 `needs_review` 时，只有报告内**全部** review 级问题都已被仍然有效的确认记录覆盖才放行。确认记录同时绑定当前源 cue 与**它被给出时你看到的那份 QA 报告**：源字幕或译文任一变化，报告就变了，旧确认自动失效，需要重新查看报告再确认（不会出现「为旧译文确认过，新译文恰好同样超宽就被自动放行」）。确认通道只有 `xvt continue JOB --acknowledge-subtitle-review`，因此确认必须在 `xvt run` 创建的端到端 Job 上完成。
- 原子 Job（单独 `xvt translate` 创建、没有持久化 `config.workflow`）上 `xvt continue` 不可用，报错会引导重跑 `xvt translate JOB --target-language LANG` 重新生成译文与 QA 报告。
- `burn` 的 QA 门在远程媒体下载之前执行：注定被拒的 Job 不会先下载整段视频。
- 端到端 Job 上，QA 报告单独缺失或 checksum 失效（译文 cue 与字幕文件都仍然有效）时，`xvt status` 归为 `retryable`，`xvt continue` 会从现有译文 cue 重算 QA 并重新写出字幕渲染产物，**不会重新翻译**（零翻译 API 调用）。重算内容与旧产物一致时 checksum 不变，也不会触发重新烧录。`needs_review` 的既有确认记录仍然有效，重算后依旧放行；重算结果为 `failed` 或出现新的 review 级问题时按常规 QA 阻断处理（`failed` 以退出码 4 失败）。

## 输出与质量控制

典型产物位于 `jobs/<job-id>/artifacts/`：

| 文件 | 用途 |
| --- | --- |
| `source.srt` | 原文字幕 |
| `translation-batches.json` | Agent 翻译批次 |
| `translation-progress.json` | API 翻译断点进度 |
| `translation-draft-cues.json` | 润色前的不可变译文草稿 |
| `translation-polish-batches.json` | Agent 润色批次 |
| `translation-polish-progress.json` | API/Agent 润色恢复进度 |
| `translation-polished-cues.json` | 完成润色且待统一收尾的 cue |
| `translated.srt` | 仅译文字幕 |
| `bilingual.srt` | 中英双语字幕 |
| `bilingual.ass` | 带独立中英样式的字幕 |
| `preview.ass`、`preview.mp4` | 短片段预览字幕和视频 |
| `qa.json`、`qa-translated.json` | 字幕 QA 报告 |
| `aligned-subtitle-qa.json` | 对齐后字幕 QA 报告（只在跑过 `--align-subtitles` 或自动对齐的 Job 上出现） |
| `burned.mp4` | 烧录视频 |

**按 Job 登记表取产物，不要按固定文件名取。** 上表是典型命名，不是契约：产物的有效性只认 Job 登记表里的内容 checksum（`xvt status --json` 的 `recovery_plan.reusable_artifacts`、`job.json` 的 `artifacts[]`，`xvt verify` 逐项复核）。两类情况会让固定路径取到错误内容：

- 源文修订（`apply-source-review-fixes`）把 `source_cues` / `source_srt` / `subtitle_qa` 重新指向 `source-reviewed-cues-<fp12>.json`、`source-reviewed-<fp12>.srt`、`source-review-qa-<fp12>.json`，而**带旧文本的 `artifacts/source.srt` 仍留在原地**。
- `rebuild-source` / `resegment-translation` 失效下游后，`translated.srt`、`bilingual.srt`、`bilingual.ass`、`qa-translated.json` 会被移出登记表，但**文件本身仍以陈旧内容留在目录里**。

这些残留文件是刻意保留的恢复证据，不会被自动删除。按固定路径读取产物的脚本请改读 Job 登记表，或先用 `xvt verify JOB` 确认。

双语输出会折叠每种语言内部的源字幕换行，确保每个 cue 固定为“译文一行 + 原文一行”。对 16:9 横版英文，64 个显示单位以内通过，65 到 72 个显示单位仅警告，超过 72 才需复核；窄画幅会自动收紧上限。中文维持更严格的单行限制。

字幕文本里的字面花括号会被保留：`bilingual.ass` 渲染时把 cue 文本中的 `{` `}` 转义为 libass 的 `\{` `\}`，因此 `{x}`、`{0}` 一类占位符会正常显示，而 `{\an8}`、`{\pos(...)}` 只作为文本呈现、不会真的改变字幕位置或样式；`source.srt`、`translated.srt`、`bilingual.srt` 导出时同样原样保留花括号。导入端按字幕的实际格式分叉（判定依据是解析出的格式而不是文件后缀，`.srt` 后缀装着 ASS 内容仍按 ASS 处理）：

- SRT/VTT（`--subtitles`、`--translation-subtitles`、`import-translation`）保留字面花括号，因此“导出 SRT → 用字幕工具编辑 → 再 `import-translation` 导回”这条链路不再丢失花括号内容。人工修订 SRT（`source-review-<fp12>.srt` / `translation-review-<fp12>.srt`）使用独立的纯文本解析器，同样保留。
- ASS/SSA 里的 `{...}` 是覆盖标签或注释块的语法本身，导入时仍整段剥除。

**输入字幕必须是 UTF-8（容忍 BOM）。** `--subtitles`、`--translation-subtitles`、`import-translation`、纯字幕输入以及人工修订 SRT 都按 UTF-8 读取，文件开头的 BOM 会被自动去掉（带 BOM 的 VTT 因此不再被误判成 SRT）。GBK、Big5、Shift_JIS、带 BOM 的 UTF-16/32 等非 UTF-8 文件会以退出码 2 报错并给出转码建议（例如 `iconv -f gb18030 -t utf-8 old.srt > new.srt`，或用编辑器另存为 UTF-8）；只有命中 UTF-16/32 的 BOM 时才会在消息里点名疑似编码。不带 BOM 的 UTF-16/32 能被 UTF-8 解码器「成功」解出夹带 NUL 的文本，因此它走的是解析失败分支（同样是退出码 2），消息会提示文件含 NUL 字节、疑似无 BOM 的 UTF-16/32。XVT 不猜测本地编码——猜错会把乱码写进翻译请求、checksum 和成品画面。内容无法按字幕解析时同样以退出码 2 报错，而不是抛出裸 traceback。

已知限制：SRT/VTT 里任何 `{\…}` 形态的字面文本（`{\an8}`、`{\alpha}` 一类公式或转义写法）与真正的覆盖标签无法区分，仍会被剥除，`{` 与 `\` 之间有空白也一样；SRT 文本里的字面 `\N` 仍被当作换行标签转换。字幕的写出只支持 `.srt` / `.vtt`，`.ass` / `.ssa` 会以 `unsupported subtitle output format` 拒绝——带样式的 ASS 输出由 `bilingual.ass` 的渲染路径负责。

若译文 QA 因超过硬显示阈值而失败，可对具有词级时间戳的 ASR cue 执行重切：

```bash
uv run --no-sync xvt resegment-translation "/path/to/job.json"
```

该命令只按语义优先边界重切超宽 cue，不会按字符拆旧译文；它会重建 `source-cues.json` 和 `source.srt`，使旧翻译批次、译文、QA、ASS 与烧录产物失效，并写入 `translation-resegmentation.json` 审计报告。端到端 Job 随后执行 `xvt continue JOB`，原子 Job 则按提示重新执行 `xvt translate`。用户导入的源字幕没有 ASR 词级时间戳，命令会拒绝改写其时间轴。

## 恢复与诊断

```bash
uv run --no-sync xvt status "/path/to/job.json"
uv run --no-sync xvt verify "/path/to/job.json" --json
uv run --no-sync xvt continue "/path/to/job.json" --events-jsonl
uv run --no-sync xvt resume "/path/to/job.json" --events-jsonl
uv run --no-sync xvt batch-continue "/path/to/batch.json" --events-jsonl
```

`continue` 根据 `run` 时持久化的目标补齐未完成步骤：远程检查或下载失败时会在同一个 Job 中重新获取素材，API 翻译、润色或语义校对中断会复用各自批次进度（Base URL 与模型取 Job 内已固化的值，换一个环境变量不同的终端也不会让指纹漂移、进度清零；API Key 仍只读当前终端的环境变量），Agent 等待会停在 `apply-translation`、`apply-polish-translation` 或 `apply-semantic-review`，局部重译（`retranslate-cues`）的等待停在 `apply-cue-retranslation`（`status` 会把具体 cue ID 与目标语言拼进命令；这一步不由 `continue` 或 `xvt apply` 推进），烧录失败只重跑烧录。译文交付物被删除或改坏时，`continue` 会从现有译文 cue 零 API 成本重建：`translated.srt`、`bilingual.srt`、`qa-translated.json` 与 `bilingual.ass` 都是「译文目标是否满足」的判定条件，其中任何一项失效都会让 `status` 归 `retryable` 并由 `continue` 走一次重算 QA + 重新渲染（不调用翻译提供方、不重写 cue）；`bilingual.ass` 这一项只对已经渲染过 ASS 的 Job 生效，纯音频与纯字幕 Job 从来不产出 ASS，不会因此永远收敛不了。上一次译文 QA 失败（例如术语死锁）之后又开了一次新的 Agent 等待时——最典型的是放弃局部重译、改跑一轮完整的 `xvt translate --provider agent`——等待优先于旧的 QA 阻断：`continue` 返回 `awaiting_input` 并提示回填，而不是抛错要求先修字幕，`status` 与 `batch-continue` 同口径（条目停在 `awaiting_input`，不记为失败）。烧录成品只有在 checksum、派生指纹和媒体 QA 全部有效时才会复用；媒体 QA 失败不会被 `status` 或批处理误判为完成。`run` / `continue` 在宣告完成（`workflow_completed`）之前还会重跑一次恢复计划做终检：计划仍有剩余步骤说明执行侧与 `status` 对同一份 Job 给出了相反结论，命令以退出码 4 失败并列出剩余步骤，而不是先报完成再让 `status` 说还差一步。正常完成路径不受影响；真实任务上撞到它请运行 `xvt status JOB` 并附该 Job 反馈，这是程序侧问题。`polished` Job 的润色草稿（`translation-draft-cues.json`）缺失或 checksum 失效时，只有翻译进度 checkpoint 能零 API 成本重建草稿，`continue` 才会继续；否则它以人工修复提示停下，不会静默重新翻译并重新润色全文，`status` 的分类与该判定一致。这种 `manual_fix` 不再是死路：Job 内仍有 checksum 有效的 `translated.srt` 时，`status` 的首选命令是 `xvt import-translation JOB <那条字幕的确切路径> --target-language LANG`（它会把这份字幕重新写成译文草稿，Job 随后可照常 `continue`；代价是这份字幕被登记为导入译文，现有译文与语义校对结论一并失效、润色腿要重跑）；没有 checksum 有效的译文字幕时仍然不给首选命令（物理文件在不在盘上不作数），但会点名那个缺失或已变化的草稿文件路径。两种情形都附带 `xvt translate JOB --target-language LANG --provider …` 作为备选，它是整轮重译 + 重润色。原子/历史 Job（没有持久化 `config.workflow`，例如旧版本 xvt 落盘或只用过 advanced `xvt translate` 的任务）上，若译文 cue 仍然有效、只有 `qa-translated.json` 失效，`status` 不再报「已完成」——烧录与预览的安全门以这份报告为硬前置，报完成会让用户在「门说去修、status 说已完成」之间打转。它现在归为 `retryable`，首选命令是 `xvt import-translation JOB artifacts/translated.srt --target-language LANG`：该命令与端到端 `continue` 的 render 走同一个收尾，从 Job 内仍然 checksum 有效的译文字幕零 API 成本重建 QA 与渲染产物（它会把这份字幕登记为导入译文，Job 从此带上「导入译文」的痕迹）；Job 内没有可复用的译文字幕时归为 `manual_fix`，只能重跑 `xvt translate`（会重新调用翻译提供方）。原子 `xvt burn` / `xvt preview` 的 QA 门文案给出同一条命令。这条零成本出口**不适用于 `polished` 质量的 Job**：`import-translation` 与其余回填路径共用同一个收尾，在 `polished` Job 上会把这份字幕写成一份新的润色草稿，QA 不重建、渲染不执行，润色候选、语义校对结论与最终译文一并失效，整条润色腿要重跑（真实模型成本）。因此 `polished` 原子 Job 上这种状态直接归 `manual_fix`，只推荐重跑 `xvt translate`，QA 门也回到通用文案。另有一条相邻的等待态残留：`TRANSLATE` 停在 `awaiting_input` 但 Job 内已经没有 checksum 有效的翻译批次 manifest 时，没有任何响应能回填这次等待，`status` 归 `retryable` 并引导重跑 `translate` 重建批次，而不是给一条注定被 fingerprint 拒绝的 `apply-translation`。**`polished` 原子 Job 上「润色候选仍有效」同样给在途等待让路**：`TRANSLATE` 停在 `awaiting_input` 且翻译批次 manifest checksum 有效时，`status` 报等待并引导 `apply-translation`，不再推荐 `xvt translate --translation-quality polished`——那条命令在这个状态下不会继续语义校对（Agent 模式下它只重写一遍 manifest 就返回），用户当初显式传过 `--batch-size` 或 `--glossary` 时还会改掉 manifest 的 fingerprint、让手里准备好的响应被拒收。`resume` 仅负责从规范化音频、词级转写或源 cue 恢复转写主链路。`verify` 会重新计算 artifact 的 SHA-256。`status` 使用同一套结构化恢复计划，区分可重试、等待输入、需要人工修复、不可自动恢复和已完成，并只把 checksum 有效的 artifact 视为可复用产物；`--json` 会额外输出 `recovery_plan`。

任务创建后会立即显示 `job.json` 路径。后续失败时，普通模式会显示失败阶段、任务路径和结构化恢复建议；已有 Job 的原子命令同样保留任务路径。恢复建议按「待编辑文件 → 前置命令 → 首选命令 → 备选命令」固定顺序输出，只有失败/中断和 `status` 这两个需要溯源的场景才额外列出报告路径；同一修订通道的 JSON 与同名 SRT 不各占一行，只列 JSON 并在行尾注明「（同名 .srt 亦可）」，两副载体都照旧接受。等待态里 `恢复建议：` 与紧邻的 `[等待] 翻译需要输入：` 逐字相同时不重复打印；这一去重仅在事件流与 stdout 同时连到终端（TTY）时生效，任一侧被重定向（例如 `xvt run … > out.txt`）时 `恢复建议：` 仍会打印，避免管道或日志里丢掉这条原因。用户按 Ctrl-C（SIGINT）或向进程发送 SIGTERM 时，外部命令的整个进程组会被清理，当前运行阶段记录为失败，CLI 分别返回退出码 130 和 143 并重新计算恢复计划。批处理在任何时刻被打断都会落盘：仍在运行的条目降级为 `interrupted`，`batch.json` 的 `status` 同样写成 `interrupted` 并带上 `next_action`（继续用 `xvt batch-continue`），不会停在 `running` 且没有下一步。字幕 QA 失败只展示报告和人工修复提示，不会错误建议继续烧录。 对 `needs_review` 级的报告（例如导入字幕折叠后超宽），确认可接受后可用 `xvt continue JOB --acknowledge-subtitle-review` 显式放行：确认记录绑定当前字幕内容、被确认的那份 QA 报告与具体问题集合，源字幕或译文变化（报告随之变化）或出现新的 review 问题后自动失效；error 级问题（时间轴无效、缺失译文、术语违规等）永远不能通过该通道放行。当前 schema 的 Job 缺少必填字段、stage 或正确字段类型时会返回无效输入错误，不输出 Python traceback；`source-cues.json`、`translated-cues.json` 等 cue 产物结构损坏时同样收口为无效输入错误（退出码 2 并发出 `cli_failed`），未知字段按前向兼容忽略。命令行里给出的 `job.json` / `batch.json` 路径不存在（或指向目录），以及 Agent response 文件缺失、不是 UTF-8 编码或不是合法 JSON，都以退出码 2（`invalid_input`）报错并给出路径，不再归入依赖故障（退出码 3）——这些位置参数永远来自用户输入。响应文件的编码要求与输入字幕一致（UTF-8，容忍 BOM），非 UTF-8 时 `xvt apply` 会附上转码建议（例如 `iconv -f gb18030 -t utf-8 old.json > new.json`）。这类回填失败只影响这一次命令：校验是只读的，Job 状态不变，原本在等的那一步仍在等，换对文件重来即可。反过来，Job 内**已登记**的 artifact 文件缺失不是用户输入错误：路径来自 Job 自己，仍按依赖或工作流问题处理，用 `xvt status` / `xvt verify` 定位。远程端到端任务即使尚未形成媒体 checkpoint，也可以用 `xvt continue JOB` 重试检查和下载；重新提供 `--cookies-from-browser` 或 `--proxy` 时，这些参数同时用于远程检查与素材下载。任务创建前的参数或输入错误仍需修正后重新执行。

`run`、`continue`、`batch`、`batch-continue`、`translate`、`prepare-translation`、`apply`、`apply-translation`、`polish-translation`、`apply-polish-translation`、`apply-semantic-review`、`acknowledge-translation-review`、`import-translation`、`resegment-translation`、`retranslate-cues`、`apply-cue-retranslation`、`preview` 和 `burn` 支持 `--events-jsonl`。该模式保留机器接口：stdout 只输出逐行 JSON 事件，不混入普通进度文本；端到端链路还会输出 `workflow_started`、`workflow_waiting`、`workflow_completed`、`download_progress`、`transcription_progress` 和 `burn_progress`，其中完成事件会列出 checksum 有效的用户产物，仅生成源字幕时也包含 `source.srt`。完整步骤计划 `workflow_planned` **只有 `run` 首跑会发**：`continue` 只发带 `continuation=true` 的 `workflow_started`、不重发计划，它的剩余步骤由 `status` / `recovery_plan` 的 `advance_steps` 给出。批处理会输出 `batch_started`、逐项终态和带汇总计数的 `batch_finished`，预览会输出 `preview_started`、`preview_progress` 和 `preview_completed`。`transcription_progress` 的 `determinate=true` 表示可用真实百分比（当前为 faster-whisper），MLX 使用 `determinate=false` 和 `elapsed_seconds`，只有词级时间戳验证成功后才发送 100%。完成或失败时以结构化终态事件收口；批处理存在失败项或等待输入项时不会发出 `cli_completed`。命令事件和批处理错误报告会脱敏代理账号、密码、Cookie、鉴权请求头和常见签名查询参数；翻译提供方 HTTP 错误里回显的响应体，以及写进 `job.json`、由 `status --json` 原样输出的失败原因，在产生消息处就套用同一套脱敏，不依赖事件边界。

`apply-translation-review-fixes` 同样支持 `--events-jsonl`，并通过 `translation_review_input_ready` / `translation_review_revised` 表达模板准备和修订接受；事件不会携带字幕正文。

## 退出码

CLI 使用集中定义的退出码表达终态语义（见 `src/xvt/errors.py`）：

| 退出码 | 含义 |
| --- | --- |
| 0 | 命令成功完成 |
| 2 | 用户输入或配置错误，需修正后重试 |
| 3 | 外部依赖或子进程故障（`doctor` 未通过、翻译提供方故障等） |
| 4 | 内部工作流不变量被破坏，或 Job 状态不满足命令前置条件（例如 `preview` / `burn` 缺少 checksum 有效的译文与 QA 报告，译文 QA 未放行，或 `run` / `continue` 的完成终检发现恢复计划仍有剩余步骤） |
| 5 | `verify` 校验未通过，artifact checksum 不一致 |
| 6 | 批处理整体完成但存在失败条目 |
| 7 | 目标 Job 或 batch 正被另一个 XVT 进程独占执行 |
| 10 | 命令按设计停在 `awaiting_input`（仅 `run` / `continue` / `apply --continue` / `batch` / `batch-continue` 显式加 `--fail-on-waiting` 时返回；不代表出错） |
| 130 | 被用户中断（SIGINT / Ctrl-C） |
| 143 | 被 SIGTERM 终止，中断状态已持久化 |

默认行为不变：`run` / `continue` / `apply --continue` / `batch` / `batch-continue` 停在 `awaiting_input` 时仍返回 0，现有脚本与 Agent 流程无需改动——每一次 Agent 模式的 `xvt run` 都会停在 `awaiting_input`，把它默认判成失败会在流程第一步就中断设计好的协作路径。需要脚本区分「完成」与「等待回填」时加 `--fail-on-waiting`：等待态返回退出码 10（`AWAITING_INPUT`），失败仍返回各自的失败码（批处理有失败条目时是 6，**优先于 10**）。10 不代表出错：不发 `cli_failed`，Job 状态、batch 状态、事件流与 stdout 文本都与不加旗标时完全一致，旗标唯一改变的是进程退出码。`--events-jsonl` 与 `--fail-on-waiting` 正交，可同时使用；JSONL 消费端也可以继续按「没有 `cli_completed`」或 `batch_finished.status` 判断，无需改用退出码。

旗标刻意只加在这 5 个导航级命令上：`prepare-translation` / `retranslate-cues` / `prepare-source-review` 的「等待」就是它们的交付物本身（生成批次文件让 Agent 去填），返回非零毫无意义；`translate` / `polish-translation` / 各条显式 `apply-*` 是原子步骤，调用方本来就在逐步驱动。`xvt apply --continue` 在列，是因为它就是 Agent 循环的主推进命令：回填一次、继续跑到下一个等待态，无人值守流水线正需要区分「跑完了」和「停在下一个等待」。不加 `--continue` 时它退化成一次纯回填，旗标没有作用。这是取舍，不是遗漏。

## 开发

```bash
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pytest -q
```

更多设计说明见 [架构说明](docs/ARCHITECTURE.md)，恢复与回填语义见 [恢复与回填契约](docs/RECOVERY.md)，已完成和待完成工作见 [路线图](ROADMAP.md)，协作约定见 [贡献指南](CONTRIBUTING.md)。

## License

[MIT](LICENSE)
