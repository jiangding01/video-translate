#!/usr/bin/env python3
"""断言本机 libass **真的能测量**双语字幕行宽，而不是只断言「装了字体包」。

为什么要有这一步：`qa.check_subtitles` 在 `render.measure_bilingual_line_widths`
抛错时会**静默降级**到保守的显示单位上限，只记一条 `layout_measurement_unavailable`
warning。缺字幕字体的机器因此照样全绿，而行宽 QA 从未真正测量过——CI 的 Linux 侧在
2026-10-20 之前就是这个状态（全历史没装过任何字幕字体，libass 报
`failed to find any fallback with glyph` → `ValueError`）。

为什么不写成 `fc-list | grep -i cjk`：那是代理指标。「字体包在盘上」不等于
「fontconfig 能把 `font_for_language` 要的家族名解析到」，更不等于「libass 能把
译文每个字形渲出来并量出包围盒」。本脚本直接调真实入口，用带 CJK / 假名字形的
文本量一次，抛错就让这一步失败。

顺序约束：它依赖已安装的 xvt 包与可用的 ffmpeg（带 `ass` 滤镜），在 CI 上必须排在
`Sync dependencies` **之后**。

本地同样可跑：`uv run --no-sync python .github/scripts/verify_layout_measurement.py`
"""

from __future__ import annotations

import sys

from xvt.models import Cue
from xvt.qa import check_subtitles
from xvt.render import font_for_language, measure_bilingual_line_widths
from xvt.segment import wrap_overflowing_target
from xvt.textwidth import display_width

VIDEO_WIDTH = 640
VIDEO_HEIGHT = 360

# 覆盖本仓库测试里真实用到的每种目标语言（tests 里出现的是 zh-CN / zh、ja、en）。
# Linux 上 `font_for_language` 分别要 Noto Sans CJK SC / Noto Sans CJK JP / DejaVu Sans。
CASES = (
    ("zh-CN", "你好，世界。宽度测量。"),
    ("ja", "こんにちは世界。幅の計測。"),
    ("en", "Hello world, width measurement."),
)


def verify(language: str, target_text: str) -> list[str]:
    """量一条双语 cue；任何失败都抛错，返回供日志取证的逐字行。"""

    lines = [f"[{language}] font_for_language -> {font_for_language(language)!r}"]
    cue = Cue(
        1,
        0,
        4000,
        "Hello world, width measurement.",
        target_text=target_text,
        target_language=language,
        source_language="en",
    )

    # ① 直接调真实测量入口：缺字形 / 缺 ffmpeg / 解析不出包围盒都会从这里抛出来，
    #    带着原始异常类型与 message，而不是被 check_subtitles 吞成一条 warning。
    widths = measure_bilingual_line_widths(
        [cue], video_width=VIDEO_WIDTH, video_height=VIDEO_HEIGHT
    )
    if not widths:
        raise RuntimeError(f"[{language}] measurement returned no widths")
    for (cue_id, role), (width, limit) in sorted(widths.items()):
        lines.append(f"[{language}] measured cue={cue_id} role={role} width={width} limit={limit}")

    # ② 再走真实消费者一遍：复用判定与行宽阻断读的是报告自己的 `layout` 块，
    #    所以断言那个结论，而不是只断言 ① 没抛。
    report = check_subtitles(
        [cue],
        video_width=VIDEO_WIDTH,
        video_height=VIDEO_HEIGHT,
        require_translation=True,
        measure_layout=True,
    )
    layout = report.layout
    if not isinstance(layout, dict):
        raise RuntimeError(f"[{language}] report has no layout block: {report}")
    codes = sorted({issue.code for issue in report.issues})
    lines.append(
        f"[{language}] report measurement={layout.get('measurement')!r} "
        f"status={report.status!r} issues={codes}"
    )
    if layout.get("measurement") != "measured":
        raise RuntimeError(
            f"[{language}] layout measurement degraded to {layout.get('measurement')!r}; "
            f"issues={codes}"
        )
    if "layout_measurement_unavailable" in codes:
        raise RuntimeError(f"[{language}] layout_measurement_unavailable is present: {codes}")
    return lines


# 译文整段超过显示单位上限（zh 为 36）但切成两行放得下：libass 必须量的是 render
# 按 `segment.wrap_overflowing_target` 切好的两行事件，报告给 `target_wrapped` 而不是
# `bilingual_overflow_risk`。
WRAP_CASE = ("zh-CN", "我们需要保留每个字幕的含义，但是也要保证每一行都清晰可读")


def verify_wrapped(language: str, target_text: str) -> list[str]:
    """量一条会被切成两行的译文：两行都放得下，必须真测且不判超宽。"""

    lines = [f"[{language}/wrap] target display width {display_width(target_text)}"]
    wrapped = wrap_overflowing_target(
        target_text, language, video_width=VIDEO_WIDTH, video_height=VIDEO_HEIGHT
    )
    if wrapped is None or len(wrapped) != 2:
        raise RuntimeError(f"[{language}/wrap] fixture is not wrapped into 2 lines: {wrapped}")
    lines.append(f"[{language}/wrap] lines={list(wrapped)}")
    cue = Cue(
        1,
        0,
        4000,
        "Hello world, width measurement.",
        target_text=target_text,
        target_language=language,
        source_language="en",
    )
    report = check_subtitles(
        [cue],
        video_width=VIDEO_WIDTH,
        video_height=VIDEO_HEIGHT,
        require_translation=True,
        measure_layout=True,
    )
    layout = report.layout
    if not isinstance(layout, dict):
        raise RuntimeError(f"[{language}/wrap] report has no layout block: {report}")
    for cue_id, role, width, limit in layout.get("widths", []):
        lines.append(
            f"[{language}/wrap] measured cue={cue_id} role={role} width={width} limit={limit}"
        )
    codes = sorted({issue.code for issue in report.issues})
    lines.append(
        f"[{language}/wrap] report measurement={layout.get('measurement')!r} "
        f"status={report.status!r} issues={codes}"
    )
    if layout.get("measurement") != "measured":
        raise RuntimeError(
            f"[{language}/wrap] layout measurement degraded to {layout.get('measurement')!r}"
        )
    if "target_wrapped" not in codes or "bilingual_overflow_risk" in codes:
        raise RuntimeError(f"[{language}/wrap] expected target_wrapped without overflow: {codes}")
    return lines


def main() -> int:
    print(f"platform: {sys.platform}")
    for language, target_text in CASES:
        for line in verify(language, target_text):
            print(line)
    for line in verify_wrapped(*WRAP_CASE):
        print(line)
    print("libass really measured every language above; width QA is not running degraded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
