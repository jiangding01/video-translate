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

> `pipeline.py`、`workflow.py`、`batch.py`（P3b）与 `process.py`、`download.py`、`transcribe.py`、`burn.py`（P3c）均已全面对齐本文件描述的 v1 信封目标契约：全部 `create_event` 调用引用 `EventType` 枚举成员，携带错误信息的失败类事件统一使用 `error_event_fields` 信封。经 P7c 对账修复 `cli.py`（`cli_failed` 顶层裸 `category`/`message` 改为嵌套 `error`）、`pipeline.py`（补齐 10 处遗漏的 `job_file`）、`batch.py`（`_mark_unfinished_item` 补齐 `index`/`total`）后，生产端 v1 迁移已核实全量完成，无已知残留失配。

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

### `workflow_waiting`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | 关联 Job ID |
| `job_file` | string | 关联 Job 文件路径 |
| `reason` | string | 等待原因（如需要人工翻译输入） |
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

单个 Job 主流程（`pipeline.py`）。

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

stage 状态迁移（`pipeline.py`），由 `StageStatus → EventType` 映射表按状态派发四种类型。

### `stage_started` / `stage_waiting` / `stage_completed` / `stage_failed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `stage` | string | stage 名（`StageName` 值） |
| `status` | string | stage 状态（`StageStatus` 值） |
| `detail` | string? | 可选人类可读细分，如 `acquire` 的 `subtitles`/`media`，或 QA/渲染的 `source`/`translated` |
| `error` | object? | 仅 `stage_failed` 且记录了失败信息时存在，`{category, message}` |

## artifact 登记

产物登记与失效（`pipeline.py`）。

### `artifact_created`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `kind` | string | 产物类型 |
| `path` | string | 产物路径 |

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

## 下载

yt-dlp 下载进度（`download.py`）。

### `download_progress`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `downloaded_bytes` | int | 已下载字节数 |
| `total_bytes` | int? | 总字节数（未知时为 `null`） |
| `percent` | float? | 进度百分比（未知时为 `null`） |
| `speed` | number? | 下载速度（字节/秒） |
| `eta_seconds` | number? | 预计剩余秒数 |

## 转写

音频转写（`transcribe.py`）与远端字幕选择（`pipeline.py`）。

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

### `remote_subtitles_selected`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `language` | string | 选中的字幕语言 |
| `automatic` | bool | 是否为自动选择（无用户指定语言时为 `true`） |

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

翻译提供方选择、回退、批次进度（`pipeline.py`）。

### `translation_provider_selected`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `provider` | string | 提供方模式（如 `agent` 或兼容 API 名） |
| `model` | string | 使用的模型名 |

### `translation_fallback`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `mode` | string | 回退后的模式（`agent`） |
| `reason` | string | 回退原因 |
| `path` | string | 生成的翻译批次文件路径 |

### `translation_progress_reset`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `reason` | string | 重置原因（如翻译指纹变化） |

### `translation_resegmented`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `source_cue_count` | int | 原始 cue 数量 |
| `resegmented_cue_count` | int | 重新分段后 cue 数量 |
| `report_path` | string | 重分段报告路径 |

### `translation_batch_started`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `batch` | int | 批次 ID |
| `batch_total` | int? | 批次总数（v1 兼容性新增可选字段，旧事件流可能缺失） |

### `translation_batch_retrying`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `batch` | int | 批次 ID |
| `batch_total` | int? | 批次总数（v1 兼容性新增可选字段，旧事件流可能缺失） |
| `attempt` | int | 当前批次即将进行的请求序号；传输重试与结构纠错共用连续序号 |
| `delay_seconds` | number | 重试前等待秒数；生产端当前限制在 0 到 30 秒 |
| `reason` | string | 重试原因；当前生产端使用 `provider temporarily unavailable` 或 `invalid provider response` |

传输重试和结构纠错使用独立预算。默认传输故障最多重试 4 次，结构性无效响应固定最多纠错 1 次；因此 `attempt` 只表示可见请求顺序，不表示任一预算的剩余次数。

### `translation_batch_completed`
| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `job_id` | string | Job ID |
| `job_file` | string | Job 文件路径 |
| `batch` | int | 批次 ID |
| `batch_total` | int? | 批次总数（v1 兼容性新增可选字段，旧事件流可能缺失） |

## 烧录与预览

FFmpeg 烧录进度（`burn.py`）与预览片段（`pipeline.py`）。

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
