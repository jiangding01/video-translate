# R2.4 烧录成品重新关联技术方案

## 目标

解决烧录成品被用户移动或替换后，`status` 持续推荐 `continue`、
`batch-continue` 可能自动触发整段重新编码的问题，同时保持以下事实边界：

- 只有当前路径上的 checksum 有效文件才能作为可复用 artifact。
- 文件缺失不能自动推断为“用户已经另行保存”。
- 媒体 QA 失败或烧录派生指纹失效时不得绕过重新烧录。
- Job、artifact、checksum 和事件仍能解释当前完成状态。

## 结论

采用**基于 v2 burn proof 与原 artifact checksum 的路径重新关联**，不采用“缺失即完成”或盲目确认。

新增命令：

```text
xvt relink-burned-output JOB_FILE EXISTING_VIDEO [--events-jsonl]
```

命令只读取候选视频并更新 Job 中既有 `burned_video` artifact 的路径，不复制、移动、
删除或重编码任何媒体。重新关联成功后，既有 `_burn_satisfied` 判定自然恢复为真，
`status`、`verify`、`continue` 和 `batch-continue` 无需建立第二套完成语义。由 `run` / `continue`
按持久化 workflow 目标完成的烧录必须写入 `version=2` burn proof；仅有效 v2 proof 可授权
重新关联。允许临时覆盖媒体、字体或编码参数的高级原子 `xvt burn` 不签发 workflow proof。

## 为什么不使用确认记录

`continue --acknowledge-missing-burned` 只能证明用户做出了声明，不能证明成品仍存在。
它还会产生以下额外状态：

- Job 显示完成，但 `verify` 仍因登记文件缺失而失败。
- 需要持久化确认记录、失效绑定和撤销或强制重烧入口。
- 批处理必须为“已确认但缺失”的 artifact 建立特殊有效性规则。

直接把缺失文件判为 `complete-with-exported-artifact` 同样无法区分正常移动、误删和复制失败。
重新关联使用已登记 checksum 验证字节完全一致，保留单一 artifact 真相。

## 核心校验

重新关联在修改 Job 前完成全部检查：

1. Job 必须存在持久化 workflow，且 `burn=true`。
2. Job 必须已经登记带非空 checksum 的 `burned_video` artifact。
3. `media_qa` stage 必须为 `succeeded`，checksum 有效的 `media_qa` artifact 内容必须为
   `status=pass`。
4. 当前 `translated_cues` artifact 必须 checksum 有效。
5. `burn_fingerprint` artifact 必须是 checksum 有效的 `version=2` burn proof。proof 必须同时
   绑定当前配置重算出的 fingerprint、原 `burned_video` checksum 和实际烧录源媒体的内容
   checksum；重新关联时重新计算实际源媒体内容 checksum 并逐项比对。
6. `burned_video` 登记必须唯一；重复登记不能安全地选择 proof 绑定对象，直接 fail closed。
7. 候选路径必须是本机现存普通文件，其 SHA-256 必须与原 `burned_video` checksum 完全一致。

任一检查失败时抛出用户输入错误，Job 和 artifact 登记保持不变。成功时只替换
`burned_video.path`，保留原 checksum，原子保存 Job；JSONL 复用既有
`artifact_invalidated` 与 `artifact_created` 事件表达路径切换。

旧/legacy fingerprint（包括仅保存在 `workflow.burn_fingerprint` 的记录，或缺少 v2 绑定字段的
artifact）仍按既有轻量完成语义读取，避免改变旧 Job 的正常 `status` / `continue` 行为；它们
绝不能授权 relink。用户必须运行 `continue` 重新烧录一次，生成 v2 proof 后才可重新关联。
普通 `status` 不会为 v2 proof 额外计算源媒体内容 checksum；仅烧录落证和 relink 校验计算它。

## 恢复语义

R2.4 两阶段均已完成：第一阶段提供显式重新关联命令；第二阶段调整恢复导航：

| 场景 | recovery category | 建议 |
| --- | --- | --- |
| 成品、媒体 QA、派生指纹/proof 均有效 | `complete` | `verify` |
| 媒体 QA、当前派生指纹和有效 v2 proof 均有效，但登记成品缺失或 checksum 失效 | `awaiting_input` | 首选重新关联占位命令，备选显式 `continue` 重烧；只表示可能可关联，执行仍校验实际源媒体内容 |
| 登记成品缺失或 checksum 失效，但只有 legacy fingerprint | `retryable` | `continue` 重烧生成 v2 proof；不能推荐 relink |
| 媒体 QA 未通过 | `retryable` | `continue` 重烧并复查 |
| 派生指纹/proof 失效 | `retryable` | `continue` 按当前输入重烧 |
| 从未登记烧录成品 | `retryable` | `continue` 完成首次烧录 |

`batch-continue` 已将 `awaiting_input` 作为停止态处理，因此不会自动覆盖或
重新编码可能已被用户移动、替换的外部成品。

## 兼容性与事件

- 不升级 Job schema，不新增 config 字段、artifact kind、stage 或依赖；在既有
  `burn-fingerprint.json` 中加入 `version=2` 和绑定字段是向后兼容的 artifact 内容演进。
- 不新增事件类型；路径切换复用现有 artifact 事件，事件不包含媒体内容。
- 旧版本读取重新关联后的 Job 时只会看到新的绝对 artifact 路径，保持完全兼容。
- 回滚不需要数据迁移；候选文件仍存在时可再次重新关联到其他同 checksum 路径。
- legacy fingerprint 的完成读取兼容不等于 relink 兼容：旧 Job 需要显式重烧，不能直接重新关联。

## 实施拆分

### 第一阶段：重新关联能力

- `src/xvt/workflow.py`：集中写入 v2 burn proof，校验 proof 绑定、唯一 `burned_video` 与原子 artifact 路径替换。
- `src/xvt/cli.py`：增加命令、普通输出和 JSONL 完成输出。
- `tests/test_workflow.py`：覆盖成功、checksum 不匹配、媒体 QA/proof/源媒体绑定无效、legacy proof 拒绝、重复 `burned_video` 拒绝及失败不改 Job。
- `tests/test_cli.py`：覆盖参数路由、普通输出和 JSONL 终态。
- README / CHANGELOG：记录命令与边界。

第一阶段独立可用：用户可显式运行命令恢复 artifact 路径和完整完成态。

### 第二阶段：恢复导航与批处理（已完成）

- `src/xvt/recovery.py`：精确区分可重新关联、媒体 QA 失败和指纹失效。
- `tests/test_workflow.py`：用真实 v2 proof Job 固定恢复分类、命令优先级和不额外 hash 源媒体的边界。
- `tests/test_cli.py`：固定普通与 JSON `status` 的首选/备选命令输出。
- `tests/test_batch.py`：证明 `batch-continue` 不自动重烧可重新关联的成品。
- ROADMAP、HANDOFF 与架构方案：记录评估结论和完成状态。

第二阶段只增加可发现性和安全默认值；第一阶段能力不依赖它。

## 验收矩阵

- 端到端 workflow 烧录：写入 checksum 有效的 `version=2` proof，绑定当前 fingerprint、`burned_video` checksum 和实际源媒体内容 checksum；高级原子 `xvt burn` 不签发该 proof。
- 正确候选文件：只更新 artifact 路径，checksum 不变，`verify` 与 `status` 通过。
- 错误候选文件：拒绝且 Job 字节内容不变。
- 媒体 QA 失败、报告缺失或报告 checksum 失效：拒绝。
- 当前烧录 fingerprint、proof 内容或实际源媒体内容 checksum 不匹配：拒绝，不允许旧成品覆盖新目标。
- legacy fingerprint / proof：不得 relink；`continue` 重烧一次后才可使用 v2 proof 重新关联。
- 重复 `burned_video`：拒绝且 Job 字节内容不变。
- 普通 `status`：不为 v2 proof 额外计算源媒体内容 checksum；烧录落证和 relink 校验才计算。
- JSONL：产生路径失效、创建和 CLI 完成事件；不泄露媒体内容。
- 第二阶段完成后，缺失成品的 `batch-continue` 停在 `awaiting_input`，显式 `continue`
  仍可重新烧录。

提交前执行：

```bash
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync pytest -q
uv run --no-sync xvt --help
```
