#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Core 聚合引擎。

职责：调度 Adapter、维护健康状态、向订阅者广播。
边界：本文件不出现任何 WorkBuddy 的字段名或路径 —— 那些都在 Adapter 和 config 里。
"""

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Dict, List

import config
from adapters.sqlite_adapter import SqliteAdapter
from adapters.trace_adapter import TraceAdapter
from contracts import Capability, HealthState
from core.store import Store

log = logging.getLogger("wbum.engine")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


class Engine:
    """按频率把 Adapter 分成两组：primary（高频增量）+ secondary（低频快照）。

    Core 只知道「两组不同频率的采集器」，不知道它们具体读什么。
    新增数据源时只加一个 Adapter，不改本类。
    """

    def __init__(self, store: Store, primary=None, secondary=None):
        self.store = store
        self.primary = primary if primary is not None else [TraceAdapter()]
        self.secondary = secondary if secondary is not None else [SqliteAdapter()]
        self.adapters = list(self.primary) + list(self.secondary)
        self.health = HealthState()
        self.caps: Dict[str, Capability] = {}
        self._subs: List[Callable[[dict], None]] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._last_pulse = ""

    # ---------- 健康状态 ----------

    def refresh_health(self):
        reasons, ok_all = [], True
        for name, cap in self.caps.items():
            if not cap.ok:
                ok_all = False
                reasons.append(f"{name}: {cap.detail}")
        with self._lock:
            if ok_all and self.health.status != "OK":
                log.info("恢复正常")
                self.health = HealthState("OK", None, [])
            elif not ok_all:
                if self.health.status != "DEGRADED":
                    log.warning("进入降级: %s", "; ".join(reasons))
                    self.health = HealthState("DEGRADED", _now(), reasons)
                else:
                    self.health.reasons = reasons

    def probe_all(self):
        for a in self.adapters:
            try:
                cap = a.probe()
            except Exception as exc:                       # Adapter 不应抛，兜底
                cap = Capability(getattr(a, "name", "?"), False, f"probe 异常: {exc}")
            self.caps[cap.name] = cap
        self.refresh_health()

    # ---------- 采集 ----------

    def collect_once(self) -> Dict[str, int]:
        """跑一轮全量采集。返回每个 Adapter 各自的产出量（口径由 Adapter 自定）。"""
        stats: Dict[str, int] = {}
        for a in self.adapters:
            name = getattr(a, "name", "?")
            try:
                stats[name] = a.collect(self.store) or 0
            except Exception as exc:                       # 单个 Adapter 崩了不影响其他
                log.exception("adapter %s 采集异常: %s", name, exc)
                self.caps[name] = Capability(name, False, f"采集异常: {exc}")
                stats[name] = 0
        self.refresh_health()
        return stats

    # ---------- 快照与广播 ----------

    def snapshot(self) -> dict:
        data = self.store.aggregate()
        data["health"] = self.health.to_dict()
        data["capabilities"] = {k: v.to_dict() for k, v in self.caps.items()}
        data["generated_at"] = _now()
        return data

    def subscribe(self, fn: Callable[[dict], None]):
        with self._lock:
            self._subs.append(fn)

    def unsubscribe(self, fn: Callable[[dict], None]):
        with self._lock:
            if fn in self._subs:
                self._subs.remove(fn)

    def publish(self, event: dict):
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(event)
            except Exception:
                log.exception("订阅者回调异常，已丢弃该订阅者")
                self.unsubscribe(fn)

    def pulse(self, added: int = 0, force: bool = False):
        """有变化才推，避免空转刷屏。"""
        snap = self.snapshot()
        key = f"{snap['totals']['requests']}|{len(snap['sessions'])}|{snap['health']['status']}"
        if force or added or key != self._last_pulse:
            self._last_pulse = key
            self.publish({"type": "update", "data": snap})
            return True
        return False

    # ---------- 调度 ----------

    def _loop(self, interval: float, fn, name: str):
        next_at = 0.0
        import time
        while not self._stop.is_set():
            start = time.monotonic()
            try:
                fn()
            except Exception:
                log.exception("%s 循环异常", name)
            next_at = start + interval
            self._stop.wait(max(0.05, next_at - time.monotonic()))

    def start(self):
        self.probe_all()
        self.collect_once()
        self.pulse(force=True)

        def tick_primary():
            added = 0
            drifted = False
            for a in self.primary:
                try:
                    added += a.collect(self.store) or 0
                    drifted = drifted or bool(getattr(a, "drift_flag", False))
                except Exception:
                    log.exception("%s 采集异常", getattr(a, "name", "?"))
            if drifted:
                # 字段漂移必须立刻反映到能力状态，否则要等 300 秒一次的自检才亮灯
                self.probe_all()
                self.refresh_health()
                self.pulse(force=True)
                return
            if added:
                self.refresh_health()
                self.pulse(added)

        def tick_secondary():
            for a in self.secondary:
                try:
                    a.collect(self.store)
                except Exception:
                    log.exception("%s 采集异常", getattr(a, "name", "?"))
            self.refresh_health()
            self.pulse()

        def tick_probe():
            self.probe_all()
            self.pulse(force=True)

        self.threads = [
            threading.Thread(target=self._loop, args=(config.TRACE_POLL_SEC, tick_primary, "primary"), daemon=True, name="primary-loop"),
            threading.Thread(target=self._loop, args=(config.SQLITE_POLL_SEC, tick_secondary, "secondary"), daemon=True, name="secondary-loop"),
            threading.Thread(target=self._loop, args=(config.PROBE_INTERVAL_SEC, tick_probe, "probe"), daemon=True, name="probe-loop"),
        ]
        for t in self.threads:
            t.start()
        log.info("引擎已启动：trace %.0fs / sqlite %.0fs / probe %.0fs",
                 config.TRACE_POLL_SEC, config.SQLITE_POLL_SEC, config.PROBE_INTERVAL_SEC)

    def stop(self):
        self._stop.set()
        for t in getattr(self, "threads", []):
            t.join(timeout=3)
