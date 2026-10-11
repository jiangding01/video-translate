# polished Agent 真实端到端验收

日期：2026-10-11。执行代码：main / 53c77727c3283702ae58d2a3f09fdc535ae15878。结果：本样本通过；默认保持 `standard + faithful`。

## 样本与范围

公开视频：[Mods in Claude Code](https://www.youtube.com/watch?v=yUuFvL1lK4k)。新建独立临时 Job，使用英文人工字幕、中文目标字幕，配置 `provider=agent`、`translation_style=natural`、`translation_quality=polished`、`burn=true`。下载匿名执行，不读取 Cookie、不调用翻译 API，不导入原 Job 译文。翻译与语义校对由当前 Agent 实际逐组处理，不使用 stub 或固定测试译文。

本次覆盖真实下载、平台字幕选择、Agent 一次翻译润色、语义校对、确定性 QA、ASS 渲染、工作流烧录和媒体 QA。人工字幕通过候选 QA 后采用其原时间轴，因此不覆盖 ASR；本样本没有语义修复项，也不代表修复/人工确认分支的真实素材验收已完成。

## 流程与证据

1. `doctor --json` 通过，FFmpeg、字体、yt-dlp、编码器和 MLX 检查正常。
2. `run` 获取 1920×1080 的 AV1/Opus 源视频和 84 条英文人工字幕，生成 `task=translate_and_polish` manifest，退出 0 并进入 Agent 等待。
3. 翻译等待期间调用 `status` / `continue`，仍提示回填，没有误报工作流完成。
4. 回填完整 84 条翻译润色响应，`apply --continue` 进入 `semantic_review_and_repair` 等待；再次 `continue` 保持正确等待状态。
5. 提交故意失配 fingerprint 的语义响应，退出 2；Job 与等待 manifest 的内容 checksum 完全不变。之后有效响应仍可正常应用。
6. 对四个批次逐 cue 与逐语义组校对，没有发现需要报告的译义、遗漏、术语或连接问题。未伪造修复项，`repair_results` / `final_results` 为空。该结果衡量译文对所选字幕的忠实性，不证明平台字幕逐字符合原音。
7. `continue` 完成实际整段烧录，媒体 QA 通过，工作流进入 complete。24 个 artifact 全部校验通过，0 failed、0 checksum warnings。
8. 全部 84 条 cue 的 ID、数量、顺序、起止时间与源文保持一致；润色 checkpoint 与候选有效。生成的 v2 burn proof 绑定成品与实际源媒体 checksum。
9. 对 cue 20、24、77 的成品帧进行目视抽查，双语字幕可见且无截断；这是抽样检查，不能替代逐帧或原音复核。
10. 完成后再次 `continue`，没有重新启动 translate / burn，源 cue、译文和成品 checksum 不变。用户原 Job 的源 cue、译文与 SRT 校验仍有效。

## 成品与 QA

| 项目 | 结果 |
| --- | --- |
| 分辨率 | 1920×1080 |
| 编码 | H.264 / AAC |
| 源时长 | 200634ms |
| 成品与源时长差 | 0ms |
| 确定性译文 QA | warning：9 条双语显示量风险、8 条阅读速度提醒；无 review/error 阻断 |
| 媒体 QA | pass |
| artifact 完整性 | 24 / 24 pass |
| 成品 SHA-256 | e66a181386754df90f62eb2a35440e184e3e42bf0c81e4c731c59bac4001daef |

本次生成的 Job、响应、事件、成品和抽查图只保存在临时目录，不提交媒体或原始提取元信息。Job ID：20261011T103246Z-27a49107。临时产物不作为永久回归 fixture。

## 默认质量决策与下一步

保留 `standard + faithful` 默认值，`natural + polished` 继续显式选择。本次证明 Agent 主链能够完成，未做 standard/polished 盲评，也未比较多领域样本、人工修订次数、模型成本和耗时，不能据此断言 polished 在所有输入上更好。

下一项按路线图推进 Q6.2 语义复查收敛：建立脱敏真实样本与新增/保留/解决问题的指标，先判断误报和多轮摆动是否可复现，再设计调整。随后再进入 R3.1 / R3.2，不把本次验收扩展为默认值或架构改动。
