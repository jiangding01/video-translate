"""翻译上下文候选包的落盘与登记（Q7a）。

只做记账：读 checksum 有效的 ``source_cues``（以及可选的 ``remote_info`` 标题），
调 ``xvt.translation_context`` 的确定性抽取器，把结果写成 ``translation_context``
artifact，并可选导出一份 glossary 骨架。

刻意不做的三件事，都是范围决定而不是遗漏：

- **不改任何 stage**。候选包不是任何 stage 的产出，也不参与推进判定
  （``plan_advance_steps`` / ``AWAIT_STEPS`` 一概不碰），先例是
  ``workflow.relink_source_media`` 只写一个字段而不动 stage。
- **不新增事件类型**。artifact 登记走 ``add_artifact`` 的既有
  ``ARTIFACT_CREATED``，kind 自身就是语义，不必再加一个专用事件。
- **不进翻译 prompt、不进任何 fingerprint**。候选只有经用户审核后通过
  ``--glossary`` 显式传回才会影响翻译；把它接进 prompt 是 Q7c 的协议变更。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from xvt.events import EventSink
from xvt.job_store import get_job_directory, get_job_file, load_job
from xvt.paths import assert_safe_output, protected_job_paths
from xvt.pipeline.common import add_artifact, load_cues, write_json
from xvt.recovery import file_checksum, valid_artifact_path
from xvt.translation_context import (
    extract_translation_context,
    glossary_skeleton_terms,
)

_GLOSSARY_SKELETON_NOTE = (
    "Review every entry before use: delete any term whose translation does not have to keep "
    "the source spelling, and replace the placeholder target with the wording you want. "
    "Targets are pre-filled with the source text because proper names most often stay as-is; "
    "an unreviewed entry forces that literal text into the translation and is checked by "
    "deterministic QA. Entries carrying `_covered_by` are parts of a multi-word proper name "
    "(the listed phrases): keep the part only if it is a term on its own, otherwise delete it "
    "and add the whole phrase as its own entry with the wording you want."
)


@dataclass(frozen=True, slots=True)
class TranslationContextResult:
    report_path: Path
    candidate_count: int
    degraded_reason: str | None
    glossary_path: Path | None
    glossary_term_count: int
    # 骨架里属于某个多词专名的条目：``(词, 覆盖它的第一个短语)``，供 CLI 当场提示。
    # 不是 artifact 的一部分——骨架是用户指定路径的文件，产物结构一个字节未变。
    covered_skeleton_terms: tuple[tuple[str, str], ...] = ()


def build_translation_context(
    job_file: str | Path,
    *,
    export_glossary: str | Path | None = None,
    event_sink: EventSink | None = None,
) -> TranslationContextResult:
    """抽取并登记翻译上下文候选包。

    ``source_cues`` 缺失或 checksum 失效时抛 ``ValueError``（CLI 收口为退出码 2），
    此时不写任何文件、不登记 artifact——半成品产物会以 checksum 有效的姿态进入
    恢复判定。``export_glossary`` 指向受保护路径（job 文件、用户原始输入、任何已
    登记 artifact）或不存在的目录时同样在写盘之前拒绝，退出码一致。
    """

    job = load_job(job_file)
    source_path = valid_artifact_path(job, "source_cues")
    if source_path is None:
        raise ValueError(
            "translation context requires a checksum-valid source_cues artifact; "
            "run `xvt status JOB` for the recovery plan"
        )
    cues = load_cues(source_path)
    if not cues:
        raise ValueError("source_cues contains no cue to analyse")

    # 导出路径的两道校验都排在抽取与写盘**之前**。本函数的契约是「拒绝时不写任何
    # 文件、不登记 artifact」，而骨架导出本身排在 artifact 登记之后：等到那一步才
    # 发现路径不能用，产物已经落了半份。
    glossary_target: Path | None = None
    if export_glossary is not None:
        glossary_target = Path(export_glossary).expanduser().resolve()
        # 骨架是本命令新开的「用户指定路径」写盘出口，走与烧录/预览输出同一道覆盖
        # 保护：它绝不能盖掉 job 文件、用户原始输入或任何已登记 artifact。骨架不是
        # artifact kind，所以 writing_kind 传 "" 让全部 artifact 都留在保护集合里
        # （没有哪个 kind 是空串）；源媒体已由 artifact 清单与用户输入清单覆盖，
        # 媒体位不再单独解析——为一次路径比对去算 GB 级媒体的 checksum 不值得。
        assert_safe_output(
            glossary_target,
            protected=protected_job_paths(job, get_job_file(job), writing_kind=""),
            label="glossary skeleton",
        )
        if not glossary_target.parent.is_dir():
            # 自己判而不是让 write_json 抛 FileNotFoundError：后者是 OSError，顶层
            # 收口成退出码 3（dependency_or_process），而目录不存在明摆着是用户输入
            # 错误，该和其余拒绝路径一样是 2。
            raise ValueError(
                f"glossary skeleton directory does not exist: {glossary_target.parent}"
            )

    source_language = next(
        (cue.source_language for cue in cues if cue.source_language),
        job.config.get("source_language", "auto"),
    )
    metadata_title = None
    remote_checksum = None
    remote_path = valid_artifact_path(job, "remote_info")
    if remote_path is not None:
        remote_checksum = file_checksum(remote_path)
        try:
            remote_data = json.loads(remote_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            # 平台元数据只是一个加分证据：读不出来就不加分，不该让整条命令失败。
            remote_data = None
        if isinstance(remote_data, dict):
            title = remote_data.get("title")
            metadata_title = title if isinstance(title, str) and title.strip() else None

    report = extract_translation_context(
        cues,
        source_language=source_language,
        metadata_title=metadata_title,
    )
    # provenance 由本层补：抽取器是零依赖叶子，不认 checksum，也不该认。
    report["source_cues_checksum"] = file_checksum(source_path)
    report["remote_info_checksum"] = remote_checksum

    report_path = get_job_directory(job) / "artifacts" / "translation-context.json"
    write_json(report_path, report)
    add_artifact(job, "translation_context", report_path, event_sink)

    terms: list[dict[str, object]] = []
    if glossary_target is not None:
        terms = glossary_skeleton_terms(report)
        write_json(glossary_target, {"_note": _GLOSSARY_SKELETON_NOTE, "terms": terms})

    covered = tuple(
        (str(term["source"]), str(term["_covered_by"][0]))
        for term in terms
        if term.get("_covered_by")
    )
    degraded = report.get("extractor_degraded")
    return TranslationContextResult(
        report_path=report_path,
        candidate_count=len(report["candidates"]),
        degraded_reason=degraded["reason"] if isinstance(degraded, dict) else None,
        glossary_path=glossary_target,
        glossary_term_count=len(terms),
        covered_skeleton_terms=covered,
    )
