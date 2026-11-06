#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
TraceAdapter —— 唯一知道 WorkBuddy trace 长什么样的地方。

WorkBuddy 升级后如果采集挂了，90% 的改动应该只发生在本文件：
  - 目录变了        -> 改 config.TRACE_GLOB_DIR / TRACE_PATTERNS
  - 只是字段改名    -> 改 config.USAGE_MAP，本文件不用动
  - 结构变了        -> 改 _parse_line()

三条硬保证：
  1. 只读：以 'rb' 打开，从不写入
  2. 半行不消费：文件正在写时读到没换行的尾巴，offset 不前进，下次重读
  3. 异常不外抛：单行解析失败只计数，让整个采集继续
"""

import glob
import hashlib
import json
import os
from typing import List, Optional

import config
from adapters.base import Adapter
from contracts import Capability, UsageRecord
from core.store import Store


class TraceAdapter(Adapter):
    name = "trace"

    # ---------- 定位 ----------

    def files(self) -> List[str]:
        found = set()
        for pat in config.TRACE_PATTERNS:
            found.update(glob.glob(os.path.join(config.TRACE_GLOB_DIR, pat)))
        return sorted(found)

    # ---------- 字段映射 ----------

    @staticmethod
    def _pick(node, dotted: str):
        cur = node
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    def _map_usage(self, resp: dict) -> dict:
        usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
        out = {}
        for target, candidates in config.USAGE_MAP.items():
            val = None
            for path in candidates:
                val = self._pick(usage, path)
                if val is not None:
                    break
            try:
                out[target] = int(val or 0)
            except (TypeError, ValueError):
                out[target] = 0
        return out

    # ---------- 单行解析 ----------

    def parse_line(self, raw: str, path: str, pos: int) -> List[UsageRecord]:
        """返回该行贡献的记录；非目标行返回空列表；坏行抛异常由调用方计数。"""
        obj = json.loads(raw)
        if obj.get("type") != "span_end":
            return []
        span = obj.get("span") or {}
        if span.get("type") != "generation":
            return []

        payload = span.get("toolOutput")
        if not payload:
            return []
        arr = json.loads(payload)
        if isinstance(arr, dict):
            arr = [arr]
        if not isinstance(arr, list):
            return []

        session = os.path.basename(path)[6:-6]  # trace_<id>.jsonl
        ts = span.get("startedAt") or ""
        recs: List[UsageRecord] = []

        for resp in arr:
            if not isinstance(resp, dict):
                continue
            if not isinstance(resp.get("usage"), dict) or not resp.get("usage"):
                continue

            v = self._map_usage(resp)
            rid = resp.get("id")
            if not rid:
                rid = hashlib.md5(f"{path}:{pos}:{resp.get('created')}".encode()).hexdigest()

            total = v["total"] or (v["input_total"] + v["output"])
            recs.append(
                UsageRecord(
                    record_id=str(rid),
                    ts=ts,
                    model=resp.get("model") or "unknown",
                    input_new=max(v["input_total"] - v["input_cached"], 0),
                    input_cached=v["input_cached"],
                    output=v["output"],
                    output_reasoning=v["output_reasoning"],
                    total=total,
                    source="trace",
                    session_ref=f"trace:{session}",
                    raw_ref=f"{os.path.basename(path)}@{pos}",
                )
            )
        return recs

    # ---------- 增量采集 ----------

    def collect(self, store: Store) -> int:
        added = 0
        for path in self.files():
            try:
                st = os.stat(path)
            except OSError:
                continue

            wm = store.get_watermark(path)
            offset = wm["offset"] if wm else 0
            if st.st_size < offset:          # 文件被截断或重建
                offset = 0
            if wm and offset >= st.st_size and wm["mtime"] == st.st_mtime:
                continue                      # 无变化，跳过

            batch: List[UsageRecord] = []
            new_offset = offset
            try:
                with open(path, "rb") as f:
                    f.seek(offset)
                    while True:
                        line = f.readline()
                        if not line:
                            break
                        if not line.endswith(b"\n"):
                            break                 # 半行：不消费，等下次补齐
                        pos = new_offset
                        new_offset += len(line)
                        text = line.decode("utf-8", "ignore").strip()
                        if not text:
                            continue
                        try:
                            batch.extend(self.parse_line(text, path, pos))
                        except Exception:
                            continue              # 坏行：跳过，不影响其他行
            except OSError:
                continue

            if batch:
                added += store.insert_records(batch)
            store.set_watermark(path, st.st_size, st.st_mtime, new_offset)
        return added

    # ---------- 自检 ----------

    @staticmethod
    def _tail_lines(path: str, limit: int = 200) -> List[str]:
        size = os.path.getsize(path)
        read = min(size, 512 * 1024)
        with open(path, "rb") as f:
            f.seek(size - read)
            chunk = f.read(read)
        lines = chunk.decode("utf-8", "ignore").splitlines()
        return lines[-limit:]

    def probe(self) -> Capability:
        try:
            files = self.files()
        except Exception as exc:                       # pragma: no cover
            return Capability(self.name, False, f"定位失败: {exc}")

        if not files:
            return Capability(
                self.name, False, f"未找到 trace 文件（{config.TRACE_GLOB_DIR}）"
            )

        recent = sorted(files, key=lambda p: os.path.getmtime(p), reverse=True)[:3]
        parsed = failed = 0
        for path in recent:
            try:
                lines = self._tail_lines(path)
            except OSError:
                continue
            for line in lines:
                if '"generation"' not in line:
                    continue
                try:
                    self.parse_line(line, path, 0)
                    parsed += 1
                except Exception:
                    failed += 1

        ok = parsed > 0 and (failed / max(parsed + failed, 1)) <= config.DEGRADED_FAIL_RATIO
        detail = f"抽样 {len(recent)} 个文件" if ok else "样本解析失败率过高，疑似格式变更"
        return Capability(self.name, ok, detail, parsed, failed)
