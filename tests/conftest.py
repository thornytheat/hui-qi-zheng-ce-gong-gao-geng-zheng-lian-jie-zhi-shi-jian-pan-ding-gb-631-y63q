"""测试固定装置：临时库、固定评估日、离线样例站 fetcher。

约定：所有时间断言都用固定评估日 2026-11-15，不读 datetime.now()。
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config                                    # noqa: E402
from app.main import create_app                           # noqa: E402
from app.modules.m1_ingest import reader                  # noqa: E402
from app.modules.m5_batches import service as batch_service  # noqa: E402
from app.store import Store                               # noqa: E402

EVAL_DATE = dt.date(2026, 11, 15)


@pytest.fixture()
def settings(tmp_path) -> config.Settings:
    return config.Settings(db_path=str(tmp_path / "noticechain.sqlite"),
                           eval_date=EVAL_DATE,
                           sample_site_dir=str(ROOT / "sample_site"),
                           sample_site_base="http://sample-site")


@pytest.fixture()
def store(settings) -> Store:
    return Store(settings.db_path)


@pytest.fixture()
def fetcher(settings) -> reader.FileFetcher:
    return reader.FileFetcher(settings.sample_site_dir, base=settings.sample_site_base)


@pytest.fixture()
def run(settings, store) -> dict:
    return batch_service.run(settings=settings, store=store, source="all")


@pytest.fixture()
def app(settings):
    return create_app(settings)


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture()
def client_with_run(client) -> TestClient:
    response = client.post("/api/crawl", json={"source": "all"})
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    return client


@pytest.fixture()
def batch_id(run) -> str:
    return run["batch"]["batch_id"]


@pytest.fixture()
def site_dir(settings) -> pathlib.Path:
    return pathlib.Path(settings.sample_site_dir)
