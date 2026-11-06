#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SqliteAdapter —— 读 WorkBuddy 自己的库，取会话级快照（上下文占用 / 积分）。

安全底线：
  - 以 mode=ro 只读打开，绝不写入，也不改 WorkBuddy 的任何状态
  - 表或列不存在时返回空快照 + 能力缺失，不抛异常
  - 每次全量替换 session_snapshot（快照语义，不是累加）
"""

import json
import os
import sqlite3
from typing import List

import config
from adapters.base import Adapter
from contracts import Capability, SessionSnapshot
from core.store import Store


class SqliteAdapter(Adapter):
    name = "sqlite"

    def _connect(self):
        uri = f"file:{config.WB_DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        conn.execute("PRAGMA query_only=1")
        return conn

    # ---------- 列名候选 ----------

    @staticmethod
    def _columns(conn, table: str) -> List[str]:
        try:
            return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        except sqlite3.Error:
            return []

    def _resolve(self, conn, key: str, table_cols: dict):
        """在候选里挑第一个真实存在的列名。"""
        for name in config.SQLITE_COLUMNS[key]:
            if name in table_cols:
                return name
        return None

    # ---------- 采集 ----------

    def collect(self, store: Store) -> int:
        snaps = self.fetch()
        return store.replace_snapshots(snaps)

    def fetch(self) -> List[SessionSnapshot]:
        if not os.path.exists(config.WB_DB_PATH):
            return []
        try:
            conn = self._connect()
        except sqlite3.Error:
            return []

        try:
            usage_table = next(
                (t for t in config.SQLITE_COLUMNS["table_usage"]
                 if self._columns(conn, t)),
                None,
            )
            sess_table = next(
                (t for t in config.SQLITE_COLUMNS["table_sessions"]
                 if self._columns(conn, t)),
                None,
            )
            if not usage_table or not sess_table:
                return []

            ucols = set(self._columns(conn, usage_table))
            scols = set(self._columns(conn, sess_table))
            C = config.SQLITE_COLUMNS

            uid = self._resolve(conn, "usage_session_id", ucols)
            used = self._resolve(conn, "usage_used", ucols)
            size = self._resolve(conn, "usage_size", ucols)
            credit = self._resolve(conn, "usage_credit", ucols)
            sid = self._resolve(conn, "session_id", scols)
            model = self._resolve(conn, "session_model", scols)
            last = self._resolve(conn, "session_last", scols)

            # 标题可能有 custom_title / title 两列，且前者常为空 —— 用 coalesce 兜住
            title_cands = [c for c in config.SQLITE_COLUMNS["session_title"] if c in scols]
            if not title_cands:
                title_expr = "''"
            elif len(title_cands) == 1:
                title_expr = f's."{title_cands[0]}"'
            else:
                parts = ", ".join(f'nullif(s."{c}",\'\')' for c in title_cands)
                title_expr = f"coalesce({parts})"

            if not (uid and sid and used):
                return []

            sel = [
                f"u.{uid}",
                f"s.{sid}",
                f"u.{used}",
                f"u.{size}" if size else "0",
                f"u.{credit}" if credit else "'{}'",
                title_expr,
                f"s.{model}" if model else "''",
                f"s.{last}" if last else "0",
            ]
            sql = (
                f'SELECT {", ".join(sel)} FROM "{usage_table}" u '
                f'LEFT JOIN "{sess_table}" s ON s."{sid}" = u."{uid}" '
                f'WHERE u."{used}" > 0'
            )

            out: List[SessionSnapshot] = []
            for row in conn.execute(sql):
                credit_obj = {}
                try:
                    credit_obj = json.loads(row[4] or "{}")
                except (TypeError, ValueError):
                    credit_obj = {}
                rounds = len(credit_obj) if isinstance(credit_obj, dict) else 0
                credit_val = (
                    round(sum(v for v in credit_obj.values() if isinstance(v, (int, float))), 2)
                    if isinstance(credit_obj, dict) else 0.0
                )
                last_raw = row[7] or 0
                try:
                    from datetime import datetime
                    last_iso = datetime.fromtimestamp(float(last_raw) / 1000).astimezone().isoformat()
                except (TypeError, ValueError, OSError):
                    last_iso = ""

                out.append(
                    SessionSnapshot(
                        session_ref=str(row[1] or row[0]),
                        title=(row[5] or "(无标题)")[:60],
                        model=row[6] or "",
                        ctx_used=int(row[2] or 0),
                        ctx_size=int(row[3] or 0),
                        rounds=rounds,
                        credit=credit_val,
                        last_active=last_iso,
                    )
                )
            return out
        except sqlite3.Error:
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ---------- 自检 ----------

    def probe(self) -> Capability:
        if not os.path.exists(config.WB_DB_PATH):
            return Capability(self.name, False, f"数据库不存在（{config.WB_DB_PATH}）")
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            return Capability(self.name, False, f"只读打开失败: {exc}")
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            need = set(config.SQLITE_COLUMNS["table_usage"]) | set(config.SQLITE_COLUMNS["table_sessions"])
            missing = need - tables
            if missing:
                return Capability(self.name, False, f"缺表: {sorted(missing)}")
            snaps = self.fetch()
            ok = len(snaps) > 0
            return Capability(
                self.name, ok,
                f"可读 {len(snaps)} 个会话" if ok else "表存在但无可读会话",
                parsed=len(snaps), failed=0,
            )
        except sqlite3.Error as exc:
            return Capability(self.name, False, f"查询失败: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
