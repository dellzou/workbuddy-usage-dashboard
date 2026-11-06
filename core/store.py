#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Core 存储层：SQLite 落库、水位、聚合。

本文件不认识 WorkBuddy —— 只接收 contracts 里的类型。
水位表让「重启后不重不漏」成为存储层的性质，而不是各 Adapter 各自处理的细节。
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Iterable, List, Dict, Any, Optional

import config
from contracts import UsageRecord, SessionSnapshot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_record (
    record_id        TEXT PRIMARY KEY,
    ts               TEXT NOT NULL,
    day              TEXT NOT NULL,
    model            TEXT NOT NULL,
    input_new        INTEGER NOT NULL DEFAULT 0,
    input_cached     INTEGER NOT NULL DEFAULT 0,
    output           INTEGER NOT NULL DEFAULT 0,
    output_reasoning INTEGER NOT NULL DEFAULT 0,
    total            INTEGER NOT NULL DEFAULT 0,
    source           TEXT NOT NULL DEFAULT 'trace',
    session_ref      TEXT,
    raw_ref          TEXT,
    ingested_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_day   ON usage_record(day);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_record(model);
CREATE INDEX IF NOT EXISTS idx_usage_ts    ON usage_record(ts);

CREATE TABLE IF NOT EXISTS file_watermark (
    path       TEXT PRIMARY KEY,
    size       INTEGER NOT NULL DEFAULT 0,
    mtime      REAL    NOT NULL DEFAULT 0,
    offset     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS session_snapshot (
    session_ref TEXT PRIMARY KEY,
    title       TEXT,
    model       TEXT,
    ctx_used    INTEGER NOT NULL DEFAULT 0,
    ctx_size    INTEGER NOT NULL DEFAULT 0,
    rounds      INTEGER NOT NULL DEFAULT 0,
    credit      REAL    NOT NULL DEFAULT 0,
    last_active TEXT,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


class Store:
    def __init__(self, db_path: str = None):
        config.ensure_dirs()
        self.path = db_path or config.DB_PATH
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------- 写入 ----------

    def insert_records(self, records: Iterable[UsageRecord]) -> int:
        """幂等写入。返回真正新增的条数。"""
        rows = []
        now = _now()
        for r in records:
            day = r.ts[:10]
            rows.append(
                (
                    r.record_id, r.ts, day, r.model, r.input_new, r.input_cached,
                    r.output, r.output_reasoning, r.total, r.source,
                    r.session_ref, r.raw_ref, now,
                )
            )
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                """INSERT OR IGNORE INTO usage_record
                   (record_id,ts,day,model,input_new,input_cached,output,
                    output_reasoning,total,source,session_ref,raw_ref,ingested_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def replace_snapshots(self, snaps: List[SessionSnapshot]) -> int:
        with self._lock:
            now = _now()
            self._conn.execute("DELETE FROM session_snapshot")
            self._conn.executemany(
                """INSERT OR REPLACE INTO session_snapshot
                   (session_ref,title,model,ctx_used,ctx_size,rounds,credit,last_active,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (s.session_ref, s.title, s.model, s.ctx_used, s.ctx_size,
                     s.rounds, s.credit, s.last_active, now)
                    for s in snaps
                ],
            )
            self._conn.commit()
            return len(snaps)

    # ---------- 水位 ----------

    def get_watermark(self, path: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT path,size,mtime,offset FROM file_watermark WHERE path=?", (path,)
            )
            row = cur.fetchone()
        if not row:
            return None
        return {"path": row[0], "size": row[1], "mtime": row[2], "offset": row[3]}

    def set_watermark(self, path: str, size: int, mtime: float, offset: int):
        with self._lock:
            self._conn.execute(
                """INSERT INTO file_watermark(path,size,mtime,offset,updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                     size=excluded.size, mtime=excluded.mtime,
                     offset=excluded.offset, updated_at=excluded.updated_at""",
                (path, size, mtime, offset, _now()),
            )
            self._conn.commit()

    # ---------- 聚合 ----------

    def aggregate(self, days: int = 60) -> Dict[str, Any]:
        with self._lock:
            c = self._conn

            tot = c.execute(
                """SELECT COUNT(*), COALESCE(SUM(input_new),0), COALESCE(SUM(input_cached),0),
                          COALESCE(SUM(output),0), COALESCE(SUM(output_reasoning),0),
                          COALESCE(SUM(total),0), MIN(day), MAX(day)
                   FROM usage_record"""
            ).fetchone()

            daily = c.execute(
                """SELECT day, COUNT(*), SUM(input_new), SUM(input_cached),
                          SUM(output), SUM(output_reasoning), SUM(total)
                   FROM usage_record GROUP BY day ORDER BY day DESC LIMIT ?""",
                (days,),
            ).fetchall()

            models = c.execute(
                """SELECT model, COUNT(*), SUM(total) FROM usage_record
                   GROUP BY model ORDER BY SUM(total) DESC"""
            ).fetchall()

            sessions = c.execute(
                """SELECT session_ref,title,model,ctx_used,ctx_size,rounds,credit,last_active
                   FROM session_snapshot ORDER BY credit DESC LIMIT 30"""
            ).fetchall()

            hourly = c.execute(
                """SELECT substr(ts,1,13) AS h, SUM(total) FROM usage_record
                   WHERE day >= date('now','-7 day') GROUP BY h ORDER BY h"""
            ).fetchall()

        return {
            "schema_version": 1,
            "totals": {
                "requests": tot[0],
                "input_new": tot[1],
                "input_cached": tot[2],
                "output": tot[3],
                "output_reasoning": tot[4],
                "total": tot[5],
            },
            "range": {"from": tot[6], "to": tot[7]},
            "daily": [
                {
                    "day": r[0], "requests": r[1], "input_new": r[2], "input_cached": r[3],
                    "output": r[4], "output_reasoning": r[5], "total": r[6],
                }
                for r in reversed(daily)
            ],
            "models": [{"model": r[0], "requests": r[1], "total": r[2]} for r in models],
            "sessions": [
                {
                    "session_ref": r[0], "title": r[1] or "(无标题)", "model": r[2] or "",
                    "ctx_used": r[3], "ctx_size": r[4] or 1, "rounds": r[5],
                    "credit": round(r[6] or 0, 2), "last_active": r[7] or "",
                }
                for r in sessions
            ],
            "hourly": [{"hour": r[0], "total": r[1]} for r in hourly],
        }

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM usage_record").fetchone()[0]

    def reset(self):
        """仅供验证脚本使用，生产不要调。"""
        with self._lock:
            self._conn.executescript(
                "DELETE FROM usage_record; DELETE FROM file_watermark;"
                "DELETE FROM session_snapshot;"
            )
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()
