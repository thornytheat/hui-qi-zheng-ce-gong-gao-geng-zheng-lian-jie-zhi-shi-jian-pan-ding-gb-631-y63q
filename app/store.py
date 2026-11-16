"""SQLite 存储：批次、页面、公告、隔离、政策链、阶段计数、合计、运行清单。

约定：
- 金额列一律 TEXT（Decimal 字符串），不落 float，避免 2^53 与二进制误差；
- 一次运行的所有行在同一个事务里「先删后插」，失败整体回滚，不留半批数据；
- 读数按 batch_id 过滤，页面永远只展示一个批次。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    batch_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    input_digest TEXT NOT NULL,
    resume_of TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_batches_key ON batches(batch_key, status);
CREATE TABLE IF NOT EXISTS pages (
    batch_id TEXT NOT NULL, url TEXT NOT NULL, portal TEXT NOT NULL, kind TEXT NOT NULL,
    page_no INTEGER NOT NULL, status TEXT NOT NULL, http_status INTEGER,
    encoding TEXT, declared_charset TEXT, bytes INTEGER, sha256 TEXT,
    row_count INTEGER NOT NULL DEFAULT 0, code TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS announcements (
    batch_id TEXT NOT NULL, doc_no TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
    title TEXT NOT NULL, published_at TEXT NOT NULL, deadline_spec_json TEXT,
    amount_wan TEXT, amount_unit TEXT, target_field TEXT, target_value TEXT,
    portal TEXT NOT NULL, source_url TEXT NOT NULL, row_ref TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}', flags_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS quarantine (
    batch_id TEXT NOT NULL, code TEXT NOT NULL, stage TEXT NOT NULL, level TEXT NOT NULL,
    portal TEXT, source_url TEXT, row_ref TEXT, field TEXT, raw_value TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS chains (
    batch_id TEXT NOT NULL, doc_no TEXT NOT NULL, status TEXT NOT NULL, title TEXT NOT NULL,
    versions INTEGER NOT NULL, final_deadline TEXT, final_amount_wan TEXT,
    listed INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
    conflicts_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS chain_versions (
    batch_id TEXT NOT NULL, doc_no TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
    title TEXT NOT NULL, published_at TEXT NOT NULL, deadline_spec_json TEXT,
    amount_wan TEXT, target_field TEXT, target_value TEXT, portal TEXT,
    row_ref TEXT, source_url TEXT, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stages (
    batch_id TEXT NOT NULL, stage TEXT NOT NULL, in_count INTEGER NOT NULL,
    out_count INTEGER NOT NULL, quarantine_count INTEGER NOT NULL,
    status TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS totals (
    batch_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rulings (
    batch_id TEXT NOT NULL, doc_no TEXT NOT NULL, seq INTEGER NOT NULL,
    reviewer TEXT NOT NULL, basis TEXT NOT NULL, code TEXT NOT NULL,
    created_at TEXT NOT NULL, manual_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (batch_id, doc_no)
);
CREATE TABLE IF NOT EXISTS manifests (
    batch_id TEXT PRIMARY KEY, json TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- 批次 ----------
    def upsert_batch(self, batch: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO batches (batch_id, batch_key, run_id, source, status,
                        created_at, finished_at, input_digest, resume_of, params_json, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     input_digest=excluded.input_digest, note=excluded.note,
                     params_json=excluded.params_json""",
                (batch["batch_id"], batch["batch_key"], batch["run_id"], batch["source"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch.get("resume_of"),
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 batch.get("note", "")),
            )

    def find_done_by_key(self, batch_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE batch_key=? AND status='done' "
                "ORDER BY created_at LIMIT 1", (batch_key,)).fetchone()
        return dict(row) if row else None

    def get_batch(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def latest_batch(self, status: str = "done") -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM batches WHERE status=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (status,)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM batches ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def list_batches(self, limit: int = 30) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- 运行结果落库（单事务替换） ----------
    def save_run(self, *, batch: dict, pages: list[dict], announcements: list[dict],
                 quarantine: list[dict], chains: list[dict], versions: list[dict],
                 stages: list[dict], totals: dict, manifest: dict) -> None:
        with self.connect() as conn:
            for table in ("pages", "announcements", "quarantine", "chains",
                          "chain_versions", "stages", "totals"):
                conn.execute(f"DELETE FROM {table} WHERE batch_id=?", (batch["batch_id"],))
            for p in pages:
                conn.execute(
                    """INSERT INTO pages (batch_id,url,portal,kind,page_no,status,http_status,
                        encoding,declared_charset,bytes,sha256,row_count,code,note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch["batch_id"], p["url"], p["portal"], p["kind"], p["page_no"],
                     p["status"], p.get("http_status"), p.get("encoding"),
                     p.get("declared_charset"), p.get("bytes"), p.get("sha256"),
                     p.get("row_count", 0), p.get("code"), p.get("note", "")))
            for a in announcements:
                conn.execute(
                    """INSERT INTO announcements (batch_id,doc_no,seq,kind,title,published_at,
                        deadline_spec_json,amount_wan,amount_unit,target_field,target_value,
                        portal,source_url,row_ref,raw_json,flags_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch["batch_id"], a["doc_no"], a["seq"], a["kind"], a["title"],
                     a["published_at"], json.dumps(a.get("deadline_spec"), ensure_ascii=False),
                     None if a.get("amount_wan") is None else str(a["amount_wan"]),
                     a.get("amount_unit"), a.get("target_field"), a.get("target_value"),
                     a["portal"], a["source_url"], a["row_ref"],
                     json.dumps(a.get("raw", {}), ensure_ascii=False),
                     json.dumps(a.get("flags", []), ensure_ascii=False)))
            for q in quarantine:
                conn.execute(
                    """INSERT INTO quarantine (batch_id,code,stage,level,portal,source_url,
                        row_ref,field,raw_value,detail) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (batch["batch_id"], q["code"], q["stage"], q["level"], q.get("portal"),
                     q.get("source_url"), q.get("row_ref"), q.get("field"),
                     q.get("raw_value"), q.get("detail")))
            for c in chains:
                conn.execute(
                    """INSERT INTO chains (batch_id,doc_no,status,title,versions,
                        final_deadline,final_amount_wan,listed,note,conflicts_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (batch["batch_id"], c["doc_no"], c["status"], c["title"],
                     c["versions"], c.get("final_deadline"),
                     None if c.get("final_amount_wan") is None else str(c["final_amount_wan"]),
                     1 if c.get("listed") else 0, c.get("note", ""),
                     json.dumps(c.get("conflicts", []), ensure_ascii=False)))
            for v in versions:
                conn.execute(
                    """INSERT INTO chain_versions (batch_id,doc_no,seq,kind,title,published_at,
                        deadline_spec_json,amount_wan,target_field,target_value,portal,row_ref,
                        source_url,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch["batch_id"], v["doc_no"], v["seq"], v["kind"], v["title"],
                     v["published_at"], json.dumps(v.get("deadline_spec"), ensure_ascii=False),
                     None if v.get("amount_wan") is None else str(v["amount_wan"]),
                     v.get("target_field"), v.get("target_value"), v.get("portal"),
                     v.get("row_ref"), v.get("source_url"), v.get("note", "")))
            for s in stages:
                conn.execute(
                    """INSERT INTO stages (batch_id,stage,in_count,out_count,quarantine_count,
                        status,elapsed_ms,note) VALUES (?,?,?,?,?,?,?,?)""",
                    (batch["batch_id"], s["stage"], s["in_count"], s["out_count"],
                     s["quarantine_count"], s["status"], s["elapsed_ms"], s.get("note", "")))
            for key, value in totals.items():
                conn.execute("INSERT INTO totals (batch_id,key,value) VALUES (?,?,?)",
                             (batch["batch_id"], key, str(value)))
            conn.execute(
                "INSERT INTO manifests (batch_id,json) VALUES (?,?) "
                "ON CONFLICT(batch_id) DO UPDATE SET json=excluded.json",
                (batch["batch_id"], json.dumps(manifest, ensure_ascii=False)))
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,source,status,created_at,
                        finished_at,input_digest,resume_of,params_json,note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     note=excluded.note, input_digest=excluded.input_digest""",
                (batch["batch_id"], batch["batch_key"], batch["run_id"], batch["source"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch.get("resume_of"),
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 batch.get("note", "")))

    def save_attempt(self, *, batch: dict, pages: list[dict], stages: list[dict],
                     note: str = "") -> None:
        """失败尝试：只写批次、页面与阶段，不清空任何已有批次的数据。"""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO batches (batch_id,batch_key,run_id,source,status,created_at,
                        finished_at,input_digest,resume_of,params_json,note)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id) DO UPDATE SET
                     status=excluded.status, finished_at=excluded.finished_at,
                     note=excluded.note""",
                (batch["batch_id"], batch["batch_key"], batch["run_id"], batch["source"],
                 batch["status"], batch["created_at"], batch.get("finished_at"),
                 batch["input_digest"], batch.get("resume_of"),
                 json.dumps(batch.get("params", {}), ensure_ascii=False),
                 note or batch.get("note", "")))
            conn.execute("DELETE FROM pages WHERE batch_id=?", (batch["batch_id"],))
            conn.execute("DELETE FROM stages WHERE batch_id=?", (batch["batch_id"],))
            for p in pages:
                conn.execute(
                    """INSERT INTO pages (batch_id,url,portal,kind,page_no,status,http_status,
                        encoding,declared_charset,bytes,sha256,row_count,code,note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch["batch_id"], p["url"], p["portal"], p["kind"], p["page_no"],
                     p["status"], p.get("http_status"), p.get("encoding"),
                     p.get("declared_charset"), p.get("bytes"), p.get("sha256"),
                     p.get("row_count", 0), p.get("code"), p.get("note", "")))
            for s in stages:
                conn.execute(
                    """INSERT INTO stages (batch_id,stage,in_count,out_count,quarantine_count,
                        status,elapsed_ms,note) VALUES (?,?,?,?,?,?,?,?)""",
                    (batch["batch_id"], s["stage"], s["in_count"], s["out_count"],
                     s["quarantine_count"], s["status"], s["elapsed_ms"], s.get("note", "")))

    # ---------- 人工裁定（M7，按链唯一，重裁定覆盖并把旧值从留痕里挤掉） ----------
    def save_ruling(self, ruling: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO rulings (batch_id,doc_no,seq,reviewer,basis,code,created_at,
                        manual_json,note) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(batch_id,doc_no) DO UPDATE SET
                     seq=excluded.seq, reviewer=excluded.reviewer, basis=excluded.basis,
                     code=excluded.code, created_at=excluded.created_at,
                     manual_json=excluded.manual_json, note=excluded.note""",
                (ruling["batch_id"], ruling["doc_no"], int(ruling["seq"]),
                 ruling["reviewer"], ruling["basis"], ruling["code"],
                 ruling["created_at"], ruling.get("manual_json", "{}"),
                 ruling.get("note", "")))

    def load_rulings(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM rulings WHERE batch_id=? ORDER BY doc_no", (batch_id,))

    def load_ruling(self, batch_id: str, doc_no: str) -> dict | None:
        rows = self._all(
            "SELECT * FROM rulings WHERE batch_id=? AND doc_no=?", (batch_id, doc_no))
        return rows[0] if rows else None

    # ---------- 读取 ----------
    def load_pages(self, batch_id: str) -> list[dict]:
        return self._all("SELECT * FROM pages WHERE batch_id=? ORDER BY page_no, url", (batch_id,))

    def load_announcements(self, batch_id: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM announcements WHERE batch_id=? ORDER BY doc_no, seq, published_at",
            (batch_id,))
        for r in rows:
            r["deadline_spec"] = json.loads(r.pop("deadline_spec_json") or "null")
            r["raw"] = json.loads(r.pop("raw_json") or "{}")
            r["flags"] = json.loads(r.pop("flags_json") or "[]")
            r["amount_wan"] = None if r["amount_wan"] is None else _dec(r["amount_wan"])
        return rows

    def load_quarantine(self, batch_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM quarantine WHERE batch_id=? ORDER BY stage, code, row_ref", (batch_id,))

    def load_chains(self, batch_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM chains WHERE batch_id=? ORDER BY doc_no", (batch_id,))
        for r in rows:
            r["conflicts"] = json.loads(r.pop("conflicts_json") or "[]")
            r["final_amount_wan"] = (None if r["final_amount_wan"] is None
                                     else _dec(r["final_amount_wan"]))
            r["listed"] = bool(r["listed"])
        return rows

    def load_chain(self, batch_id: str, doc_no: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM chains WHERE batch_id=? AND doc_no=?", (batch_id, doc_no)).fetchone()
            if row is None:
                return None
            versions = [dict(r) for r in conn.execute(
                "SELECT * FROM chain_versions WHERE batch_id=? AND doc_no=? ORDER BY seq",
                (batch_id, doc_no)).fetchall()]
        chain = dict(row)
        chain["conflicts"] = json.loads(chain.pop("conflicts_json") or "[]")
        chain["final_amount_wan"] = (None if chain["final_amount_wan"] is None
                                     else _dec(chain["final_amount_wan"]))
        chain["listed"] = bool(chain["listed"])
        for v in versions:
            v["deadline_spec"] = json.loads(v.pop("deadline_spec_json") or "null")
            v["amount_wan"] = None if v["amount_wan"] is None else _dec(v["amount_wan"])
        chain["version_rows"] = versions
        return chain

    def load_stages(self, batch_id: str) -> list[dict]:
        from . import config
        rows = self._all("SELECT * FROM stages WHERE batch_id=? ORDER BY rowid", (batch_id,))
        for row in rows:
            row["label"] = config.STAGE_LABELS.get(row["stage"], row["stage"])
        return rows

    def load_totals(self, batch_id: str) -> dict:
        rows = self._all("SELECT key, value FROM totals WHERE batch_id=?", (batch_id,))
        return {r["key"]: r["value"] for r in rows}

    def load_manifest(self, batch_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT json FROM manifests WHERE batch_id=?", (batch_id,)).fetchone()
        return json.loads(row["json"]) if row else None

    def count_rows(self, batch_id: str) -> dict:
        with self.connect() as conn:
            out = {}
            for table in ("pages", "announcements", "quarantine", "chains", "chain_versions"):
                out[table] = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE batch_id=?", (batch_id,)
                ).fetchone()["n"]
        return out

    def _all(self, sql: str, args: tuple) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _dec(text: str):
    from decimal import Decimal
    return Decimal(text)
