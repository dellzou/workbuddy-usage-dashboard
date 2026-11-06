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
from datetime import date, datetime, timedelta, timezone
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

-- 会话表只累积、不删除：历史会话必须始终可被检索到。
-- live 标记「本次采集时上游仍存在」，用于排序，不用于过滤。
CREATE TABLE IF NOT EXISTS session_snapshot (
    session_ref TEXT PRIMARY KEY,
    title       TEXT,
    model       TEXT,
    ctx_used    INTEGER NOT NULL DEFAULT 0,
    ctx_size    INTEGER NOT NULL DEFAULT 0,
    rounds      INTEGER NOT NULL DEFAULT 0,
    credit      REAL    NOT NULL DEFAULT 0,
    last_active TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    live        INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sess_credit ON session_snapshot(credit);
CREATE INDEX IF NOT EXISTS idx_sess_title  ON session_snapshot(title);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


# ---------- 时间分桶（纯日期运算，与 WorkBuddy 无关）----------

GRANULARITIES = ("day", "week", "month", "year")

_GRAN_LABEL = {
    "day": "近 7 天",
    "week": "近 4 周",
    "month": "近 12 个月",
    "year": "近 3 年",
}


def _shift_month(d: date, n: int) -> date:
    """按月偏移，结果固定在当月 1 号。"""
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return date(y, m, 1)


def _buckets(gran: str, today: date):
    """返回该粒度下完整的时间桶列表：[(start, end, label), ...]，含无数据的空桶。

    空桶必须保留，否则趋势图会把「没干活的那几天」直接跳过，看起来像天天在用。
    """
    out = []
    if gran == "day":
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            out.append((d, d, d.strftime("%m-%d")))
    elif gran == "week":
        cur = today - timedelta(days=today.weekday())          # 本周一
        for i in range(3, -1, -1):
            s = cur - timedelta(weeks=i)
            out.append((s, s + timedelta(days=6), s.strftime("%m-%d") + " 周"))
    elif gran == "month":
        cur = today.replace(day=1)
        for i in range(11, -1, -1):
            s = _shift_month(cur, -i)
            out.append((s, _shift_month(s, 1) - timedelta(days=1), s.strftime("%Y-%m")))
    else:                                                      # year
        for i in range(2, -1, -1):
            y = today.year - i
            out.append((date(y, 1, 1), date(y, 12, 31), str(y)))
    return out


_FIELDS = ("requests", "input_new", "input_cached", "output", "output_reasoning", "total")


def build_series(by_day: Dict[date, tuple], by_model: Dict[tuple, tuple] = None,
                 today: date = None) -> Dict[str, Any]:
    """把「按天聚合」折叠成四种粒度的序列。纯函数，便于单测。

    by_model: {(date, model): (requests, total)}，用于让模型分布也跟随所选范围，
              避免「趋势图是近 7 天、模型分布却是全部历史」这种自相矛盾。
    """
    by_model = by_model or {}
    today = today or date.today()
    out = {}
    for gran in GRANULARITIES:
        spans = _buckets(gran, today)
        buckets = []
        sums = dict.fromkeys(_FIELDS, 0)
        agg_model: Dict[str, list] = {}

        # 先建「日期 -> 桶序号」索引，模型维度只扫一遍，不按桶重复扫
        idx: Dict[date, int] = {}
        for i, (start, end, _label) in enumerate(spans):
            d = start
            while d <= end:
                idx[d] = i
                d += timedelta(days=1)

        model_by_bucket: List[Dict[str, list]] = [dict() for _ in spans]
        for (day, model), mv in by_model.items():
            i = idx.get(day)
            if i is None:
                continue
            slot = model_by_bucket[i].setdefault(model, [0, 0])
            slot[0] += mv[0] or 0
            slot[1] += mv[1] or 0

        for i, (start, end, label) in enumerate(spans):
            acc = dict.fromkeys(_FIELDS, 0)
            d = start
            while d <= end:
                row = by_day.get(d)
                if row:
                    for k, v in zip(_FIELDS, row):
                        acc[k] += v or 0
                d += timedelta(days=1)
            for k in _FIELDS:
                sums[k] += acc[k]
            for model, mv in model_by_bucket[i].items():
                slot = agg_model.setdefault(model, [0, 0])
                slot[0] += mv[0]
                slot[1] += mv[1]
            buckets.append(
                {
                    "key": start.isoformat(),
                    "label": label,
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                    **acc,
                }
            )
        out[gran] = {
            "label": _GRAN_LABEL[gran],
            "buckets": buckets,
            "totals": sums,
            "models": [
                {"model": m, "requests": v[0], "total": v[1]}
                for m, v in sorted(agg_model.items(), key=lambda kv: -kv[1][1])
            ],
        }
    return out


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
            self._migrate()
            self._conn.commit()

    def _migrate(self):
        """老库平滑升级：缺列就补，不动已有数据。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(session_snapshot)")}
        for name, ddl in (
            ("first_seen", "TEXT"),
            ("last_seen", "TEXT"),
            ("live", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE session_snapshot ADD COLUMN {name} {ddl}")
        # 升级前的会话一律视为历史（live 保持 1 也无妨，下一次采集会重判）
        self._conn.execute(
            "UPDATE session_snapshot SET first_seen=COALESCE(first_seen,updated_at),"
            " last_seen=COALESCE(last_seen,last_active,updated_at)"
        )

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
        """写入本轮的会话快照。

        名字沿用旧的，语义已改为「累积上屏」：
          - 上游仍在的会话更新并标记 live=1
          - 上游已消失的会话保留下来，只把 live 置 0（历史记录不能丢）
        因此这个表只增不减，历史会话永远检索得到。
        """
        if not snaps:
            # 采集失败时不能把全表标成离线，直接跳过
            return 0
        now = _now()
        with self._lock:
            self._conn.execute("UPDATE session_snapshot SET live=0")
            self._conn.executemany(
                """INSERT INTO session_snapshot
                   (session_ref,title,model,ctx_used,ctx_size,rounds,credit,
                    last_active,first_seen,last_seen,live,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,1,?)
                   ON CONFLICT(session_ref) DO UPDATE SET
                     title       = excluded.title,
                     model       = excluded.model,
                     ctx_used    = excluded.ctx_used,
                     ctx_size    = excluded.ctx_size,
                     rounds      = excluded.rounds,
                     credit      = excluded.credit,
                     last_active = excluded.last_active,
                     last_seen   = excluded.last_seen,
                     live        = 1,
                     updated_at  = excluded.updated_at""",
                [
                    (s.session_ref, s.title or "(无标题)", s.model, s.ctx_used, s.ctx_size,
                     s.rounds, s.credit, s.last_active, now, now, now)
                    for s in snaps
                ],
            )
            self._conn.commit()
            return len(snaps)

    # ---------- 会话检索 ----------

    @staticmethod
    def _like_escape(q: str) -> str:
        """转义 LIKE 通配符，避免用户输入的 % 或 _ 把整表都匹配出来。"""
        return (
            q.replace("\\", "\\\\")
             .replace("%", "\\%")
             .replace("_", "\\_")
        )

    def search_sessions(self, q: str = "", offset: int = 0, limit: int = 24):
        """按标题检索全部会话（含历史上游已消失的）。

        返回 (rows, total)。total 是**匹配总数**而不是本页条数，
        前端据此显示「已显示 x / 共 y」并决定还要不要继续翻页。
        """
        q = (q or "").strip()
        where, args = "", []
        if q:
            where = "WHERE title LIKE ? ESCAPE '\\'"
            args = [f"%{self._like_escape(q)}%"]

        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM session_snapshot {where}", args
            ).fetchone()[0]
            rows = self._conn.execute(
                f"""SELECT session_ref,title,model,ctx_used,ctx_size,rounds,credit,
                           last_active,live,first_seen,last_seen
                    FROM session_snapshot {where}
                    ORDER BY live DESC, credit DESC, last_active DESC, title ASC
                    LIMIT ? OFFSET ?""",
                args + [max(1, limit), max(0, offset)],
            ).fetchall()

        return [
            {
                "session_ref": r[0],
                "title": r[1] or "(无标题)",
                "model": r[2] or "",
                "ctx_used": r[3] or 0,
                "ctx_size": r[4] or 1,
                "rounds": r[5] or 0,
                "credit": round(r[6] or 0, 2),
                "last_active": r[7] or "",
                "live": bool(r[8]),
                "first_seen": r[9] or "",
                "last_seen": r[10] or "",
            }
            for r in rows
        ], total

    def session_total(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM session_snapshot").fetchone()[0]

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

            # 一次取出「按天」的完整结果：四种粒度都由它折叠而来，
            # 既保证四种口径同源（不会互相对不上），也避免重复扫表。
            daily_all = c.execute(
                """SELECT day, COUNT(*), COALESCE(SUM(input_new),0), COALESCE(SUM(input_cached),0),
                          COALESCE(SUM(output),0), COALESCE(SUM(output_reasoning),0),
                          COALESCE(SUM(total),0)
                   FROM usage_record GROUP BY day ORDER BY day"""
            ).fetchall()

            models = c.execute(
                """SELECT model, COUNT(*), SUM(total) FROM usage_record
                   GROUP BY model ORDER BY SUM(total) DESC"""
            ).fetchall()

            model_days = c.execute(
                """SELECT model, day, COUNT(*), COALESCE(SUM(total),0)
                   FROM usage_record GROUP BY model, day"""
            ).fetchall()

            sessions = c.execute(
                """SELECT session_ref,title,model,ctx_used,ctx_size,rounds,credit,last_active
                   FROM session_snapshot ORDER BY live DESC, credit DESC LIMIT 20"""
            ).fetchall()

            sess_total = c.execute("SELECT COUNT(*) FROM session_snapshot").fetchone()[0]
            credit_sum = c.execute(
                "SELECT COALESCE(SUM(credit),0) FROM session_snapshot"
            ).fetchone()[0]

            hourly = c.execute(
                """SELECT substr(ts,1,13) AS h, SUM(total) FROM usage_record
                   WHERE day >= date('now','-7 day') GROUP BY h ORDER BY h"""
            ).fetchall()

        by_day: Dict[date, tuple] = {}
        for r in daily_all:
            try:
                by_day[date.fromisoformat(str(r[0])[:10])] = tuple(r[1:7])
            except (ValueError, TypeError):
                continue

        by_model: Dict[tuple, tuple] = {}
        for r in model_days:
            try:
                by_model[(date.fromisoformat(str(r[1])[:10]), r[0])] = (r[2], r[3])
            except (ValueError, TypeError):
                continue

        daily = daily_all[-days:]

        return {
            "schema_version": 1,
            "totals": {
                "requests": tot[0],
                "input_new": tot[1],
                "input_cached": tot[2],
                "output": tot[3],
                "output_reasoning": tot[4],
                "total": tot[5],
                "credit": round(credit_sum or 0, 2),
                "sessions": sess_total,
            },
            "range": {"from": tot[6], "to": tot[7]},
            "daily": [
                {
                    "day": r[0], "requests": r[1], "input_new": r[2], "input_cached": r[3],
                    "output": r[4], "output_reasoning": r[5], "total": r[6],
                }
                for r in daily
            ],
            "series": build_series(by_day, by_model),
            "models": [{"model": r[0], "requests": r[1], "total": r[2]} for r in models],
            "sessions": [
                {
                    "session_ref": r[0], "title": r[1] or "(无标题)", "model": r[2] or "",
                    "ctx_used": r[3], "ctx_size": r[4] or 1, "rounds": r[5],
                    "credit": round(r[6] or 0, 2), "last_active": r[7] or "",
                }
                for r in sessions
            ],
            "session_total": sess_total,
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
