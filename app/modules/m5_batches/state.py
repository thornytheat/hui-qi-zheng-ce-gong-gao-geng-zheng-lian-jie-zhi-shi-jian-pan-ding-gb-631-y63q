"""批次状态机：批次号、批次键（幂等依据）、阶段记录与状态推导。

批次键 = sha256(规则版本 + 来源 + 已采信页面的内容摘要集合)：
同一次输入重复跑得到同一个键，命中已完成的批次即复用结果，不产生第二份清单。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

from ... import config, manifest


def new_batch_id(prefix: str = "b") -> str:
    stamp = dt.datetime.now(config.UTC).strftime("%y%m%d%H%M%S")
    return f"{prefix}{stamp}{uuid.uuid4().hex[:4]}"


def batch_key(*, rule_version: str, source: str, digests: list[str]) -> str:
    payload = json.dumps({"rule": rule_version, "source": source,
                          "pages": sorted(digests)}, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")


def stage(stage_key: str, *, in_count: int, out_count: int, quarantine_count: int,
          elapsed_ms: int, status: str = "ok", note: str = "") -> dict:
    return {"stage": stage_key, "label": config.STAGE_LABELS[stage_key],
            "in_count": in_count, "out_count": out_count,
            "quarantine_count": quarantine_count, "elapsed_ms": elapsed_ms,
            "status": status, "note": note}


def failed_stage(stage_key: str, *, message: str, elapsed_ms: int = 0) -> dict:
    return stage(stage_key, in_count=0, out_count=0, quarantine_count=0,
                 elapsed_ms=elapsed_ms, status="failed", note=message)


def derive_status(stages: list[dict]) -> str:
    if any(s["status"] == "failed" for s in stages):
        return "failed"
    if len(stages) < len(config.STAGES):
        return "running"
    return "done"


def interrupted_stage(stages: list[dict]) -> str | None:
    for item in stages:
        if item["status"] == "failed":
            return item["stage"]
    return None


def page_digests(pages: list[dict]) -> list[str]:
    return sorted(p["sha256"] for p in pages if p.get("sha256"))


def new_run_manifest(*, run_id, batch_id, batch_key_value, settings, source, input_digest,
                     pages, params, started_at, finished_at, elapsed_ms, stages,
                     counters) -> dict:
    return manifest.build(
        run_id=run_id, batch_id=batch_id, batch_key=batch_key_value, settings=settings,
        source=source, input_digest=input_digest, pages=pages, params=params,
        started_at=started_at, finished_at=finished_at, elapsed_ms=elapsed_ms,
        stages=stages, counters=counters)
