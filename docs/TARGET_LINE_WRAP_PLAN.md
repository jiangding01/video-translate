# 译文超宽的 cue 内确定性换行技术方案

拍板时间：2026-10-22。状态：已批准，待实施。

## 背景

HANDOFF「已知限制」第 1 条：译文超过硬显示阈值时只能由 QA 阻断（`needs_review`），没有自动出路。对标 BaoCut（见 2026-10-22 调研，经 Fable 5.1 核验）后确认：它解决这个问题的核心机制是**确定性的展示切分**（DP 切行 + 代价函数），而**不是**回头让模型重译缩短——其设计文档 §6.3 把「边译边凑行会丢信息」写成实验结论。这与 XVT「翻译只管文本，分段/时间轴/QA/渲染由确定性代码完成」的架构方向一致。

关键事实：**不可破坏约束 1 约束的是 cue 的 ID、起止时间、顺序、数量，不约束一个 cue 内部能否换行**。ASS 的 `\N` 正是为此存在，且 `_styled_subtitles` 已设 `WrapStyle=2`（libass 不自动换行），换行权完全在我们手里。

### 现状（已核实）

| 环节 | 现在 | 位置 |
| --- | --- | --- |
| 译文超宽判定 | 整段算一行，超 `display_width_review_limit` 给 `review`（不是 error） | `qa.py` `_overflow_risk` |
| 译文腿 QA | 只按显示单位，不测 libass | `pipeline/translation.py` |
| 烧录腿 QA | libass 真实测量，一个 cue 一个事件 | `pipeline/burnin.py` → `render.measure_bilingual_line_widths` |
| ASS 写出 | `target\N{\rSource}source`，固定两行 | `render.save_bilingual_ass` |
| 换行前归一化 | `normalize_display_text` 把一切空白（含 `\n`）折成空格，QA / 测量 / 写出三处都过它 | `subtitles.py` |
| 宽度切分 DP | `_partition_words` 已有：禁则惩罚 + 句末/软标点/连接词代价 + 孤词惩罚 | `segment.py` |
| 禁则谓词 | `forbidden_break` 是全仓唯一「哪里不许切」的判定，现有三个切分出口 | `segment.py` |

## 目标

译文（`role == "target"`）整段超过显示单位上限时，先用确定性算法按禁则切成**最多 2 行**；两行都不超限即通过并记一条 `target_wrapped` warning；切不开或切完仍超限才像今天一样给 `review`。ASS 写出与 libass 测量使用**同一份切分结果**，QA 量的就是烧出去的。

## 非目标

- 不动 `translated_cues` artifact、不改 Job schema、不新增 artifact。换行是 `(文本, 语言, 画幅)` 的纯函数，三处现场重算。
- 源文不换行（约束 2，源文归用户 / ASR）。
- 不做 3 行及以上译文。
- 不做 libass 驱动的逐像素二分换行；先按显示单位切、libass 复核（BaoCut 同样先走确定性 DP）。
- 不回头让模型重译缩短。
- SRT 输出不换行：它是人工修订与导入的载体，`\n` 会污染文本层往返；纯字幕 Job 写 SRT 时也没有画幅。

## 设计决策

### D1 换行函数放 `segment.py`，直接复用 `_partition_words`

新增 `wrap_target_lines(text, language, *, max_width, max_lines=2) -> tuple[str, ...] | None`。对译文构造伪 `Word(text, start_ms=0, end_ms=0)` 喂给 `_partition_words(tokens, 2, max_width=limit)`。分词：非 CJK 按空白分词、每个 token 保留尾随空格以便 `_text` 原样拼回；CJK 按字分词，但 Latin / 数字连续段作为一个 token 不拆。

这是全仓第 **4** 个切分出口，按 AGENTS 必须接入 `forbidden_break`——DP 内部经 `_break_penalty` 已经走它。但 `KINSOKU_BREAK_PENALTY = 64` 是代价不是硬禁，无路可走时 DP 仍会给出违禁切点；分段腿可以接受这个兜底，展示换行不行。所以**切完后对每个边界再过一遍 `forbidden_break`，违禁即返回 `None` → review**。`forbidden_break` docstring 从「三个产出点」改为四个。

单 token 或 `_partition_words` 返回 `None` 时同样返回 `None`。

### D2 先抽零依赖叶子 `textwidth.py`，否则必然 import 环

现在 `segment → qa → render`，而换行要被 `qa._overflow_risk` 和 `render` 两边调用，放哪都成环。把六个纯函数下移到新叶子 `src/xvt/textwidth.py`：`display_width`、`is_cjk_language`、`character_limit`、`display_width_limit`、`reading_speed_limit`、`display_width_review_limit`。`qa.py` 原名 re-export（8 处既有导入零改动），`segment.py` 改从叶子导入。之后依赖图为 `textwidth ← segment ← {qa, render}`，`render ← qa`，无环。不用函数级惰性 import 规避。

### D3 换行上限三处同源

译文腿 QA、烧录腿测量、ASS 写出对同一段文本必须切出同一结果，否则 QA 量的和烧的不是一回事。上限统一取 `display_width_review_limit(language, video_width, video_height)`，由一个 helper 给出，不在三处各算。libass 测的是切好后的事件——`\N` 下 bbox 宽度天然是最宽那行，所以 `measure_bilingual_line_widths` 返回值形状 `dict[(cue_id, role), (width, limit)]` **不变**，7 处既有测试打桩继续有效。

调用顺序固定为：`normalize_display_text` → `wrap_target_lines` → 逐行 `escape_ass_text` → `"\\N".join`。归一化必须在切分**之前**，否则折叠掉 `\n`。

### D4 报告 `layout` 块新增 `wrap` 绑定

`check_subtitles` 在 `measure_layout` 时于 `layout` 加 `"wrap": {"version": 1, "max_lines": 2}`。`pipeline.burnin._reusable_rendered_layout` 把它纳入绑定：旧报告没有这个键 → 不复用 → 烧录腿重测重渲（fail closed）。否则旧 QA 量的单行与新代码烧的两行会错位。这是对 RECOVERY「烧录腿复用渲染腿的布局 QA 与 ASS」契约的一处显式扩展。

### D5 issue 文案前缀不变

切完仍超限时文案保持 `target line width N exceeds M display units` 前缀，后缀加 ` after wrapping into 2 lines`。`candidate_qa._LINE_WIDTH_PATTERN` 是 `search`，前缀不变就仍能解析，`recovery.py` / `pipeline/revision.py` 两个消费者零改动；人工修订模板里给的上限自然就是「每行上限」。`N` 取最宽那行的宽度。

补记（2026-10-23）：「上限自然就是每行上限」只在代码视角成立——模板读者只看到 `limit: 36` 会以为要把整句缩进一行。`pipeline/revision.py` 的宽度标注随后补了 `max_lines`（取 `segment.TARGET_WRAP_MAX_LINES`），只标在 `target` 的宽度项上；解析仍是 `overflow_limits` 那一份正则，行数不从 message 里解析，`recovery.py` 的消费确实零改动。

### D6 不超限的 cue 报告字节逐字不变

协议快照冻结了 `translated_subtitle_qa` 的内容 checksum，快照里没有烧录 / 渲染场景（`layout` 恒为 `None`）。只要不超限路径不改一个字节，快照不动。实现后跑快照：若 diff **恰好**是「某条 fixture 原本 review、现在 `target_wrapped`」，那是刻意的协议变更，按 AGENTS 修改流程第 8 条重生成 golden 并写 Changelog；若 diff 是别的，回退改动。

### D7 `target_wrapped` 记 warning（已拍板）

换过行的 cue 应在报告里看得见；`warning` 的既有语义就是「过了，但值得看一眼」。对下游零影响：`complete_qa_stage` 只特判 `needs_review`，`_reusable_rendered_layout` 接受 `pass` / `warning`。代价仅是原本 `pass` 的报告 `status` 字面变 `warning`。

## 改动清单（11 个文件，两个 commit，各自独立绿）

### Commit 1 — 纯搬移，零行为变化

- 新建 `src/xvt/textwidth.py`。
- `src/xvt/qa.py`：改为从叶子导入并原名 re-export。
- `src/xvt/segment.py`：改从叶子导入。
- 验证：ruff + 全量 pytest；`tests/test_pipeline_imports.py` 只守 pipeline 包，不受影响。

### Commit 2 — 换行本体

- `src/xvt/segment.py`：`wrap_target_lines`；`forbidden_break` docstring 改四个出口。
- `src/xvt/qa.py`：`_overflow_risk` 对 target 先切后判，新增 `target_wrapped` warning；`check_subtitles` 的 `layout` 加 `wrap`。
- `src/xvt/render.py`：`save_bilingual_ass` 与 `measure_bilingual_line_widths` 的 Target 文本改为切后逐行转义、`\N` 拼接。
- `src/xvt/pipeline/burnin.py`：`_reusable_rendered_layout` 绑定 `layout["wrap"]`。
- 测试：
  - `tests/test_qa.py`：能切则 `warning` 不 `review`；切完仍超则 `review` 且 `overflow_limits` 仍能解析；禁则兜底返回 `review`；源文永不切；**不超限 cue 报告字节不变**（D6 的回归守卫）。
  - `tests/test_render.py`：ASS 事件文本含两个 `\N`（译文行间一个、译文与 `{\rSource}` 间一个）；测量事件使用切后文本。
  - `tests/test_pipeline_burnin.py`：无 `wrap` 键的旧报告不复用。
  - 新建 `tests/test_segment_wrap.py`：Latin 按词、CJK 按字、CJK 内 Latin 段不拆、禁则后检返回 `None`、单 token 返回 `None`。
- 文档：README「输出与质量控制」、`docs/ARCHITECTURE.md` `render.py` 行、`docs/RECOVERY.md` 布局复用契约加 `wrap` 绑定、`AGENTS.md`（`segment.py` 行改四个出口、`burnin` 行加绑定项）、CHANGELOG Unreleased、HANDOFF 已知限制第 1 条。

## 验证

```bash
uv run --no-sync ruff check src tests && uv run --no-sync ruff format --check src tests
uv run --no-sync pytest -q
uv run --no-sync pytest -q tests/test_provider_protocol_snapshot.py   # 期望零 diff，见 D6
uv run --no-sync python .github/scripts/verify_layout_measurement.py # libass 真测一条两行译文
```

另加一条真实烧录冒烟：拿一个超宽译文的 cue 走 `xvt preview`，肉眼确认三行（译文两行 + 源文一行）的位置与遮挡。`PlayResY=288`、默认字号 22、`marginv=16`，三行约 70px，纵向算术上绰绰有余，但必须看一眼实际画面。

## 风险

**最脆弱的假设**：显示单位切出的两行，libass 一量仍可能有一行超像素上限（字体对各字符宽度的估计与东亚宽度表不一致时）。此时退回 `review`，与今天行为相同，**永远不会比现在差**。若实测这种情况常见，说明显示单位估计偏差大，那时该做的是让像素测量反哺切点（二分），不是现在就做。

**在途 Job 的确认记录**：确认绑定 `report_checksum`。升级后重跑 QA 产生不同报告 → 旧确认失效。多数情况下该 cue 现在能切开通过，不再需要确认；切不开的会再次 `review`，用户需重新确认。写进 Changelog。

## 实施方式

主会话模型为 Fable 5.1 时按 CLAUDE.md 分工：主会话只做方案、分发与验收；两个 commit 的实现、测试与文档同步派给 subagent，验收以本文件的改动清单与验证命令为准。
