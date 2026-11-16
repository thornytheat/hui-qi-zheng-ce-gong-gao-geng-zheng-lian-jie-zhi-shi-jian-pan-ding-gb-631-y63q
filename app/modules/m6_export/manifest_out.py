"""运行清单输出：人读 JSON 与可对账的扁平行。

清单里带 run_id、批次键、输入摘要、规则版本、参数、耗时与阶段计数，
拿同一份输入重跑可以逐字段比对。
"""
from __future__ import annotations

import json


def to_json(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False)


def flat_rows(manifest: dict) -> list[list[str]]:
    rows = [
        ["run_id", manifest.get("run_id", "")],
        ["batch_id", manifest.get("batch_id", "")],
        ["batch_key", manifest.get("batch_key", "")],
        ["source", manifest.get("source", "")],
        ["rule_version", manifest.get("rule_version", "")],
        ["eval_date", manifest.get("eval_date", "")],
        ["input_digest", manifest.get("input_digest", "")],
        ["started_at", manifest.get("started_at", "")],
        ["finished_at", manifest.get("finished_at", "")],
        ["elapsed_ms", str(manifest.get("elapsed_ms", ""))],
        ["decimals", manifest.get("decimals", "")],
        ["timezone", manifest.get("timezone", "")],
    ]
    for page in manifest.get("inputs", []):
        rows.append([f"input:{page['url']}", f"sha256={page['sha256']} bytes={page['bytes']} "
                                             f"encoding={page['encoding']}"])
    for key, value in (manifest.get("counters") or {}).items():
        rows.append([f"counter:{key}", str(value)])
    for stage in manifest.get("stages", []):
        rows.append([f"stage:{stage['stage']}",
                     f"in={stage['in_count']} out={stage['out_count']} "
                     f"quarantine={stage['quarantine_count']} status={stage['status']}"])
    return rows
