"""运行清单：run_id、输入摘要、规则版本、参数、耗时，可复算。

run_id = 批次号 + 清单摘要前缀，人读短、机读唯一。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import uuid

from . import config


def new_run_id(prefix: str = "NC") -> str:
    stamp = dt.datetime.now(config.UTC).strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:4]}"


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_parts(parts: list[str]) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def build(*, run_id: str, batch_id: str, batch_key: str, settings, source: str,
          input_digest: str, pages: list[dict], params: dict, started_at: str,
          finished_at: str, elapsed_ms: int, stages: list[dict], counters: dict) -> dict:
    return {
        "run_id": run_id,
        "batch_id": batch_id,
        "batch_key": batch_key,
        "source": source,
        "rule_version": settings.rule_version,
        "eval_date": settings.eval_date.isoformat(),
        "sample_site_base": settings.sample_site_base,
        "decimals": "金额 Decimal，单位万元，最小刻度 0.01",
        "timezone": "入库 UTC，展示 Asia/Shanghai(+08:00)",
        "input_digest": input_digest,
        "inputs": [
            {"url": p["url"], "status": p["status"], "bytes": p["bytes"],
             "sha256": p["sha256"], "encoding": p["encoding"]}
            for p in pages
        ],
        "params": params,
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_ms": elapsed_ms,
        "stages": stages,
        "counters": counters,
        "replayable": "同 batch_key 重跑返回同一批次，不新增行",
    }
