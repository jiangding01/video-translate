from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from xvt.locking import hold_job_lock
from xvt.models import Job


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_output_root(output_root: str | Path) -> Path:
    expanded = Path(output_root).expanduser()
    if not expanded.is_absolute():
        raise ValueError("output_root must be an absolute path or start with ~")
    return expanded.resolve()


def create_job(
    input_value: str,
    output_root: str | Path,
    config: dict[str, Any] | None = None,
) -> Job:
    root = normalize_output_root(output_root)
    now = utc_now()
    job_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    job = Job(
        id=job_id,
        created_at=now,
        updated_at=now,
        input=input_value,
        output_root=str(root),
        config=dict(config or {}),
    )
    # 在首次持久化之前就持有 Job 目录锁（挂在当前命令锁作用域上，无作用域时跳过），
    # 保证 job.json 出现在磁盘上的那一刻起任务已被独占，并发 continue 无窗口可乘。
    hold_job_lock(get_job_file(job))
    save_job(job)
    return job


def get_job_directory(job: Job) -> Path:
    return Path(job.output_root) / "jobs" / job.id


def get_job_file(job: Job) -> Path:
    return get_job_directory(job) / "job.json"


def save_job(job: Job) -> Path:
    job.updated_at = utc_now()
    job_dir = get_job_directory(job)
    job_dir.mkdir(parents=True, exist_ok=True)
    job_file = job_dir / "job.json"
    temporary_file = job_dir / ".job.json.tmp"
    temporary_file.write_text(
        json.dumps(job.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_file, job_file)
    return job_file


def load_job(job_file: str | Path) -> Job:
    path = Path(job_file).expanduser()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("job file must contain a JSON object")
    return Job.from_dict(data)
