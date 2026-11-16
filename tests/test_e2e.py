"""端到端：样例站 → 采信 → 链判定 → 清单 → 导出；以及失败输入的错误码。"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest

from fixtures import expected as X


def test_full_pipeline_end_to_end(client_with_run):
    """端到端：一次采集走完六个阶段，计数与金额与人工推算一致。"""
    policies = client_with_run.get("/api/policies").json()
    assert policies["counts"]["total"] == X.CHAINS
    assert policies["counts"]["listed"] == X.LISTED
    assert policies["amount_total"] == X.AMOUNT_TOTAL
    assert policies["eval_date"] == "2026-11-15"

    reconcile = client_with_run.get("/api/reconcile").json()
    assert reconcile["all_balanced"] is True

    exported = client_with_run.get("/api/export?kind=policies")
    assert exported.status_code == 200
    assert exported.headers["content-disposition"].endswith('.csv"')
    assert exported.content.startswith(b"\xef\xbb\xbf")


def test_failed_source_returns_4xx(client):
    """失败输入：未知来源返回 400 且带错误码，不落半批数据。"""
    response = client.post("/api/crawl", json={"source": "portal_z"})
    assert response.status_code == 400
    assert response.json()["error"] == "E_BAD_SOURCE"
    assert client.get("/api/batches").json()["total"] == 0


def test_bad_mode_returns_4xx(client):
    """失败输入：mode 非法返回 400。"""
    response = client.post("/api/crawl", json={"source": "all", "mode": "incremental"})
    assert response.status_code == 400
    assert response.json()["error"] == "E_BAD_MODE"


def test_parse_preview_rejects_bad_row(client):
    """失败输入：缺必需字段的预览返回隔离原因，不返回「成功」。"""
    response = client.post("/api/parse/preview",
                           json={"fields": {"标题": "只有标题", "公告类型": "首次发布"}})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["quarantine"][0]["code"] == "Q_MISSING_REQUIRED"


def test_second_crawl_is_idempotent(client_with_run):
    """不变量（幂等）：通过 API 再跑一次，批次与行数不变。"""
    first = client_with_run.get("/api/batches").json()
    again = client_with_run.post("/api/crawl", json={"source": "all"}).json()
    assert again["idempotent_replay"] is True
    second = client_with_run.get("/api/batches").json()
    assert second["total"] == first["total"]
    assert again["batch_id"] == first["batches"][0]["batch_id"]


def test_all_six_pages_render(client_with_run):
    """六个页面都能渲染，且首页显示阶段卡与四账。"""
    home = client_with_run.get("/")
    assert home.status_code == 200
    html = home.text
    assert "阶段卡" in html or "stage-card" in html
    for path in ("/policies", "/quarantine", "/batches", "/export"):
        assert client_with_run.get(path).status_code == 200
    chain_page = client_with_run.get("/policies/澄工信发〔2026〕37号")
    assert chain_page.status_code == 200
    assert "版本时间线" in chain_page.text


def test_unknown_batch_returns_404(client):
    """失败输入：不存在的批次返回 404。"""
    assert client.get("/api/crawl/b000000000000000").status_code == 404
    assert client.get("/api/policies/b000000000000000").status_code == 404


def test_http_sample_site_optional():
    """容器内真实走 HTTP 抓样例站；本地无样例站时跳过（记录为未运行）。"""
    base = os.getenv("SAMPLE_SITE_BASE", "http://sample-site")
    try:
        import httpx
        httpx.get(f"{base}/a/list_p1.html", timeout=1.5)
    except Exception:
        pytest.skip("样例站不可达，跳过真实 HTTP 采信（容器内会执行）")
    from app import config
    from app.modules.m1_ingest import reader, service
    from app.store import Store
    from app.modules.m5_batches import service as batch_service
    import tempfile
    settings = config.Settings.from_env()
    settings = config.Settings(db_path=tempfile.mktemp(suffix=".sqlite"),
                               sample_site_base=base)
    store = Store(settings.db_path)
    result = batch_service.run(settings=settings, store=store, source="all",
                               fetcher=reader.HttpFetcher(timeout=3.0))
    assert result["status"] == "done"
    assert result["totals"]["rows"] == str(X.ROWS_SEEN)
    assert result["reconciliation"]["all_balanced"] is True
