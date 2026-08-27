# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的分类方式。以下记录基于首次公开版本前已验证的功能状态。

## [Unreleased]

### Added

- 人工校对恢复链新增 `xvt apply JOB RESPONSE --continue`，译文修订可在同一进程回填并继续；JSONL 模式保持逐行纯 JSON。修订模板的 JSON/SRT 都严格要求完整 cue ID 集合，接受后记录实际 `changed_ids`，只复查受影响的完整语义组并把相邻组作为只读上下文，不重新翻译、润色或再次自动修复。源文修订会失效译文及下游，因此明确拒绝 `--continue` 并要求先查看 `status`。
- polished 候选新增 checksum 绑定的 `translation_candidate_qa` 门禁。候选变化、报告缺失或损坏时必须重跑，不能复用旧 pass；只有可通过修改目标译文解决的确定性问题进入译文修订，源文宽度与时间轴问题不会形成无出口循环。
- R2.4 第二阶段恢复导航与批处理安全：当烧录目标的静态条件（媒体 QA、当前 fingerprint 和 checksum 有效的 `version=2` proof）均满足、但已登记 `burned_video` 缺失或 checksum 失效时，`status` 返回 `awaiting_input`，首选给出 `relink-burned-output` 占位命令、备选显式 `continue` 重烧。该建议只表示成品**可能**可重新关联，执行 relink 仍会重新校验实际源媒体内容 checksum；legacy fingerprint、媒体 QA/proof/fingerprint 无效和从未登记成品仍保持 `retryable` 并仅推荐重烧。`batch-continue` 将此 `awaiting_input` 作为停止态，不会自动重编码可能已移动或替换的外部成品。
- 新增 `xvt relink-burned-output JOB EXISTING_VIDEO`：烧录成品被移动后，仅 checksum 有效的 `version=2` burn proof 可重新关联；proof 绑定当前烧录 fingerprint、原 `burned_video` checksum 和实际源媒体内容 checksum，候选文件还须与原成品 checksum 完全一致并通过既有媒体 QA。旧/legacy fingerprint 仍可按既有轻量完成语义读取，但不得 relink，必须先 `continue` 重烧一次生成 v2 proof。重复 `burned_video` 登记 fail closed；普通 `status` 不会为 v2 proof 额外计算源媒体内容 checksum，该 checksum 仅在烧录落证和 relink 校验时计算。只有 `run` / `continue` 按持久化目标完成的烧录签发 workflow proof，高级原子 `xvt burn` 不签发。成功只更新 artifact 路径，不复制、移动或重新编码视频；失败时 Job 保持不变，并复用既有 artifact 事件和单一完成判定。
- 源文修订新增与译文通道对称的人工 SRT 编辑载体：`prepare-source-review` 同时生成 `source-review-<fp12>.json` 与同名 SRT，`apply-source-review-fixes` 和统一入口 `xvt apply` 均可按固定文件名识别 SRT。SRT 用文件名承载 source checksum 与精确 cue ID 集合的 fingerprint，按原时间轴还原 cue ID；改时间轴、增删 cue、注释 ID 不符、空文本或改名都会在回填前拒绝。JSON/SRT 汇聚到同一源文修订流程，只改 `source_text`，保留 ID、时间轴、words 和 provenance；`status` 同时列出两种输入载体。
- 模型生成的简体中文字幕新增确定性标点收口，覆盖初译、一次翻译加润色、显式润色、语义修复和局部重译：消除全角中文逗号、句号，其中句中位置按需替换为一个半角空格以保留 `OpenAI GPT-5` 一类词边界，行尾与换行前直接移除；模型把省略号写成的连续全角句号归一为单个 `…`；问号、感叹号、分号、冒号、破折号、ASCII 标点和原有换行一律保留。左右同形的引号（`"`、`'`、`＂`、`＇`）按上下文推断开闭，英文词内撇号和数字后的尺寸符号不参与引号计数，补出的空格始终落在引号之外。源 cue 末尾问句而译文缺少句末语义标点时补 `？`，位置镜像源文中问号相对末尾闭合符号的位置：源文 `(ready?)` 得到 `（准备好了吗？）`，源文 `Is it (a)?` 得到 `是吗（a）？`；译文闭合符号少于源文时按译文实际数量收敛。语义审查把半角空格和 cue 边界视为有效分隔，不会仅因缺少全角逗号或句号报告 `awkward_join`。语言门只接受显式 `zh-CN` / `zh-Hans` 等简中标签，bare `zh`、繁中及其他目标语言不处理；导入译文、人工译文修订和源字幕保持原文。该步骤不改变 cue、时间轴、Job schema、artifact 或事件，checkpoint/Agent 回填重放使用相同路径以保持 checksum 稳定。
- 翻译与润色提示词增加字幕专用的去模型化表达约束：避免无来源依据的破折号、公式化对比、空泛连接词、宣传式措辞和抽象拔高，同时明确保留源文真实的对比、强调、列举、打断、语气与全部信息；语义修复遵守同一约束但语义审查类别保持不变。translation、polish、semantic review、semantic repair prompt 分别为 v5、v3、v4、v3；review v4 进一步拒绝无修改建议、`source_suspect` 非空建议和单 cue `awkward_join`。旧 API checkpoint、润色 checkpoint 及带版本指纹的 polished/semantic Agent manifest 安全失效，已等待的 standard Agent Job 仍可按严格 ID 契约回填。Agent 局部重译 manifest 绑定提示词版本、源文、当前译文和策略 fingerprint，旧响应不能覆盖新状态；恢复 planner 会绕过失效的旧润色 manifest 并重建当前批次。Agent 翻译协议同步更新，不改变 cue、时间轴、Job schema 或事件协议。
- README 新增完整 CLI 命令与参数参考：按日常命令和高级/修复命令覆盖全部位置参数、可选参数、默认值、互斥关系及源字幕选择速查；新增文档契约测试，从真实参数解析器校验每条命令和参数均已登记，防止 CLI 与 README 漂移。
- 译文修订新增人工 SRT 编辑通道：`await_translation_review` 生成 JSON 模板的同时，在 `review/` 目录写出同名同 fingerprint 的 `translation-review-<fp12>.srt`（每条正文为 `#<cue id>` 注释行加当前译文），可用任意字幕工具编辑；`apply-translation-review-fixes` 按 `.srt` 后缀自动识别该通道。fingerprint 由文件名承载（要求文件名含当前 fingerprint 前 12 位），cue ID 按时间轴精确匹配还原，因此改动时间轴、增删 cue、注释行与时间轴不一致都在解析边界被拒。两条通道解析后汇聚为同一 `changes` 映射，fingerprint 绑定、只改 `target_text`、review-only 复查完全共用，对同一组编辑产生完全相同的修订记录 checksum。统一入口 `xvt apply JOB RESPONSE` 同样接受该 SRT：按生成时的固定文件名（`translation-review-<fp12>.srt`）确定性识别为修订载体，不与 `import-translation` 的译文字幕输入混淆；其他 SRT 仍按原样报错并引导 `import-translation`。JSON 通道原样保留给 Agent 与脚本消费；旧 Job 只有 JSON 模板时按同一 fingerprint 补齐 SRT；`status` 的 `input:` 一并列出 SRT 路径。
- 新增统一回填入口 `xvt apply JOB RESPONSE`：人工修订模板（`source-review-*.json` / `translation-review-*.json`，CLI 生成、用户编辑后原样提交）携带 `task` 字段，按该字段路由到对应的 `apply-*` 实现；Agent 翻译/润色/语义校对响应契约不含 `task` 字段，按 Job 当前等待的推进步骤路由。原子 Job（无持久化 `config.workflow` 目标）不受统一入口支持，仍需使用显式 `apply-*` 命令；路由不出来时报结构化错误而不猜测。`--target-language` 由 Job 配置自动解析。各 `apply-*` 命令保留为稳定入口且行为不变，回填实现的校验逻辑零改动。`status` 的 `before:` 改为推荐统一入口，原命令降为 `alternative:`。需额外参数的 `apply-cue-retranslation` 与输入为字幕文件的 `import-translation` 不纳入路由。
- CI 新增 Python 版本矩阵（3.11、3.13，与 os 矩阵正交）与 `uv build` 打包验证 step，确保多版本、多平台均可正常构建 wheel/sdist；AGENTS.md 补全 `pipeline` 包依赖规约（`polish → translation`、`revision → common`），并新增 `tests/test_pipeline_imports.py` 以 `ast` 解析各模块顶层 import 断言依赖方向与硬编码白名单完全一致，把包内无环约束从文档纪律变成失败的构建。
- Q6.1c 提示词加固：API 与 Agent 统一以完整语义组处理目标语言表达，减少逐 cue 直译、悬空片段和生硬拼接；强化专名、产品名和代码标识符保护，细化语义问题分类及 `source_suspect` 证据门槛，并要求完整组联合修复。translation、polish、semantic review、semantic repair prompt 分别升级为 v4、v2、v2、v2，使旧 API checkpoint、润色 checkpoint 及带版本指纹的 polished/semantic Agent manifest 安全失效；已等待的标准 Agent Job 继续按严格 ID 契约兼容回填，重新准备批次后使用新版提示词。不改变 cue、时间轴、Job schema 或事件协议。
- Q6.1b 收尾：Q6 语义报告包含 `source_suspect` 时，API、Agent 和 `continue` 等待路径自动为相关 cue 生成源文修订模板；`status` 保持只读并优先展示源文回填，同时保留译文回填和直接确认。旧等待 Job 缺模板时引导 `continue` 懒生成，避免展开超长 cue 参数；导入译文任务不生成或推荐不可提交的源文修订。复用现有 stage、artifact 和事件协议，不升级 Job schema。
- Q6.1b 源文修订核心：新增 `prepare-source-review` 与 `apply-source-review-fixes`，以 source checksum 和精确 cue ID 集合绑定可编辑模板；回填只修改源文，保留 ID、时间轴、words 与 provenance。接受后立即执行源字幕 QA，以 fingerprint 文件和一次 Job 原子保存切换修订、source cue、选择、QA 与 SRT 证据链，并失效旧翻译、语义校对、渲染和烧录产物。新增 `source_review_input_ready` / `source_review_revised` 事件，事件不包含字幕正文；导入译文任务首版明确拒绝。
- Q6.1a 人工译文修订：语义复查仍有 review 问题时生成 fingerprint 绑定的可编辑模板；`apply-translation-review-fixes` 严格校验并只修改既有 cue 的译文，保存修订记录、独立候选和复查报告。API/Agent 都只重新复查，不增加自动修复预算；`status` 展示修订首选命令和直接确认备选命令，旧等待 Job 可由 `continue` 补生成模板。新增 `translation_review_input_ready` 与 `translation_review_revised` 事件，事件不包含字幕正文。
- Q6 polished 译文语义校对：润色候选先生成结构化语义报告，对 `meaning_error`、`omission`、`terminology`、`awkward_join` 按完整语义组最多修复一次并最终复查，`source_suspect` 只进入人工确认且在无关修复后继续保留。API 各阶段逐批 checkpoint，并使用阶段对应的结构纠错契约；Agent manifest 严格绑定当前 Job、候选、版本和批次，完整响应校验通过后才持久化。新增四类 checksum artifact、`apply-semantic-review`、`acknowledge-translation-review`、`continue --acknowledge-translation-review`、恢复步骤与 `translation_review_acknowledged` 事件；初次可修复报告不能提前确认，最终报告必须绑定完整有效的修复链，确认也不能绕过确定性字幕 QA。默认仍为 `standard + faithful`。
- Q5b 新 Job 翻译质量模式：`run`、`batch`、`translate` 和 `prepare-translation` 新增 `--translation-quality standard|polished`，默认保持 `standard + faithful`。兼容 API 的 polished 路径按 draft -> polish 分阶段 checkpoint；Agent 以带 task/version/fingerprint 的单个 manifest 完成一次翻译+润色回填，并可从完整 progress 确定性补齐中断的最终 cue、QA 和渲染。旧 Job、旧 batch 和旧 Agent manifest 继续按 `standard`。
- Q5a 可恢复译文润色：新增 `polish-translation` 与 `apply-polish-translation`，为已有端到端 Job 保存不可变草稿，API 逐批 checkpoint、Agent 以 fingerprint 和完整 ID 严格回填，并由 `status` / `continue` / `batch-continue` 恢复。新增 draft、batch、progress、polished 四类 checksum artifact；润色只改译文文本，完成后仍经确定性 QA 与渲染。旧 Job 缺质量字段继续按 `standard`。

- Q4 语义组翻译：翻译前按句末标点、650ms 间隔及 20 cue / 30 秒上限确定性分组；provider 批次按持久化配置上限以完整语义组装箱，避免在批次边界切开句意，当前新 Job 默认最多 25 个 cue。`translation-batches.json` 的每个 batch 新增兼容字段 `semantic_groups`（cue ID 组），Agent / provider 的响应仍只返回既有 `items`，并继续严格校验全部 ID；回填不改变 cue 顺序或时间轴。提示词版本升级会使旧 API 翻译进度安全失效并从完整批次恢复；已等待输入的旧 Agent Job 继续按严格 ID 契约兼容回填。默认翻译风格仍为 `faithful`。
- 新 Job 支持 `--source-strategy auto|subtitles|asr`；显式平台字幕候选会先经过确定性 QA，并持久化候选报告、最终选择和 `source_selected` 事件。
- advanced `xvt rebuild-source JOB --strategy asr [--cookies-from-browser ...] [--proxy ...]`：显式以 ASR 重建既有媒体 Job 的源字幕，不由 `continue` 自动执行。复用校验通过的 words/audio/本地或已下载媒体，远程缺 checkpoint 时才重取；持久化 ASR 策略、修复意图并关闭对齐，失效源 cue、候选 QA、选择、重分段及全部下游产物，同时保留输入译文字幕以重新验证映射。重建中断后 `status` 继续给出显式修复命令；端到端 Job 成功后用 `continue`，旧原子 Job 用 `translate`。不新增 stage、schema 或事件类型，复用 `source_selected` 与 `cli_completed`。

### Fixed

- SRT/VTT 导入端不再剥除字面花括号，字幕的写出也不再接受 `.ass` / `.ssa`。此前 `load_subtitles` 一律读 pysubs2 的 `plaintext`，而它按 SSA 规则把任意 `{...}` 当作覆盖标签块整段删掉：`--subtitles`、`--translation-subtitles`、`import-translation` 读进来的 `变量 {x} 的值` 会变成 `变量 的值`，「导出 SRT → 用字幕工具编辑 → 再导回」这条链路上的占位符因此每过一次就丢一次，而导出侧已经在保留花括号，两侧口径相反。现在导入按 pysubs2 解析出的实际格式分叉（不看文件后缀，`.srt` 后缀装着 ASS 内容仍按 ASS 处理）：SRT/VTT 用自写的取文本实现，只剥 `{\…}` 形态的覆盖块（`{` 与 `\` 之间允许空白，`{ \an8}` 同样被剥，SRT reader 由 HTML 标签转出的 `{\i1}` 也不会漏进正文），随后按与 `plaintext` 一致的顺序处理 `\h`、`\n`、`\N`；ASS/SSA 维持原样，那里的 `{...}` 是标签语法本身。用户可见的影响有四点：一是跨版本 `resume` / `continue` 重读 `input_subtitles` 时，含字面花括号的老 Job 会得到不同的 `source_cues` checksum，可能触发润色指纹或 Agent manifest 失配并重建（polished 链路的重建有 API 成本），命中面限于字幕本就含花括号的 Job，且这些 Job 的旧内容原本就是残缺的；二是保留下来的花括号会计入显示宽度，个别本就接近上限的 cue 可能从 `pass` 变成 `warning` / `needs_review`（这是修正而非回归，此前的宽度是按被吞掉内容后的短文本算的）；三是整条正文只有 `{x}` 的 cue 此前被判为空而丢弃，现在会保留，导入字幕的 cue 条数可能变多，`import-translation` 的映射也因此更容易命中 `strict_order` 严格顺序分支；四是 `save_subtitles` 写 `.ass` / `.ssa` 改为以 `unsupported subtitle output format` 拒绝——该路径在项目内没有调用方，且写出的 ASS 不做花括号转义、内容会被 libass 吞掉，真正的 ASS 输出一直由 `bilingual.ass` 的渲染路径负责（它会转义）。仍存在的限制：SRT/VTT 里任何 `{\…}` 形态的字面文本与真覆盖标签无法区分，仍会被剥除；SRT 文本里的字面 `\N` 仍按换行标签转换。
- 输入字幕的编码与解析失败改为可操作的用户错误，带 BOM 的字幕不再误判或崩溃。`load_subtitles` 此前按 `encoding="utf-8"` 读取，UTF-8 BOM 因此原样留在正文开头，而 pysubs2 的格式嗅探是 `lstrip().startswith("WEBVTT")`——BOM 不是空白、`lstrip` 去不掉，于是带 BOM 的 VTT 一律漏判：`hh:mm:ss.mmm` 形态被当成 SRT 解析出错位内容，`mm:ss.mmm` 形态直接抛 `FormatAutodetectionError`；该异常直接继承 `Exception`，不在 CLI 顶层兜底的捕获范围内，用户看到的是裸 traceback、返回码 1，也没有 `cli_failed` 事件。非 UTF-8 字幕（GBK、Big5、Shift_JIS、UTF-16/32）则以 `UnicodeDecodeError` 结束：它是 `ValueError` 的子类，CLI 侧退出码虽是 2，但 `stage_failed` 记成 `workflow`、与 `cli_failed` 的 `invalid_input` 两处口径不一致，消息还是 codec 层的字节偏移文案，并且指向 Job 内部副本 `artifacts/input-subtitles.*`——让用户去转码那个副本没有任何意义。现在读取统一为 `encoding="utf-8-sig"`（与人工修订 SRT 的读取口径一致，无 BOM 时与 `utf-8` 完全等价），BOM 在解析前被去掉，带 BOM 的 VTT 恢复按 VTT 解析；`UnicodeDecodeError` 与 pysubs2 的解析异常都包装为 `UserInputError`，得到 `invalid_input` 与退出码 2，`stage_failed` 与 `cli_failed` 的 category 首次一致。编码报错给出可执行的下一步（`iconv -f gb18030 -t utf-8 old.srt > new.srt`，或用编辑器另存为 UTF-8）；文件头命中 UTF-16/32 的 BOM 时消息额外点名疑似编码，这只是 BOM 前缀比对，XVT 仍不做本地编码探测——宽松解码器会把 Big5、Shift_JIS、cp1252 的内容「成功」解成乱码，乱码会流进翻译 API、固化进 checksum 并烧进画面，代价远高于直接报错。报错里的路径改为用户传入的原始文件：`load_subtitles` 新增可选展示路径参数，先 copy2 成 Job 内部副本再读的三处调用点都会传入原路径（内容由 copy2 保证一致），Job 内部副本路径不再出现在消息里。消息只包含路径与建议，不回显文件内容。候选字幕的评估路径行为不变——编码或解析失败仍降级为 `invalid_candidate` 并回退 ASR，不会让 Job 失败，只是降级理由变得可操作；`OSError` 仍不在此处捕获，保持既有的依赖类退出码 3。
- 译文 QA 报告单独失效的端到端 Job 不再卡死在「门让你 continue、continue 说已完成」的闭环里。`burn` 与 `preview` 共用的 fail-closed 译文 QA 门要求 checksum 有效的 `qa-translated.json`，而判断译文目标是否完成的谓词此前只看译文 cue 与两份 SRT：报告被删除、被覆盖或从未登记时，`plan_advance_steps` 返回空计划，`status` 归为 `complete`，`continue` 打印「已完成」且什么也不做，QA 门却继续拒绝烧录和预览，用户没有任何自动修复路径。现在译文 QA 报告与译文 cue、译文/双语 SRT 一样是译文目标的完成条件，planner、`continue` 执行侧与 `status` 恢复建议三处仍从同一谓词派生：该状态落进既有的 `render` 分支，`status` 变为 `retryable` 并给出 `continue`，`continue` 走「从现有译文 cue 补跑 QA 与渲染」的既有路径——重算 QA、重新写出字幕产物，**不调用翻译 API、不重写 cue**。polished 链路的复用短路（语义校对完成后判断是否可直接复用已有译文产物）同步把译文 QA 报告纳入判定，否则它会在报告失效时空转短路、让 `continue` 永远修不好这一份报告。恢复建议里的 relink 导航也一并修正：报告失效会让计划从 `("burn",)` 变成 `("render", "burn")`，此前只匹配首个步骤的判断会因此丢掉 `relink-burned-output` 建议，把本可零成本重新关联的成品引导成一次完整重编码；`render` 只从现有 cue 重算、不改写 `translated_cues`，烧录派生指纹仍然成立，因此该建议在此形态下继续给出。用户可见的影响：历史上从未登记过译文 QA 报告的老 Job（例如 QA 落盘前中断留下的断点）首次 `continue` 会多做一次 QA 与渲染，这一步零翻译 API 调用；重算出的字幕内容与旧产物一致时 checksum 不变，因此不会触发重新烧录，紧接着的第二次 `continue` 计划为空、正常报告已完成（幂等，不会形成循环）。`needs_review` 的既有确认记录绑定的是源 cue checksum 而不是报告文件，重算后仍然有效并照常放行；重算结果为 `failed` 或出现未被确认覆盖的新 review 级问题时按常规 QA 阻断——前者由 QA 收口逻辑抛出并以退出码 4（`workflow`）失败，后者把 `subtitle_qa` 置为等待人工确认。原子 Job（没有持久化 `config.workflow`）上报告失效仍归为 `complete`，与既有的原子命令入口保持一致，不在本次改动范围内。
- `--glossary` 的术语校验在端到端 Job 的兼容 API 翻译路径上真正生效。此前两处缺陷叠加：一是 `prepare_agent_translation` 把 `target_language` / `source_language` / `translation_policy` 写进 `job.config` 的时机晚于本函数最后一次落盘（`add_artifact` 内的 `save_job`），而 API 模式下 `run_translation` 紧接着重新 `load_job`，端到端 Job 的解析后策略因此从未落到顶层配置；二是译文 QA 取必需术语时只读顶层 `job.config["translation_policy"]`，不看端到端 Job 真正的策略快照 `config.workflow.translation_policy`。两者叠加的结果是 `xvt run --glossary ...` 在 API 翻译下必需术语集合恒为空：违反术语表的译文不会产生 `glossary_violation`，`qa-translated.json` 直接 `pass`，渲染和烧录照常完成。现在配置在 `add_artifact` 之前写入以保证持久化，必需术语也改为优先读 workflow 策略快照、再回退顶层配置，因此修复前创建的 Job 在下次 QA 时同样恢复校验；polished 链的候选 QA 与译文 QA 共用同一份来源。这是用户可见的行为变化：此前能通过 QA 并被渲染、烧录的违规译文，现在会以 error 级 `glossary_violation` 阻断，需修正译文或调整术语表后再继续。Agent 模式此前因 `set_stage` 顺带落盘而未受影响，原子 `xvt translate` 也不受影响。
- 翻译响应与回填拒绝布尔 cue id。JSON 的 `true` / `false` 在 Python 中是 `int` 的子类，因此 `{"id": true, "text": "..."}` 能通过 `isinstance(cue_id, int)` 并被当作 cue 1 接受：兼容 API 响应解析会把该条译文错写到 cue 1 上，Agent 回填也一样，或者在 cue 1 已被正常条目占用时报出「重复 id」这种指错方向的错误。项目其余解析器（润色、语义校对、局部重译、人工修订）早已显式排除 `bool`，只有主翻译的这两处遗漏。现在两处口径统一：兼容 API 得到的响应按结构性无效处理，进入既有的一次携带纠正提示的重试；`apply-translation` 与统一入口 `xvt apply` 则以 `invalid_input`（退出码 2）拒绝并保留 Job 状态，不写入错位译文。字幕 QA 确认记录的读取（`acknowledged_review_issues`）同样补上该排除，布尔 `cue_id` 不再被归一成 cue 1 而扩大放行范围。
- 译文中的字面花括号不再被静默吞掉。ASS 用 `{...}` 包裹覆盖标签，此前渲染 `bilingual.ass` 时直接写入 cue 原文：译文里的 `{x}`、`{0}` 一类占位符会被 libass 当作标签块整段丢弃，烧录后画面上什么都不剩；更糟的是模型或人工修订写出的 `{\an8}`、`{\pos(...)}` 会被当成真正的覆盖标签执行，实际改变该条字幕的位置或样式。同时 `translated.srt` / `bilingual.srt` / `source.srt` 的导出走 pysubs2 默认路径，也会按 SSA 标签块规则剥掉 `{...}`。现在渲染前对 cue 文本转义为 `\{` / `\}`（libass 扩展，本项目烧录固定走 ffmpeg/libass），双语行分隔用的 `\N` 与样式切换 `{\rSource}` 是渲染器自己拼接的结构、不参与转义；SRT/VTT 导出改为保留原文，花括号原样落盘。反斜杠刻意不转义：ASS 没有标准的反斜杠转义，libass 对未知 `\x` 序列原样输出，转义反而会引入可见杂字符。该条目当时遗留的导入端边界（`load_subtitles` 读 pysubs2 的 `plaintext`，仍剥除 `{...}`）已由本节「SRT/VTT 导入端不再剥除字面花括号」一条收口，不再存在。
- 输出目录路径包含单引号时的烧录与预览不再必然失败。字幕通过 `-vf ass=filename=...` 进入 FFmpeg filtergraph，此前的实现把路径包在单引号里并只做一层替换，而 filtergraph 的单引号包裹结构无法表示引号本身（shell 风格的 `'\''` 在这里不成立），于是 `--output "/Users/me/Tom's Videos"` 这类路径下的 Job 每次烧录都以 libass 打不开字幕文件告终，错误信息还指向字幕文件本身而不是路径转义。现在 `escape_filter_path` 实现规范的两层转义——先按 filter 选项值层处理 `\`、`'`、`:`，再按 filtergraph 层处理 `\`、`'`、`,`、`;`、`[`、`]`——并去掉引号包裹，直接以裸值嵌入；逗号、分号、方括号、冒号等 filtergraph 元字符一并覆盖。外部命令仍以参数数组执行，本次只修 filtergraph 这一层内部语法。新增使用真实 ffmpeg 的冒烟测试，在含单引号、逗号和元字符的目录里烧录并按亮度确认字幕确实渲染上去。
- 退化时间轴改为钳制而不是产出非法 cue。相邻 cue 重叠时的历史处理是取中点切开，但当后一条 cue 与前一条严重重叠或完全嵌套（例如平移后的对齐结果），或 ASR 词级时间戳严重回退时，中点会落在某条 cue 的边界之外，直接产出零时长甚至负时长的 cue；ASR 分段的尾部收口 `min(padded_end, next_start, media_duration_ms)` 在媒体探测时长早于 cue 本身时也会算出 `end <= start`。这类 cue 会一路走到字幕 QA，以 error 级 `invalid_timestamp` / `overlap` 卡住 Job，并给出「请修复源字幕」这种对着 ASR 或对齐产物无从下手的误导性指引。现在分段与对齐共用同一个边界归一实现：中点无法给两条 cue 都留出正时长时，改为把后一条推到前一条结束处（QA 的重叠判定是严格小于，边界相接合法），仍不足则补足 1ms 最小时长，受影响的 cue 在 `warnings` 里留痕（写入 `source-cues.json`，不产生新的 QA 问题）；归一后再做一次单调性自检，真出现不变量破坏时以明确的内部错误失败，而不是把非法时间轴交给下游。对齐路径同时不再让输出 cue 与输入 cue 共享同一个 `warnings` 列表，钳制不会回头污染候选 cue。
- cue 产物结构异常时不再抛裸 `TypeError`。`load_cues` 此前直接把 JSON 条目展开成 `Cue(**item)` / `Word(**word)`，因此 `source-cues.json` / `translated-cues.json` 被手工改坏、被别的工具写入未知字段，或数组里混进字符串、`null` 一类非对象条目时，会以 Python traceback 退出、返回码 1 且不发出 `cli_failed` 事件，调用方拿不到结构化错误。现在与 `Job.from_dict` 采用同一套前向兼容策略：只保留 `Cue` / `Word` 的已知字段，未知字段忽略（新版本写入的扩展键不再让旧版本崩溃），结构确实无法解析时包装为 `invalid cue artifact: ...` 的 `ValueError`，由既有边界收口为 `invalid_input`、退出码 2 并正常发出 `cli_failed`。
- yt-dlp 字幕下载按请求语言选取文件。下载目录使用固定的 `source.%(ext)s` 模板，yt-dlp 会把语言写进文件名，因此同一个 Job 的 `input/subtitles/` 里可能同时存在 `source.en.srt`、`source.en-orig.srt`、`source.de.srt`、`source.zh-Hans.srt`（一次请求写出多个变体，或先后重试过不同语言）。此前取 `sorted(glob("source*.srt"))` 的第一个，实际就是字典序最小的文件，可能选中与请求语言完全无关的字幕并把它当作源字幕候选送进 QA 与翻译。现在优先精确匹配 `source.<language>.srt`，其次匹配 `source.<language>.*` / `source.<language>-*` 变体（大小写不敏感，因此 `zh` 也能命中 `source.zh-Hans.srt`），只有目录里没有任何携带请求语言的文件时才回退到原来的字典序首个。产物路径必须仍在 Job 目录内的既有校验不变。
- polished Job 的润色草稿失效后，`continue` 不再静默重跑全量翻译和润色。恢复计划在「polished 链已开始但 `translation_draft_cues` 缺失或 checksum 失效」时给出 `repair_polish_draft` 步骤，但执行侧此前不拦截，直接落到 `run_translation`；当翻译 checkpoint 也已失效时，这一步会真实地重新调用提供方翻译全文再重新润色，用户只会看到 `continue` 跑了很久，看不到自己被计费了一整轮。现在两侧共用同一个判定：只有 `translation_progress` 的 fingerprint 与当前配置一致、覆盖全部批次且每个批次的 cue 都有译文时（即逐批循环会全部命中续跑分支、零次提供方调用）才放行，否则 `continue` 以明确的人工修复文案停下，要求重新导入或生成原始译文草稿。`status` 的分类同步为同一谓词：可零成本重建时归为 `retryable` 并推荐 `continue`，此前一律报 `manual_fix`，与 `continue` 的实际行为相反。谓词本身无副作用，provider 无法解析（Agent 模式或缺少 base URL / model / API key）时按不可零成本重建处理，`status` 不会因环境变量不全而失败。
- 高级原子 `xvt burn` / `xvt preview` 补上 fail-closed 的译文字幕 QA 门。此前 `preview` 只检查报告 `status` 是否为 `pass` / `warning`，`burn` 完全不读报告，因此绕开端到端链路直接调用原子命令，可以把 `failed`、未确认的 `needs_review` 甚至 checksum 已失效的译文烧进视频。现在两者共用与 `complete_qa_stage` 同口径的放行谓词：`pass` / `warning` 直接放行；`needs_review` 仅当报告内全部 review 级问题都落在对当前源 cue 仍然有效的确认集合内才放行；出现 error 级问题、报告或译文 cue 缺失、checksum 失效、报告结构异常一律拒绝。合法的 `needs_review` 放行仍走既有 `xvt continue JOB --acknowledge-subtitle-review` 通道，没有新增逃生口。`burn` 的门在远程媒体下载之前执行，注定被拒的 Job 不再先白下载整段视频。恢复指引按 Job 类型分叉：端到端 Job 维持 `status` / `continue` 与确认命令；原子 Job（无持久化 `config.workflow`）上 `xvt continue` 必然报错，文案改为说明该命令不适用，并引导重跑 `xvt translate JOB --target-language LANG` 重新生成译文与 QA 报告，同时指出 `needs_review` 的确认只能在 `xvt run` 创建的端到端 Job 上完成。
- `xvt preview` 缺少 checksum 有效的译文 cue 或译文 QA 报告时改为 `RuntimeError`，退出码由 2（`invalid_input`）变为 4（`workflow`），`cli_failed` 的 error category 同步为 `workflow`。缺 artifact 属于 Job 状态问题而不是用户输入错误，也与 `burn` 共用同一道门后的分类保持一致。这是一次刻意的契约变更，已由 CLI 级测试锁定退出码与 category。
- 强制 `--align-subtitles` 现在同样受对齐后字幕 QA 的 error 门约束。此前只有自动对齐路径会因 error 级问题回退，显式请求对齐时无论对齐后 QA 结果如何都直接采用对齐时间轴，可能把超出媒体边界一类 error 级问题带进下游。`--allow-low-confidence-alignment` 语义不变，仍只放行低置信度对齐，不放行 error 级问题。失败文案同时修正：`--align-subtitles` 必须与 `--subtitles` 同用，去掉该参数后走的是「保留源字幕原时间轴」而不是 ASR 分段，需要 ASR 时间轴必须显式 `--source-strategy asr`（该策略不能与 `--align-subtitles` 同用），或先修好源字幕。
- `xvt resume` 对远程 Job 不再把 URL 当成本地路径。此前 `Path("https://host/...")` 会被折叠成 `https:/host/...` 并直接交给 ffmpeg，远程 Job 的转写恢复必然失败。现在优先复用 checksum 有效的 `source_video` / `downloaded_media`；没有可复用媒体时委派远程恢复路径重新获取，因此这条路径可能触发一次媒体下载（按 Job 已持久化的 Cookie 配置执行）。本地输入的处理不变。
- Agent 标准翻译 manifest 新增 `source_cues_checksum` 绑定，`apply-translation` 与统一入口 `xvt apply` 会拒绝源 cue 在 `prepare-translation` 之后发生变化的陈旧响应，避免按新 cue 集合错位回填；修复前 prepare 的旧 manifest 没有该字段，仍按原样接受。同时收紧已登记 manifest 的可信度：`translation_batches` 从未登记的极旧 Job 维持接受，但已登记却 checksum 失效、文件缺失或内容不是 JSON 对象时改为拒绝并引导重新 `prepare-translation`——此前这三种情况一律被当成「没有 manifest」放行，可疑状态会让 `task` 路由静默退化成标准翻译并绕过上述源 cue 绑定。收紧只作用于回填这一写路径，`status` 等只读查询不受影响，manifest 损坏不会让查询命令失败。同一次回填还把源 cue 的读取从「按登记路径直接读」改为要求 checksum 有效：`source-cues.json` 被改动或损坏时立即拒绝，而不是把响应回填到与 manifest 已经对不上的 cue 上；响应解析也改为复用与 `prepare-translation` 同一份实现，不再维护第二套内联校验。
- 语义校对链的翻译策略回退口径统一。`workflow` 配置没有快照 `translation_policy` 时，批次构建侧一直回退到 `job.config["translation_policy"]`，但修复链的校验侧（修复链复用判定与 Agent 语义响应回填）直接读快照值并落到默认空策略，重建出的修复批次与已落盘批次逐字节不一致，导致复用判定恒为 false、语义校对永不收敛并重复消耗 API 预算。现在两侧共用同一份回退实现。等待中的 Agent 语义 manifest 的批次内容与 fingerprint 不受影响，在途响应仍可原样回填，无需重新 prepare。
- 润色、翻译、语义校对等提供方请求在读取响应时遇到分块传输中途断开不再直接抛出 `http.client.IncompleteRead` 堆栈：`IncompleteRead`、`BadStatusLine` 和 `LineTooLong` 归类为可重试传输故障，进入既有的最多 4 次退避重试；其余 `http.client.HTTPException` 属于客户端连接状态错误，包装为不可重试的提供方依赖故障。`HTTPException` 既不是 `OSError` 也不是 `ValueError`，此前既不被提供方边界捕获、也不被 CLI 顶层兜底覆盖，因而输出裸 traceback 并返回退出码 1；现在统一以退出码 3 和结构化恢复计划结束，Job 逐批进度不受影响，仍可 `continue` 续跑。`RemoteDisconnected` 保持既有连接故障归类不变。
- 同源修复 TLS 传输故障的分类，两个阶段此前各错一半：请求阶段 `urlopen` 会把全部 `SSLError` 包装成 `URLError`，而该分支无条件可重试，因此证书校验失败、握手失败等重发必然同样失败的问题会被空跑满 4 次重试预算；响应读取阶段不经过该包装，`SSLError` 又因不属于 `ConnectionError` 被判为不可重试，使 `SSLEOFError`（未经正常关闭即断开）、`SSLZeroReturnError`（传输中途关闭）和 `SSLSyscallError`（底层 I/O 中断）这类瞬时故障直接失败。现在两个阶段共用同一张 TLS 分类表：上述三类进入退避重试，其余 `SSLError`（含 `SSLCertVerificationError`）立即失败。`SSLCertVerificationError` 同时继承 `ValueError`，由提供方边界包装可确保它按依赖故障退出，不会被 CLI 顶层误判为用户输入错误。
- 局部语义复查保留稳定批次 ID 供 checkpoint/事件消费，同时新增本轮 `batch_index` 供人类进度显示，不再出现「批次 38/1」一类误导输出；`xvt apply ... --continue` 在继续工作流前不再打印回填阶段的旧产物路径或恢复建议，最终只展示继续后的真实终态。
- 字幕 QA 的纯 `needs_review` 现在持久化为 `awaiting_input`，所有原子译文入口与端到端命令都不会发出 `cli_completed`；error 级问题仍记为 `failed`。恢复导航会展示真正阻断的候选 QA 报告，且仅 QA 阻断时不再提供必然失败的语义确认命令。已登记的译文 QA 报告缺失或 checksum 失效时，只要源 QA 无 error 且译文 cue 有效，`continue` 会重新计算 QA，不再误用源报告形成永久阻断。
- 人工平台字幕候选失败时尝试对齐后再回退 ASR，自动候选在翻译/烧录目标中不再直接决定时间轴。仅平台最后一个 cue 超出媒体不超过 500ms 时安全裁剪。
- 翻译提供方连接在 `urlopen` 或响应读取阶段抛出的 `RemoteDisconnected`、连接重置等 `ConnectionError` 统一归类为可重试传输故障；其他 `OSError` 与 401/403 等不可重试 HTTP 错误仍立即失败。
- 翻译重试拆分为独立预算：传输故障默认最多重试 4 次并遵循最长 30 秒的有效 `Retry-After` / 指数退避，结构性无效响应固定只做 1 次携带纠正提示的重试；传输重试不再误带结构纠正提示。主翻译与局部重译共用同一重试实现和 retry 事件语义。
- 译文 QA `needs_review` 确认后续跑不再漏写 `translated.srt` / `bilingual.srt`：翻译满足度要求 cue 与两份 SRT 均有效；已有 `translated_cues` 时经 `complete_translated_outputs` 只补 QA+渲染，不重打翻译 API。`plan_advance_steps` 新增 `render` 步骤。
- 语义复核确认后的译文收尾会将 `translate` stage 正确收口为 `succeeded`，清除此前 QA 阻断留下的旧错误，避免字幕 QA、媒体 QA 和烧录均完成后恢复计划仍显示译文失败。
- 字幕 QA `needs_review` 经 `--acknowledge-subtitle-review` 确认后，`continue` 会把语义复核腿留在 `translate` stage 上的过期阻断错误收口为 `succeeded`，旧 Job 下次 `continue` 自动修正，不再出现 stage 显示 `failed` 而恢复计划显示 complete 的矛盾状态。收口要求 QA 门已放行、确认记录对当前源 cue 仍有效且译文 cue 与两份 SRT 均 checksum 有效，不会绕过 error 级 QA 问题。
- 烧录恢复原因文案拆分：媒体 QA 已通过但烧录成品缺失或 checksum 已失效时，不再复用「烧录尚未通过媒体 QA」误导文案，改为提示成品已不在登记路径或内容已变化，避免与 `verify` 的 `artifact file is missing` 诊断矛盾；媒体 QA 未通过与指纹失效两种文案保持不变。
- `needs_review` 恢复引导：`status` / recovery / `continue` 入口闸区分 review 与 error；review 给出 `xvt continue JOB --acknowledge-subtitle-review`，不再笼统要求「修字幕」。

### Changed

- 兼容 API 翻译默认批次从 40 调整为 25 个 cue，请求默认超时从 60 秒调整为 180 秒，以降低 DeepSeek 等远程服务在大批次下的断连和超时概率；旧 Job 继续使用其持久化批次与超时配置。

- `cli.py` 的 `_run_command`（~622 行 if/elif 链）拆分为 26 个独立 `_cmd_<name>` 函数 + `_COMMAND_DISPATCH` 字典分发表；纯搬移，不改变任何命令的行为和输出。
- `pipeline` 包内确实被跨模块引用的 `_` 前缀符号去掉下划线并公有化（如 `set_stage`、`complete_qa_stage`、`translate_batch_with_retries`、`artifact_checksum_for`、`write_json`、`load_cues`、`checksum` 等）；纯重命名，不保留旧名别名，不改变任何行为。
- 烧录与预览输出路径安全统一到 `xvt.paths`：允许写到 Job 目录外的绝对路径，但写入前拒绝覆盖源媒体、`job.json` 与除「正在写入的同 kind」外的已登记 artifact（含 hardlink/`samefile`）；同 kind 重烧/重预览允许。冲突抛 `UserInputError`，且烧录在 RENDER/BURN 记账前校验、不把路径错误记成 stage failed。
- ASR / `asr_resegmented` 来源的 `low_timing_confidence` 由 `review` 降为 `warning`，避免端到端几乎必卡人工确认；`aligned` 与用户导入字幕仍为 `review`。硬显示超限（`line_too_wide` / `bilingual_overflow_risk`）与跨门 display_width 确认族映射不变。
- YouTube/远程视频下载格式选择改为 `bv*+ba/b/18/best`（分离流失败时可回退渐进式 360p 与 best）；HTTP 403 时给出可操作提示（`continue` 重试、更新 yt-dlp、显式 `--cookies-from-browser`），仍不默认读取浏览器 Cookie。
- CLI 人类可读进度打磨：计划性跳过（translate/burn/media_qa）不再逐条刷屏，其余跳过合并为一行；同一 stage 连续失败去重；`acquire` / 源·译文 QA·渲染标签区分；翻译重试行显示尝试序号与等待秒数；API 翻译准备批次不再误报「等待」；`status` 将可确认的 QA 显示为 `needs_review`；完成摘要按视频/字幕/其他分类，并显示总耗时。stage 事件可选 `detail` 字段见 `docs/EVENTS.md`。
- 局部重译（`retranslate-cues`）解析翻译策略的口径与译文 QA 统一：两侧现在共用 `pipeline.translation.resolved_translation_policy`（workflow 策略快照优先，顶层 `config.translation_policy` 是原子 Job 的回退位置）。此前重译 manifest 只读顶层配置，而译文 QA 的术语校验读 workflow 快照，同一语义存在两份实现。绝大多数端到端 Job 上两处内容一致，行为不变；唯一会分叉的形态是在已有 workflow 目标的 Job 上再显式跑 `xvt translate --glossary NEW` 覆盖顶层配置——此时重译提示词现在跟随 workflow 快照，与随后回填时的 QA 判定使用同一份术语表和风格，不会再出现「按 A 表翻译、按 B 表判定」。Agent 润色 manifest 的同型解析一并收敛到该函数。另外，`xvt apply` 收到 `task: retranslate_cues` 的响应时不再报「unknown task」，改为直接给出 `xvt apply-cue-retranslation JOB RESPONSE --cue ID [--cue ID ...] --target-language LANG` 这条必须手工执行的命令（该子命令需要 `xvt apply` 拿不到的 cue ID 与目标语言）；错误类别与退出码不变，仍是 `invalid_input` 与 2。

## [0.1.1] - 2026-07-24

### Changed

- 版本号 `0.1.0` → `0.1.1`（文档与包元数据同步；无 Job schema / 事件协议破坏性变更）。
- CLI 默认路径收口：`xvt --help` 增加 recommended path epilog；`run` / `status` / `continue` 文案强调日常导航；`resume`、`translate`、`burn`、`preview`、重分段/局部重译等标为 `[advanced]`（不改命令语义）。README 与 AGENTS 同步「run → status → continue」。

- 烧录派生指纹迁到 artifact：新增 `burn_fingerprint` kind 与 `burn-fingerprint.json`，依赖图挂在 `burned_video` 下；`_record_burn_fingerprint` 写 artifact 并清除 `workflow.burn_fingerprint` 旁路；`_burn_satisfied` / recovery 优先读 checksum 有效的 artifact，旧 Job 仍回退 workflow 字段；`clear_burn_fingerprint` 同时移除 artifact 登记与历史键。

- `status` / recovery 与 continue 共用推进判定：端到端 Job 在源字幕就绪且字幕 QA 未失败时，通过 `plan_advance_steps` 生成恢复建议；`RecoveryPlan` 新增 `advance_steps`（JSON `recovery_plan` 与人类可读 `steps:` 行），烧录路径补上「派生指纹失效」可续跑。原子 Job（无 `config.workflow`）仍走独立 `translate` / `apply-translation` 入口。

- 端到端推进抽出 Goals 与可测试计划：`workflow_goals` / `plan_advance_steps` 用与执行路径相同的满足度判定（`translated_cues`、烧录指纹与媒体 QA）列出剩余步骤（`translate` / `import_translation` / `await_agent_translation` / `burn`）；`_advance_workflow` 在 Agent 等待、翻译、烧录上复用同一套判定，并在翻译副作用后重新检查烧录是否仍需执行。`planned_run_steps` 统一 `run` 首跑的 `WORKFLOW_PLANNED` 步骤列表。行为与原先 imperative 分支一致（仍以 `target_language` / `burn` 为权威目标）。

- 端到端 `job.config["workflow"]` 收敛为类型化 `WorkflowConfig`（`src/xvt/workflow_config.py`）：`from_dict`/`to_dict` 校验已知字段类型与默认值；未知顶层字段进入 `extras` 并在写回时保留，避免指纹更新抹掉新版本扩展；`requested_outputs` 保留未知输出名以维持前向兼容。`WorkflowOptions.persisted_config` 返回结构体，落盘边界仍序列化为既有 JSON 形状。`workflow`、`recovery`、`pipeline` 与 CLI 提示统一经 `get_workflow_config` / `require_workflow_config` / `resolve_target_language` / `clear_burn_fingerprint` 读写，消除对 dict 袋的散落 `config.get` 与目标语言双处解析。纯内部收敛，旧 Job 可继续加载；已知字段类型错误在边界显式失败。

- `pipeline.py`（2385 行单体）拆分为 `src/xvt/pipeline/` 包，纯搬移、零行为变化：`common`（共享记账与失效 helper）、`review`（QA 确认通道）、`transcription`（转写与恢复主链）、`translation`（翻译主链与统一收尾）、`retranslate`（局部重译）、`resegment`（语义重分段）、`burnin`（预览与烧录），`__init__` 仅保留 re-export 门面与 `__all__` 公共 API 清单；依赖方向无环（`common ← review ← {transcription, translation}`，`retranslate ← translation`，`resegment`/`burnin` 仅依赖 `common`）。测试同簇拆分为 `test_pipeline_{transcription,translation,retranslate,resegment,burnin}.py`，全部 mock patch 目标随调用点迁移到对应子模块命名空间；三处源码扫描守卫测试（artifact kind 覆盖、config 默认值、事件裸字符串/信封）改为递归扫描以覆盖包内子模块。

- artifact 依赖图与失效闭包收敛到单一来源（`src/xvt/artifacts.py`）：新增 `ArtifactKind` 枚举登记全部产物种类、`DERIVES_FROM` 依赖边表和 `downstream_of` 传递闭包函数；`pipeline.py` 中 `_invalidate_rendered_artifacts`、`_invalidate_preview_artifacts` 和 `resegment_translation` 的三处失效集合改由闭包推导，不再各自维护硬编码集合。新增测试将三处历史集合钉为基线并校验依赖图无环，行为与现状完全一致。
- workflow 配置默认值收敛到单一来源（`src/xvt/config.py`）：新增纯常量模块集中定义 `DEFAULT_ENGINE`、`DEFAULT_SEGMENTATION_PRESET`、`DEFAULT_PROVIDER`、`DEFAULT_BATCH_SIZE`、`DEFAULT_MAX_RETRIES`、`DEFAULT_TIMEOUT_SECONDS`、`DEFAULT_API_KEY_ENV`、`DEFAULT_TRANSLATION_STYLE`、`DEFAULT_MIN_ALIGNMENT_CONFIDENCE`、`DEFAULT_HARDWARE_ENCODING`；`cli.py` 各子命令参数默认值、`WorkflowOptions` 字段默认值以及 `pipeline.py`/`workflow.py` 读取持久化 config 的手写兜底改引常量。纯内部收敛，无行为或数值变化。
- 集中定义错误模型与退出码（`src/xvt/errors.py`）：新增 `ExitCode` 枚举和 `XvtError` 领域异常层次（`UserInputError`、`DependencyError`、`InternalError`）；顶层异常映射优先识别领域异常并沿用其退出码与 category，旧的按 Python 异常类型兜底映射保持不变。
- **Breaking**：拆分冲突的退出码语义。`verify` 校验未通过由 `4` 改为 `5`（VERIFY_FAILED），`batch`/`batch-continue` 存在失败条目由 `4` 改为 `6`（PARTIAL_FAILURE）；`doctor` 未通过仍为 `3`，中断仍为 `130`。依赖脚本请同步更新退出码判断。

### Added

- 翻译进度可预估：`translation_batch_started` / `translation_batch_retrying` / `translation_batch_completed` 事件新增可选字段 `batch_total`（批次总数，v1 兼容性变更不升协议版本）；CLI 人类可读输出的翻译批次行改为「批次 N/总数」，重试行同时显示事件里已有的 `reason` 字段（已知原因在渲染层映射为中文，契约字符串保持英文，未知原因回退原文）；旧事件流缺少 `batch_total` 时回退为仅显示批次 ID。
- `xvt continue --acknowledge-subtitle-review`：人工确认 `needs_review` 的字幕 QA 报告后继续翻译与烧录（导入字幕折叠后超宽等场景）。确认绑定字幕 checksum 与具体 review 问题集合、字幕变化或新问题自动失效、error 级永不放行；放行以 `subtitle_review_acknowledged` 事件与 config 记录留痕，原始 QA 报告保持不变。

- Job schema 前向兼容与迁移链骨架：`Artifact` 和 stage record 解析改为按已知字段过滤构造，未知字段静默忽略，避免旧版本 CLI 读取新版本 `job.json` 时因新增字段崩溃；新增模块级 `_MIGRATIONS` 迁移链骨架（当前为空），供未来结构性 schema 变更登记逐版本迁移函数。
- JSONL 事件协议 v1 地基：新增 `EVENT_PROTOCOL_VERSION` 与 `EventType` 枚举作为全部事件类型的单一事实来源，事件信封新增 `v` 版本字段，消费端改引枚举成员；新增 `job_event_fields` / `error_event_fields` 信封辅助函数，并以 `docs/EVENTS.md` 记录协议版本、兼容性承诺和全部事件字段契约。
- 生产端 v1 事件迁移全量完成（P3b/P3c）：`pipeline.py`、`workflow.py`、`batch.py`、`process.py`、`download.py`、`transcribe.py`、`burn.py` 的全部 `create_event` 调用改引 `EventType` 枚举成员，`command_failed` 统一接入 `error_event_fields` 错误信封；`create_event` 签名收紧为仅接受 `EventType`，删除裸字符串兼容分支；新增测试扫描源码防止裸字符串调用回归。
- `transcription_progress` 结构化事件：faster-whisper 使用词级分段提供真实百分比和 ETA，MLX Whisper 提供运行时长心跳。
- 超宽 ASR 片段会在翻译前按句末、软标点、连接词或介词等语义优先边界切分，并保持词级时间轴连续。
- `xvt resegment-translation`：对超过硬显示阈值的 ASR 译文 cue 按语义边界重切、重建源字幕并使翻译及下游产物失效，随后按新 cue ID 重新翻译。
- `translate` 和 `prepare-translation` 支持 JSON 术语表及 `faithful`、`natural`、`concise` 翻译风格；策略会持久化到 Job 并进入 API 翻译指纹。
- `xvt run` 支持同一套翻译策略；端到端 Job 保存策略快照，`xvt continue` 可在原术语表文件不可用时保持相同翻译约束。
- 译文 QA 对术语表执行确定性检查；违规以 `glossary_violation` 定位到 cue，并阻断渲染和烧录。
- `retranslate-cues` 和 `apply-cue-retranslation` 支持仅修复指定 cue；保留其他译文和时间轴，重跑 QA 并使旧渲染、烧录产物失效。
- `xvt preview` 支持按 cue 或起始时间生成短片段双语预览，复用正式 ASS 样式和烧录参数，并独立登记 checksum artifact。
- `xvt run --segmentation-preset` 支持 `conservative`、`balanced` 和 `compact` 三种 ASR 分段预设；选择会持久化到 Job 和端到端工作流，`resume` 可保持一致。
- `xvt run source.srt` 支持 SRT、VTT、ASS、SSA 纯字幕 Job：保留用户时间轴，无需媒体即可翻译、QA、`continue` 和导出原文、译文、双语 SRT。
- `xvt batch` 和 `xvt batch-continue`：固定目录输入清单，串行创建独立 Job，隔离单项失败，并通过原子 `batch.json` 汇总报告恢复未完成任务。
- 外部命令以独立进程组执行：超时、取消或中断时清理全部子孙进程，不再遗留 ffmpeg/yt-dlp 孤儿进程；提供静默超时机制（默认不启用，由保证持续输出的调用方显式设置——yt-dlp 提取阶段等合法静默场景不受误杀），并新增协作式取消入口（`CommandCancelled`）。
- CLI 改为命令级信号作用域：进入 `main()` 复位取消状态、退出恢复原信号处理器（同进程多次调用互不污染）；第二次 Ctrl-C/SIGTERM 不再产生裸 traceback，只保持取消状态直至清理完成。
- CLI 现在处理 SIGTERM：与 Ctrl-C 一样清理进程组、持久化中断的 stage 并生成恢复计划，退出码为 143；翻译批次循环和批处理条目在迭代边界响应取消。
- Job 与 batch 全生命周期独占执行：flock 锁自资源首次持久化点持有至终态——既有 Job 子命令与 `batch-continue` 在 CLI 入口、在途 `run` 自 Job 创建起、`batch` 自 batch.json 写出前；第二个进程以退出码 7 立即失败并提示持有者 PID；进程崩溃时内核自动释放锁。含真实跨进程冲突测试。

- stage 跳过决策收敛到 `stages.py` 计划表：入口在业务逻辑开始前一次性标记跳过项（先计划后执行），取代散落在各分支的逐个记账；中途失败时计划内的跳过状态已持久化。
- run 首跑与 `continue` 续跑统一为同一推进函数：转写完成后的翻译、Agent 等待与烧录决策只有一份实现，首跑烧录同样走指纹感知路径；`workflow_completed` 与等待事件的 artifacts 统一为 checksum 有效的用户产物。
- 翻译收尾统一为单一 `_finalize_translation`：局部重译、Agent/API 响应回填和导入译文字幕三条路径共用同一段「写译文 cue → 失效下游 → 译文 QA → 渲染」逻辑，消除三份重复骨架，无行为变化。

### Fixed

- 再加固事件脱敏（P7h）：敏感头命中后一律吞到行尾（不再解析引号/转义，堵住转义引号 Digest 泄漏）；URL userinfo 放宽为 @ 前一切（覆盖无用户名、无冒号形态）；事件 data 中命中敏感 key（Cookie/Authorization/api_key/password/token 等）的字典值整体清空，覆盖嵌套 Header 字典。
- 加固事件自由文本脱敏（P7g）：Cookie 等敏感头的值改为吞到闭引号或行尾（覆盖多 Cookie 对、Digest 后续字段、字典形态 Header），并新增任意协议 URL userinfo 脱敏（socks5:// 等）；非 UTF-8 的 provider 响应体纳入 `ProviderResponseError` 边界；batch 条目取 Job 锁失败时不再于无锁状态下重读恢复计划。
- 修复事件自由文本脱敏缺口：外部命令的原始 stderr/stdout 进入事件错误消息时，`Authorization`/`Proxy-Authorization`/`Cookie`/`Set-Cookie`/`X-Api-Key` 头与 `Bearer` 令牌现在会在事件边界统一脱敏（此前只脱敏 URL，凭据可能原样进入 JSONL）。
- Job 锁提前到首次持久化之前：`create_job` 在写出 `job.json` 前即持有目录锁，消除「文件已出现、锁尚未取得」的并发窗口；batch 对已有 Job 的恢复分类与 artifact 校验同样改为先取 Job 锁再读取，避免按旧状态误判条目完成。
- provider 响应解析边界统一包装为 `ProviderResponseError`：HTTP 200 非 JSON 响应体、模型输出 JSON 语法错误、cue ID 缺失/重复/多余、译文为空等结构性错误在纠错重试耗尽后按依赖故障退出码 3 分类，不再被误报为用户输入错误退出码 2。
- 重分段后旧 `subtitle_qa` 报告一并失效：它派生自 source cue 且不会被立即重建，此前以 checksum 有效姿态残留，污染 `status`/`verify` 与恢复复用清单。
- 修复 P2 引入的翻译重试回归：结构性无效的 provider 响应（缺 `items`、报文不完整、内容非文本）改抛专用 `ProviderResponseError`，重新进入携带纠正提示词的纠错重试；重试耗尽后按依赖故障分类（退出码 3）。`TranslationProviderError` 一并归入 `DependencyError` 层次。
- 修复媒体 QA 失败但 `burned_video` checksum 有效时，恢复计划、批处理或续跑可能错误宣告任务完成的问题。
- 修复仅生成源字幕时 `workflow_completed` 和 `cli_completed` 没有返回 `source.srt` 的问题。
- 损坏或字段不完整的当前版本 Job 现在返回可控的无效输入错误，不再泄漏 Python traceback。
- 修复转写失败或缺少词级时间戳时可能误报完成进度的问题。
- 修复预览未继承工作流烧录媒体、字体和编码设置，以及自定义输出可能覆盖 Job、源媒体或正式 artifact 的问题；预览中断现在提供独立重试提示。
- 修复 `resegment-translation` 失效集合遗留 `cue_retranslation_batches`/`cue_retranslation_response`（P6a 为保持行为而排除）的问题：这两类 artifact 在重分段后引用的是废弃 cue id，虽然 `apply-cue-retranslation` 会因 `translated_cues` 已失效而拒绝执行、无正确性漏洞，但会以 checksum 有效的姿态污染 `status`、`verify` 和恢复计划的可复用产物清单；现按依赖图闭包一并失效（P7e）。
- 修复 P7c 终审发现的 v1 事件信封失配：`cli.py` 的 `cli_failed` 顶层裸 `category`/`message` 改为嵌套 `error_event_fields` 信封；`pipeline.py` 中 `remote_subtitles_selected`、`translation_resegmented`、`translation_fallback`、`translation_progress_reset`、`translation_batch_started`、`translation_batch_retrying`（两处）、`translation_batch_completed`、`translation_provider_selected`、`preview_completed` 共 10 处补齐缺失的 `job_file`；`batch.py` 的 `_mark_unfinished_item` 补齐 `batch_item_failed`/`batch_item_waiting` 缺失的 `index`/`total`。`docs/EVENTS.md` 同步对账，新增载荷结构契约测试（`tests/test_events.py`）防止回归。

## [0.1.0] - 2026-07-15

### Added

- 本地视频、音频和 YouTube URL 的转写与字幕导入流程。
- MLX Whisper 与 faster-whisper 转写适配器，支持词级时间戳和基于停顿、标点、长度的分段。
- SRT、VTT、ASS、SSA 解析，已有字幕时间轴保留和可选 Whisper 对齐。
- YouTube 人工字幕优先选择，可显式回退自动字幕或 Whisper。
- OpenAI-compatible Chat Completions 翻译与 Agent 翻译协议。
- 翻译断点续跑、输入与配置指纹、请求超时、429/5xx 退避与错误分类。
- 原文、译文、双语 SRT，独立中英样式 ASS，以及 FFmpeg 烧录。
- 字幕 QA、媒体 QA、`job.json` 持久化、checksum 校验、恢复和 JSONL 事件。
- 统一 `xvt run` 端到端编排与 `xvt continue` 目标恢复，覆盖 API 中断、Agent 等待和烧录重试。
- 远程检查或下载失败后，可在同一个 Job 中重新进入素材获取阶段，并复用 checksum 有效的远程产物。
- yt-dlp 下载与 FFmpeg 烧录的结构化实时进度事件。

### Changed

- 双语输出会折叠每种语言内部的源字幕换行，固定为译文和原文各一行。
- 英文分段默认上限调整为 64 个显示单位。横版英文 65 到 72 个显示单位仅警告，超过 72 才需要复核，窄画幅自动收紧。
- macOS 默认中文字幕字体使用可被 libass 读取的 `Hiragino Sans GB`。
- 远程烧录工作流前置下载完整视频，ASR 和烧录复用同一文件。

### Fixed

- 修复远程字幕作业中不必要的音频下载和 ASR。
- 修复 VideoToolbox 对不兼容质量参数的烧录失败，保留精确的软件编码回退。
- 修复字幕内部换行被 ASS 原样保留，导致英文额外换行的问题。
- 修复外部命令事件可能记录代理凭据、Cookie、鉴权头或签名 URL 的问题。
- 修复远程元数据检查未使用显式 Cookie 来源和代理的问题。
