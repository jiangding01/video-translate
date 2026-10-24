# XVT JSONL 事件契约

本文件是 XVT JSONL 事件协议的目标契约。规划中的可视化校对界面只消费 JSONL 事件、`job.json` 和 `batch.json`，因此事件结构必须稳定、可自解释。

## 协议版本与兼容性承诺

- 当前协议版本：**v1**（`src/xvt/events.py` 中的 `EVENT_PROTOCOL_VERSION = 1`）。
- 每条事件都携带信封字段 `v`（整数），标明产出该事件时的协议版本。
- **加字段不升版本**：向既有事件 `data` 增加新的可选字段属于向后兼容变更，不改变 `v`。消费端必须容忍未知字段。
- **加事件类型不升版本**：新增事件类型属于向后兼容变更；消费端必须忽略未知事件类型。
- **删除或修改字段必须升版本**：删除字段、重命名字段、改变字段类型或语义都属于破坏性变更，必须递增 `EVENT_PROTOCOL_VERSION` 并在此处记录迁移说明。
- 事件类型的单一事实来源是 `EventType`（`StrEnum`）。生产端与消费端都应引用枚举成员，不写裸字符串。

## 事件信封结构

每行 JSONL 是一个对象：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `v` | int | 协议版本，当前恒为 `1` |
| `type` | string | 事件类型，取值见下方各分组，对应 `EventType` 成员值 |
| `timestamp` | string | 事件产出时刻，ISO 8601（UTC） |
| `data` | object | 事件载荷，字段随 `type` 而定，见下方各表 |

## v1 信封约定

- **关联 Job 的事件应同时携带 `job_id` 与 `job_file`**：两键始终存在（无值时为 `null`），便于界面稳定关联 Job。辅助函数 `job_event_fields(job_id, job_file)` 提供该形态。
- **失败类事件的错误载荷统一为 `error: {category, message}`**：辅助函数 `error_event_fields(category, message)` 提供该形态。
- 所有 URL、鉴权头、Cookie、签名查询参数在写入事件前统一脱敏。

> `pipeline/`、`workflow.py`、`batch.py`（P3b）与 `process.py`、`download.py`、`transcribe.py`、`burn.py`（P3c）均已全面对齐本文件描述的 v1 信封目标契约：全部 `create_event` 调用引用 `EventType` 枚举成员，携带错误信息的失败类事件统一使用 `error_event_fields` 信封。经 P7c 对账修复 `cli.py`（`cli_failed` 顶层裸 `category`/`message` 改为嵌套 `error`）、原 `pipeline.py`（补齐 10 处遗漏的 `job_file`）、`batch.py`（`_mark_unfinished_item` 补齐 `index`/`total`）后，生产端 v1 迁移已核实全量完成，无已知残留失配。

---

## 工作流生命周期

端到端编排（`workflow.py`）。

### `workflow_started`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string? | 关联 Job ID（Job 尚未创建时为 `null`） |
| `job_file` | string? | 关联 Job 文件路径（Job 尚未创建时为 `null`） |
| `continuation` | bool? | 是否为继续既有工作流（`continue` 场景为 `true`） |

### `workflow_planned`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `steps` | string[] | 计划执行的步骤名列表，如 `["transcribe", "translate", "burn"]` |

只有 `xvt run` 首跑会发这条事件。`continue` 不重发计划，只发带 `continuation=true` 的 `workflow_started`；它的剩余步骤看 `status` / `recovery_plan` 的 `advance_steps`。

### `workflow_waiting`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | 关联 Job ID |
| `job_file` | string | 关联 Job 文件路径 |
| `reason` | string | 等待原因，自由文本，与恢复计划（`recovery_plan`）的 `reason` 一致；消费方不应解析 |
| `artifacts` | string[] | 相关产物路径列表 |

### `workflow_completed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | 关联 Job ID |
| `job_file` | string | 关联 Job 文件路径 |
| `artifacts` | string[] | 产出的用户产物路径列表 |

### `workflow_failed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string? | 关联 Job ID（未创建 Job 时为 `null`） |
| `job_file` | string? | 关联 Job 文件路径（未创建 Job 时为 `null`） |
| `error` | object | `{category, message}`，`category` 为 `XvtError.category`（无该属性时默认 `"workflow"`） |

## Job 生命周期

单个 Job 主流程（`pipeline/`）。

### `job_started`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `input` | string | 输入源（本地路径或 URL，已脱敏） |

### `job_completed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |

### `job_resumed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |

## stage 推进

stage 状态迁移（`pipeline/common.py`），由 `StageStatus → EventType` 映射表按状态派发四种类型。

### `stage_started` / `stage_waiting` / `stage_completed` / `stage_failed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `stage` | string | stage 名（`StageName` 值） |
| `status` | string | stage 状态（`StageStatus` 值） |
| `detail` | string? | 可选人类可读细分，如 `acquire` 的 `subtitles`/`media`，或 QA/渲染的 `source`/`translated` |
| `phase` | string? | 可选业务子阶段：`draft`、`polish`、`semantic_review` 或 `repair` |
| `error` | object? | 仅 `stage_failed` 且记录了失败信息时存在，`{category, message}` |

`detail` 与 `phase` **实际上只挂在 `stage_started` 上**（`detail` 的全部调用点都是 `StageStatus.RUNNING`；`phase` 另有少量收尾调用点带它，例如一次成型润色的 `stage_completed`）。收尾事件不带 `detail` 是既有行为，本文档不承诺改变它；消费端要把「哪一侧的 QA / 渲染」关联到收尾事件，得自己按 `(job_id, stage)` 记住上一条 `stage_started` 的 `detail`——CLI 的人读输出就是这么做的。

## artifact 登记

产物登记与失效（`pipeline/common.py` 与各业务子模块）。

### `artifact_created`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `kind` | string | 产物类型 |
| `path` | string | 产物路径 |

`run` / `continue` 按持久化目标完成烧录后登记的 `version=2` burn proof 也走这条事件
（`kind=burn_fingerprint`）。它是 `relink-burned-output` 重新关联已移动成品的唯一授权凭据，
因此与其它产物一样在事件流里可见；高级原子 `xvt burn` 不签发该 proof，也就没有这条事件。

### `artifact_invalidated`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `kind` | string | 产物类型 |
| `path` | string | 产物路径 |

### `subtitle_review_acknowledged`

用户显式确认 `needs_review` 的字幕 QA 报告（`continue --acknowledge-subtitle-review`），或工作流复用仍然有效的既有确认记录放行同一批 review 问题。原始 QA 报告不会被改写；界面应据此显示「用户已确认风险」而非「QA 自然通过」。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `review_count` | int | 被确认的 review 级问题数量 |
| `source` | string | `user_flag`（本次显式确认）或 `stored_acknowledgment`（复用既有确认） |

### `translation_review_acknowledged`

用户显式确认当前 `polished` 译文的语义校对 review 问题。确认记录绑定 source、候选和报告 checksum、prompt version 与精确问题集合；内容变化后不能复用。该事件不代表确定性字幕 QA 已通过。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `review_count` | int | 被确认的语义 review 问题数量 |
| `source` | string | 当前为 `user_flag` |

### `translation_review_input_ready`

语义校对仍需人工处理时，可编辑的译文修订模板已经准备完成。模板不是 artifact；事件只公布路径和数量，不包含字幕正文。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `path` | string | 当前修订模板路径（JSON 载体） |
| `scope` | string | 当前固定为 `translation` |
| `item_count` | int | 模板涉及的 cue 数量 |

同一目录下还有同名 `.srt`（把 `path` 的后缀换成 `.srt`），是同一修订通道的人工载体，供用字幕工具直接改译文；两条载体 fingerprint 相同、回填结果等价。事件只公布 JSON 路径，SRT 路径由消费方按后缀推导，`xvt status` 会把它一并列出。

### `translation_review_revised`

用户修订已通过严格校验并保存为新的完整译文候选。事件不包含字幕正文。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `scope` | string | 当前固定为 `translation` |
| `item_count` | int | 本次提交修改的 cue 数量 |
| `checksum` | string | 新候选 artifact checksum |

### `source_review_input_ready`

指定源 cue 的可编辑修订模板已经准备完成。模板不是 artifact；事件只公布路径和数量，不包含字幕正文。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `path` | string | 当前源文修订模板路径 |
| `scope` | string | 当前固定为 `source` |
| `item_count` | int | 模板涉及的 cue 数量 |

### `source_review_revised`

用户源文修订已通过严格校验，确定性源字幕 QA 已执行，并保存为新的完整 source cue 候选。事件不包含字幕正文。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `scope` | string | 当前固定为 `source` |
| `item_count` | int | 本次提交修改的 cue 数量 |
| `checksum` | string | 新 source cue artifact checksum |
| `qa_status` | string | 确定性源字幕 QA 状态 |

## 下载

### `media_download_selection`

Cookie YouTube 完整视频实际下载并校验成功后，pipeline 登记可选诊断摘要时发出。`data` 包含公共 Job 字段及 `summary_path: string`。摘要 `version=1` 包含视频 ID、`access`（`configured` / `anonymous`）、固定格式 ID、实际尺寸与时长、音轨语言、`original_audio`、选择原因与媒体 checksum；无 Cookie、签名 URL 或原始诊断日志。摘要缺失、损坏或 checksum 不匹配时不发该事件，也不影响媒体有效性。

yt-dlp 下载进度（`download.py`）。

### `download_progress`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `downloaded_bytes` | int | 已下载字节数 |
| `total_bytes` | int? | 总字节数（未知时为 `null`） |
| `percent` | float? | 进度百分比（未知时为 `null`） |
| `speed` | number? | 下载速度（字节/秒） |
| `eta_seconds` | number? | 预计剩余秒数 |

### `download_space_insufficient`
下载**之前**的容量预检判定不通过，整单以 `DependencyError`（退出码 3）失败，一个字节都不下载。
只在能从 yt-dlp 探测拿到所选格式登记大小时判定；拿不到大小一律跳过预检、不发本事件（只做证伪，不猜）。
`required_bytes` 含合并峰值：video / audio 分离（`part_count > 1`）时要先各自落盘再 mux，
mux 完才删中间文件，所以按两份算；单文件格式按一份算。按约束 5 不写路径。
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `required_bytes` | int | 预计需要的字节数（含分离格式的合并峰值） |
| `available_bytes` | int | 目标目录所在文件系统的可用字节数 |
| `format_id` | string | 所选 yt-dlp 格式 ID |
| `part_count` | int | 实际要下载的格式数：`>1` 表示分离后需 mux |

## 转写

音频转写（`transcribe.py`）与远端字幕选择（`pipeline/transcription/remote.py`）。

### `transcription_progress`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `engine` | string | 转写引擎名（如 `Whisper`） |
| `determinate` | bool | 是否有确定进度 |
| `processed_ms` | int? | 已处理音频毫秒数 |
| `total_ms` | int? | 音频总毫秒数 |
| `percent` | float? | 进度百分比 |
| `elapsed_seconds` | float | 已耗时秒数 |
| `eta_seconds` | float? | 预计剩余秒数 |

### `transcription_fallback`
请求的 ASR 后端没有安装时发出。回退本身保留（否则只装了一种后端的用户会被彻底卡住），但它改变了实际生效的引擎：`words.json` 与 `transcription_progress` 里的 `engine` 会是 `effective_engine`，`job.config["engine"]` 也会被改写成它，用户原本请求的引擎只留在本事件里。该事件不带 `job_id` / `job_file`：它由 `transcribe.py` 这个零 Job 依赖的叶子模块发出。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `requested_engine` | string | 用户请求的引擎（`mlx` 或 `faster`） |
| `effective_engine` | string | 实际生效的引擎 |
| `reason` | string | 回退原因（后端缺失的报错原文） |

### `remote_subtitles_selected`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `language` | string | 选中的字幕语言 |
| `automatic` | bool | 是否为平台自动生成字幕 |
| `anonymous` | bool | 是否使用匿名补查得到的人工字幕及匿名下载；旧事件可能缺少该字段 |

### `source_selected`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `strategy` | string | `auto`、`subtitles` 或 `asr` |
| `candidate_kind` | string | `none`、`user`、`manual` 或 `automatic` |
| `selected_kind` | string | 最终时间轴来源（`subtitles`、`aligned_subtitles` 或 `asr`） |
| `reason` | string | 稳定选择原因码 |
| `report_path` | string | `source-selection.json` 路径 |

`rebuild-source --strategy asr` 复用此事件记录重建后的选择（`strategy=asr`、`selected_kind=asr`）；不新增事件类型。命令成功后的 CLI JSONL 终态仍为既有 `cli_completed`。

## 翻译

翻译提供方选择、回退、批次进度（`pipeline/translation.py`、`polish.py`、`semantic.py`）。

### `translation_provider_selected`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `provider` | string | 提供方模式（如 `agent` 或兼容 API 名） |
| `model` | string | 使用的模型名 |
| `phase` | string? | 可选业务子阶段：`draft`、`polish`、`semantic_review` 或 `repair` |

### `translation_fallback`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `mode` | string | 回退后的模式（`agent`） |
| `reason` | string | 回退原因 |
| `path` | string | 生成的翻译批次文件路径 |
| `phase` | string? | 可选业务子阶段；润色回退为 `polish`，语义校对回退为 `semantic_review` |

### `translation_progress_reset`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `reason` | string | 重置原因（如翻译指纹变化） |
| `phase` | string? | 可选业务子阶段：`draft`、`polish`、`semantic_review` 或 `repair` |

### `translation_resegmented`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `source_cue_count` | int | 原始 cue 数量 |
| `resegmented_cue_count` | int | 重新分段后 cue 数量 |
| `report_path` | string | 重分段报告路径 |

### `translation_import_dropped`

`xvt drop-imported-translation` 显式放弃导入译文时发出，紧跟在被失效 artifact 的 `artifact_invalidated` 之后。只记录「放弃了哪一份」，不删除任何文件。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `kind` | string | 被放弃的 artifact kind，恒为 `input_translation_subtitles` |
| `path` | string? | Job 内被放弃副本的路径；该登记原本就不存在时为 `null`（文件保留在原处） |
| `checksum` | string? | 该副本此刻的内容 SHA-256；文件已不在时为 `null` |
| `registered_checksum` | string? | Job 原先登记的 checksum；与上一字段不等即说明文件在登记后被改过 |
| `translation_subtitles` | string? | 被清掉的 `config.workflow.translation_subtitles`；原本为空或原子 Job 时为 `null` |
| `invalidated_kinds` | string[] | 本次移出登记的全部 artifact kind（已排序） |

### `translation_batch_started`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `batch` | int | 批次 ID |
| `batch_total` | int? | 批次总数（v1 兼容性新增可选字段，旧事件流可能缺失） |
| `batch_index` | int? | 当前批次在本次执行集合中的 1-based 顺序；人类进度显示优先使用，旧事件流可能缺失 |
| `phase` | string? | 可选业务子阶段：`draft`、`polish`、`semantic_review` 或 `repair` |

### `translation_batch_retrying`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `batch` | int | 批次 ID |
| `batch_total` | int? | 批次总数（v1 兼容性新增可选字段，旧事件流可能缺失） |
| `batch_index` | int? | 当前批次在本次执行集合中的 1-based 顺序；人类进度显示优先使用，旧事件流可能缺失 |
| `attempt` | int | 当前批次即将进行的请求序号；传输重试与结构纠错共用连续序号 |
| `delay_seconds` | number | 重试前等待秒数；生产端当前限制在 0 到 30 秒。指数退避带抖动，因此该值可以是两位小数（例如 `6.37`），不承诺是整数 |
| `reason` | string | 重试原因；当前生产端使用 `provider temporarily unavailable` 或 `invalid provider response` |
| `phase` | string? | 可选业务子阶段：`draft`、`polish`、`semantic_review` 或 `repair` |

传输重试和结构纠错使用独立预算。默认传输故障最多重试 4 次，结构性无效响应固定最多纠错 2 次；因此 `attempt` 只表示可见请求顺序，不表示任一预算的剩余次数。

### `translation_batch_completed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `batch` | int | 批次 ID |
| `batch_total` | int? | 批次总数（v1 兼容性新增可选字段，旧事件流可能缺失） |
| `batch_index` | int? | 当前批次在本次执行集合中的 1-based 顺序；人类进度显示优先使用，旧事件流可能缺失 |
| `phase` | string? | 可选业务子阶段：`draft`、`polish`、`semantic_review` 或 `repair` |

## 烧录与预览

FFmpeg 烧录进度（`burn.py`）与预览片段（`pipeline/burnin.py`）。

### `burn_progress`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `out_time_ms` | int | 已输出时长毫秒数 |
| `duration_ms` | int? | 总时长毫秒数 |
| `percent` | float? | 进度百分比 |
| `frame` | string? | 当前帧号 |
| `speed` | string? | 编码速度 |
| `state` | string | FFmpeg progress 状态 |

### `preview_started`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `start_ms` | int | 预览起点毫秒 |
| `duration_ms` | int | 预览时长毫秒 |

### `preview_progress`
与 `burn_progress` 字段相同（复用烧录进度处理，`progress_event_type="preview_progress"`）。

### `preview_completed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `path` | string | 预览产物路径 |
| `start_ms` | int | 预览起点毫秒 |
| `duration_ms` | int | 预览时长毫秒 |

## 批处理

目录批处理编排（`batch.py`）。

### `batch_started`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `batch_file` | string | `batch.json` 路径 |
| `total` | int | 批处理项总数 |

该事件在 `batch.json` 首次持久化后发送，并位于批处理中断兜底内。若发送期间收到 SIGINT 或 SIGTERM，批次会持久化为 `interrupted` 并写入 `next_action`；此时尚无条目开始，因此不会发送 `batch_item_interrupted`。

### `batch_item_started`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `batch_file` | string | `batch.json` 路径 |
| `input` | string | 该项输入路径 |
| `index` | int | 当前项序号 |
| `total` | int | 批处理项总数 |

### `batch_item_completed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `batch_file` | string | `batch.json` 路径 |
| `input` | string | 该项输入路径 |
| `job_file` | string | 该项 Job 文件路径 |
| `index` | int | 当前项序号 |
| `total` | int | 批处理项总数 |
| `reused` | bool? | 是否复用已完成的 Job（仅复用路径设置） |
| `verification` | object? | 已完成条目的产物校验标注：`null` 表示全部登记产物 checksum 有效；否则为 `{"status": "degraded", "invalid_artifacts": [kind, ...]}`，表示用户交付物全部有效、仅列出的中间 checkpoint 已失效。degraded **不是失败**，条目仍是 `completed`，批处理退出码不受影响。语义边界：`bilingual_ass` 已经是「译文目标是否满足」的判定条件之一（对渲染过 ASS 的 Job），它失效时条目的恢复分类不再是 `complete`，因此**不会**出现在 `invalid_artifacts` 里，也不会以 `batch_item_completed` 收尾——该条目落到 `continue` 由 `render` 重建 |

### `batch_item_waiting`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `batch_file` | string | `batch.json` 路径 |
| `input` | string | 该项输入路径 |
| `job_file` | string? | 该项 Job 文件路径 |
| `index` | int | 当前项序号 |
| `total` | int | 批处理项总数 |
| `recovery_plan` | object? | 恢复计划（含 `reason`、`prerequisite_commands`、`primary_command` 等） |

### `batch_item_failed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `batch_file` | string | `batch.json` 路径 |
| `input` | string | 该项输入路径 |
| `job_file` | string? | 该项 Job 文件路径 |
| `index` | int | 当前项序号 |
| `total` | int | 批处理项总数 |
| `error` | object? | 异常路径下存在，`{category, message}`；恢复计划判定为不可恢复/需人工处理时可能缺省 |
| `recovery_plan` | object? | 恢复计划 |

### `batch_item_interrupted`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `batch_file` | string | `batch.json` 路径 |
| `input` | string | 该项输入路径 |
| `job_file` | string? | 该项 Job 文件路径 |
| `index` | int | 当前项序号 |
| `total` | int | 批处理项总数 |
| `next_action` | string | 继续批处理的建议命令 |

### `batch_finished`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `batch_file` | string | `batch.json` 路径 |
| `status` | string | 批处理终态（如 `completed`、`partial_failure`） |
| `total` | int | 项总数 |
| `completed` | int | 完成数 |
| `awaiting_input` | int | 等待输入数 |
| `failed` | int | 失败数 |
| `next_action` | string | 继续批处理的建议命令 |

### `batch_message`
内部辅助事件，用于对批处理项消息统一脱敏，一般不直接对外发送。
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `message` | string | 脱敏后的消息文本 |

### `batch_error`
内部辅助事件，仅用于复用 `create_event` 的脱敏逻辑生成 `item["error"]` 文本，不通过 `event_sink` 直接对外发送。
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `error` | object | `{category, message}`，`message` 为脱敏后的错误信息 |

## 外部命令

无 shell 外部进程执行（`process.py`），命令参数已脱敏。

### `command_started`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `command` | string[] | 脱敏后的命令参数数组 |

### `command_completed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `command` | string[] | 脱敏后的命令参数数组 |
| `exit_code` | int | 退出码（恒为 `0`） |

### `command_failed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `command` | string[] | 脱敏后的命令参数数组 |
| `exit_code` | int | 退出码（`127` 表示命令不存在，其余为实际返回码） |
| `error` | object | `{category, message}`，`category` 恒为 `"dependency_or_process"`；`message` 为命令不存在时的异常信息，或命令返回非零退出码时的 `stderr`/`stdout` 摘要（均已脱敏） |

## CLI 终态

CLI 层最终结果（`cli.py`），写入 stdout。

`run` / `continue` / `apply --continue` / `batch` / `batch-continue` 的 `--fail-on-waiting` **只改变进程退出码**（等待态返回 10）：不产生 `cli_failed`，不改 Job / batch 状态，事件流与不加旗标时逐条相同。消费端不要把退出码 10 与任何失败事件关联；按「没有 `cli_completed`」或 `batch_finished.status` 判断等待态的既有做法继续有效。

### `cli_completed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `command` | string | 执行的子命令名 |
| ... | any | 随命令附加的结果字段（如产物路径） |

### `cli_failed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `command` | string | 执行的子命令名 |
| `error` | object | `{category, message}`，`category` 为失败类别（如 `interrupted`、`invalid_input`、`dependency_or_process`、`workflow` 等），`message` 为失败信息 |
| `next_action` | string? | 建议的后续命令（预览、批处理等场景） |
| `batch_file` | string? | 关联 `batch.json` 路径（批处理场景） |
| `recovery_plan` | object? | 恢复计划 |
