# 恢复与回填契约

## 人工译文预检与布局确认

已知画幅但测量失败时仍保存请求画幅、字体样式及 `measurement=unavailable`；成功时为 `measured` 并保存宽度。回退字数规则不会丢失确认上下文，因此另一画幅或字体的旧确认也不能复用。

`xvt apply JOB REVIEW --check` 和 `xvt apply-translation-review-fixes JOB REVIEW --check` 仅检查人工译文 JSON/SRT，不写 Job、artifact、事件日志或锁文件，不调用模型。返回 `translation_review_preflight`，包括 `valid`、`changed_ids`、`blockers` 和完整 `qa`；无效退出 2。统一入口仅允许译文修订路由使用该旗标，拒绝与 `--continue` 合用。正式提交重新执行相同检查，译文超宽或 error 未解决时不保存、不失效旧产物。

已知画幅时，候选及最终译文 QA 按实际 ASS 样式测量可见宽度；建议字数超限但能容纳的降为 warning。测量失败保留原阻断规则。报告可包含 `layout`（画幅、样式、宽度和可用宽度），subtitle acknowledgment 绑定相同布局；旧无 layout 的确认仅匹配旧无 layout 的报告。换画幅、字体或测量结果后需重新判断，不能用旧确认放行新布局。恢复/status 仍严格只读，旧候选报告在实际推进时重算；烧录重查实际媒体与字体，新的 review 返回 workflow 等待态，不宣告完成。预览不改写正式 QA。

本文是 `status` / `continue` / `batch` / `batch-continue` / `xvt apply` / `relink-burned-output` 共用恢复语义的单一来源。判定顺序、产物有效性口径、回填路由和烧录 proof 资格都以这里为准，其它文档不得另立说法。

模块职责、artifact 依赖图与失效闭包见 [架构说明](ARCHITECTURE.md)；事件名与字段见 [事件说明](EVENTS.md)。

## 远程字幕获取的访问方式

显式请求 YouTube 字幕并传入浏览器 Cookie 时，缺少匹配人工字幕会触发一次匿名补查。发现同一视频的匹配人工字幕后，`remote_info` 以 `subtitles_anonymous=true` 保存并重新登记 checksum；后续字幕下载匿名执行、忽略 yt-dlp 本机配置，显式代理仍保留，媒体访问参数不变。字幕下载中断后，重新进入 acquire 会从有效 `remote_info` 恢复匿名访问方式；旧元信息缺少该字段时默认使用原访问参数。补查失败会使 inspect 失败，必须显式重试，不得当成字幕缺失继续 ASR。已生成的源 cue、译文和烧录产物不会因补查规则更新而自动失效或替换。

有效的已下载字幕、已复制用户字幕或显式用户源字幕配置会阻止补查：保留候选的原来源分类，不用新查到的人工字幕语言列表把旧自动字幕重标为人工字幕。

## 批处理恢复分类与中断兜底

### 中断兜底

批次首次落盘后的 `batch_started` 事件必须位于批处理的中断兜底内。任何中断都要留下 `interrupted` 与 `next_action`，不允许出现「批次已落盘但事件流里看不出被中断」的状态。

### 等待项绝不自动推进

- 已登记烧录成品缺失、但具备重新关联资格的 Job 会停在 `awaiting_input`，**绝不由批处理自动重烧**。用户须先显式 `relink-burned-output`，或明确选择 `continue` 重烧。
- 未回填的局部重译（`retranslate-cues`）同样是等待项，条目记为 `awaiting_input` 而不是已完成。
- 通则：等待项绝不由批处理自动推进。

### 已完成条目的产物失效分类

**非目标产物**（`normalized_audio`、`word_transcript`、批次 manifest 等中间 checkpoint）失效只做标注，不改写终态：

- `item["verification"].status == "degraded"`
- `batch_item_completed.verification`

**目标产物**失效仍按各自路径处理：

- `burned_video`：走 relink / 重烧的 `awaiting_input`。
- `bilingual_ass`：已经在 `plan_advance_steps` 的译文满足度判定内（登记过就必须有效），失效时条目的恢复分类根本不是 `complete`，直接落到 `continue`，由 `render` 零 API 成本重建。
- `source_srt`：不在 `plan_advance_steps` 判定内，但在纯转写批次上就是交付物，条目落到 `continue` 由 `resume_job` 真正重建，不得当作「非目标产物」放行。

### 输出与退出码

- 普通模式下 `batch` / `batch-continue` 会逐条打印需要处理的条目及其恢复命令。数据取自**已持久化的 `recovery_plan`**，不重算。
- 两者同样支持 `--fail-on-waiting`：无失败条目、有等待条目时返回 10；有失败条目仍返回 6（**6 优先于 10**）。

## 端到端推进计划的判定顺序

### 译文满足度

译文满足度包含 SRT、译文 QA 报告与**已登记过**的 `bilingual_ass`。渲染侧只对有正整数宽高的媒体写 ASS，因此从未登记过 ASS 的 Job 不要求它。缺渲染、QA 报告或 ASS 时走 `complete_translated_outputs`。

### 判定顺序

1. `plan_advance_steps` 在**函数体最前**判定局部重译等待（`await_cue_retranslation`）。理由：它会叠加在一个译文目标已满足的 Job 上，放在后面就会被「已满足」短路掉。
2. 在 `TRANSLATE` 等输入且 `translation_batches` checksum 有效时，早退为 `await_agent_translation`。这一早退**必须排在 `translated_cues` → `render` 之前**：新整轮请求不会失效旧译文，按 render 先判会把等待误报成「只差渲染」。

## 翻译提供方身份的固化

### 固化时机

- 端到端 Job 的 `config.workflow.base_url` / `model` 允许在 `run` 时为 null，由环境变量补齐。两者都进入 API 翻译进度指纹，因此 `continue` 若在另一个环境变量不同的终端里执行，指纹会漂移、批次进度清零，还可能拿过期模型名去撞 4xx。
- openai-compatible 模式在**首个翻译批次成功后**（或全部批次都命中 checkpoint、指纹按这套身份校验通过时）把实际生效的 `base_url` / `model` 写回 `config.workflow`，与该批次的 `save_job` 一并落盘。此后 `continue` / `status` 读到非 null 值，解析提供方与指纹都不再受进程环境影响。
- `xvt translate` 在端到端 Job 上不传 `--base-url` / `--model` 时回退到 Job 固化值（与 `polish-translation` 同口径），显式传入则覆盖并按同一时机写回，保证 `translate` 与 `continue` / `status` 看到同一套身份。

### 失败不写回的原因

- 写回必须排在首个批次成功**之后**：先写回再请求，环境变量里的过期模型名会先被钉进 `job.json`，而 `continue` 没有 `--model`，用户修好环境变量也没有入口再改它。首批 4xx 后字段保持 null，修好环境变量即可直接 `continue`。
- 指纹早在解析时已按同一份 provider 配置算出，推迟写回不影响一致性。
- Agent 降级不写回（半套配置会误导下一次 `continue`）；原子 Job 没有 `config.workflow`，不写。
- `api_key` 永不落盘，仍只读当前终端的环境变量。Job 已固化身份、当前终端只缺 API Key 时，auto 模式的降级原因会点名 Job 钉住的 `model` / `base_url` 与缺失的环境变量，不静默降级。
- 换模型的途径：重新 `run`，或 `xvt translate JOB --target-language LANG --model NEW`，或直接编辑 `job.json`。

## 产物有效性校验与 checksum 记忆化

### 与 continue 同一套校验

`status` 的产物有效性判定与 `continue` 使用**同一套全量 checksum 校验**，只是同一次判定内对同一文件只哈希一次：进程内、单次调用作用域内的记忆化，不跨命令、不落盘。

### 已知且被接受的边界

记忆化的身份键含 `st_mtime_ns`，由此有一条已知且被接受的边界：在同一个只读作用域执行期间（毫秒级）把文件改成等长内容并把 mtime 恢复原值，会复用旧哈希——与 git 的 racy-timestamp 取舍同类。

`build_recovery_plan` 本身只读，但 `batch` 会把 plan 及其完成分类写入 `batch.json`，所以该窗口里的误判**可能持久化**。接受它的理由：

- 命中需要外部写入者绕过 Job 锁、精确插入首次哈希后的极短窗口。
- 无缓存时「计划返回后、落盘前」仍有更宽的同类 TOCTOU。
- `xvt verify JOB` 会在作用域外重新哈希并识破。

### 元数据只能证伪，不得证实

除上述记忆化窗口外，size、mtime 等文件元数据只能用于**证伪**（元数据与登记值不符即判失效），不得用于**证实**有效——`cp -p`、`rsync -a` 和备份还原都会保留 mtime。

既有成例同此界线：

- `add_artifact` 写后立即哈希登记。
- `_burn_fingerprint` 的 size+mtime 只参与身份指纹，不承担有效性判定。

## `xvt apply` 统一回填路由

`xvt apply JOB RESPONSE` 是统一回填入口，但路由规则依响应类型而不同。

### 路由规则

- **人工修订模板**（`source-review-*.json` / `translation-review-*.json`，CLI 生成、用户编辑后原样提交）携带 `task` 字段，按该字段路由。
- **人工修订 SRT**（固定命名 `translation-review-<fp12>.srt`）按文件名识别为修订通道，不与 `import-translation` 的字幕输入混淆。
- **Agent 翻译/润色/语义校对响应**契约（见 `translation-protocol.md`）不含 `task` 字段，按 Job 当前等待步骤路由——仅支持有持久化 `config.workflow` 目标的端到端 Job。
- **原子 Job**（无 `config.workflow`，例如单独 `xvt translate` 创建的等待）不受统一入口支持。路由不出来时**报错而不猜测**，需改用对应的显式 `apply-*` 命令。

### `--continue` 与 `--fail-on-waiting`

译文相关响应可加 `--continue` 在回填后继续同一工作流。`--fail-on-waiting` 透传给这次续跑：停在下一个等待态时返回 10；不加 `--continue` 时该旗标无作用。

源文修订会失效译文及下游，必须先独立回填并查看 `status`，因此**拒绝 `--continue` 组合**。

### 推荐命令

- 端到端 Job 上的人工 translation review：`xvt apply JOB FILE --continue`（回填并续跑同一工作流）。
- 原子 Job（无 `config.workflow`）拿不到 `--continue`，推荐命令退回 `xvt apply-translation-review-fixes JOB FILE`。
- 人工 source review 的推荐命令仍是 `xvt apply-source-review-fixes JOB FILE` 加一次显式 `status`，保持两步。

各 `apply-*` 命令保留为稳定入口，语义零改动。

### 刻意不纳入路由的命令

需要额外参数的 `apply-cue-retranslation` 与输入非 JSON 的 `import-translation` 不纳入统一路由。

局部重译现在是 `plan_advance_steps` 的一个正式等待步骤（`await_cue_retranslation` ∈ `AWAIT_STEPS`），但仍刻意不进 `_STEP_ROUTES`——`apply-cue-retranslation` 必须显式收 `--cue` 与 `--target-language`，而 `xvt apply` 的命令行里没有这两个参数、也不能从响应里安全地推出来。

理由显式登记在 `cli._UNROUTABLE_AWAIT_STEPS`。**新增等待步骤必须在两张表（`_STEP_ROUTES` / `_UNROUTABLE_AWAIT_STEPS`）之一里表态**，守卫测试强制。

## 烧录 proof 与成品重新关联资格

### proof 的签发

- `xvt burn JOB` 是独立烧录入口（advanced）。该原子命令可覆盖持久化 workflow 之外的媒体、字体和编码参数，因此**不签发 workflow v2 proof**。
- 只有 `run` / `continue` 按持久化目标完成的烧录，才登记 checksum 保护的 `version=2` `burn_fingerprint` proof，绑定 fingerprint、`burned_video` checksum 和实际源媒体内容 checksum。
- 旧 / legacy fingerprint 可按既有轻量完成语义读取，但**不能用于 relink**。

### relink 的匹配条件

`xvt relink-burned-output JOB EXISTING_VIDEO` 是已移动烧录成品的唯一重新关联入口（advanced）。仅接受 checksum 有效的 v2 proof，且以下必须**全部匹配**：

- 重算的源媒体内容 checksum
- 原 `burned_video` checksum
- 当前 fingerprint
- 媒体 QA
- 候选文件 SHA-256

重复 `burned_video` 一律 fail closed。

### 不具备资格的情形

legacy proof、媒体 QA、proof 或 fingerprint 无效的 Job 都不具备重新关联资格，按 `retryable` 恢复并由 `continue` 重烧。旧 Job 必须先 `continue` 重烧一次生成 v2 proof，不得宣称可直接 relink。

### status 的成本边界

普通 `status` 不会为 v2 proof 额外计算源媒体内容 checksum；内容 checksum 只在烧录落证和 relink 校验时计算。

`xvt status JOB` 对缺失的已登记烧录成品只做静态资格判断，不额外计算源媒体内容 checksum；符合资格时归类为 `awaiting_input`，优先给出 `relink-burned-output`，并列出显式 `continue` 重烧作为备选。
