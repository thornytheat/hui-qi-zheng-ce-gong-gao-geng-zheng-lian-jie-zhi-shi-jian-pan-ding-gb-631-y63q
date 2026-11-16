"""断点续跑：从失败阶段继续，已落库的页面与公告直接复用，不重抓。"""
from __future__ import annotations

from ... import config
from . import state

# 采信之后的阶段都可以复用已落库数据
REUSABLE_AFTER = {
    "ingest": (),                       # 采信失败：页面不完整，整体重抓
    "parse": (),                        # 解析失败：原始表格行未落库，整体重抓
    "quality": (),                      # 质量失败：同上，整体重抓
    "chain": ("ingest", "parse", "quality"),   # 链判定之后可直接复用已解析公告
    "list": ("ingest", "parse", "quality", "chain"),
    "export": ("ingest", "parse", "quality", "chain", "list"),
}


def plan(store, batch_id: str) -> dict:
    batch = store.get_batch(batch_id)
    if batch is None:
        return {"can_resume": False, "code": "E_NOT_FOUND", "from_stage": None,
                "reused_pages": 0, "note": "批次不存在"}
    stages = store.load_stages(batch_id)
    failed = state.interrupted_stage(stages)
    if batch["status"] != "failed":
        return {"can_resume": False, "code": "B_RESUME_NO_STATE", "from_stage": None,
                "reused_pages": 0,
                "note": f"批次状态为 {batch['status']}，只有失败批次可以续跑"}
    if failed is None:
        return {"can_resume": False, "code": "B_RESUME_NO_STATE", "from_stage": None,
                "reused_pages": 0, "note": "没有记录到失败阶段，按全新批次重跑"}
    reused = len([p for p in store.load_pages(batch_id) if p["status"] == "ok"])
    reusable_stages = REUSABLE_AFTER[failed]
    return {
        "can_resume": True,
        "code": None,
        "from_stage": failed,
        "reusable_stages": list(reusable_stages),
        "reused_pages": reused if "ingest" in reusable_stages else 0,
        "note": (f"从「{config.STAGE_LABELS[failed]}」阶段续跑"
                 + (f"，复用已采信页面 {reused} 个" if "ingest" in reusable_stages else "")),
    }
