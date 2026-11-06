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
import logging
import os
from typing import List, Optional

import config
from adapters.base import Adapter
from contracts import Capability, UsageRecord
from core.store import Store

log = logging.getLogger(__name__)

# 连续漂移多少轮后放弃回退水位（回退是为了让「改对映射」能补回历史，但无限回退等于原地空转）
DRIFT_HOLD_MAX = 3


class TraceAdapter(Adapter):
    name = "trace"

    def __init__(self):
        # 字段映射漂移的现场标记：给引擎看，让它立刻把状态推到 DEGRADED，而不是等 5 分钟自检
        self.drift_flag = False
        self._drift_streak = 0

    # ---------- 定位 ----------

    def files(self) -> List[str]:
        found = set()
        for pat in config.TRACE_PATTERNS:
            found.update(glob.glob(os.path.join(config.TRACE_GLOB_DIR, pat)))
        return sorted(found)

    # ---------- 字段映射 ----------

    @staticmethod
    def _has_positive(node) -> bool:
        """usage 子树里是否有任何一个正数，用来区分「真的没有用量」和「字段名对不上」。"""
        if isinstance(node, dict):
            return any(TraceAdapter._has_positive(v) for v in node.values())
        if isinstance(node, bool):
            return False
        if isinstance(node, (int, float)):
            return node > 0
        return False

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

    def parse_line(self, raw: str, path: str, pos: int, stats: Optional[dict] = None) -> List[UsageRecord]:
        """返回该行贡献的记录；非目标行返回空列表；坏行抛异常由调用方计数。

        stats 用来把「字段映射失效」这件事带出去（不改返回值，保持惰性兼容）。
        """
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
            usage = resp.get("usage")
            if not isinstance(usage, dict) or not usage:
                continue

            v = self._map_usage(resp)
            # 判「字段漂移」不能只看全为 0：上游常常只改其中一个名字，
            # 剩下 total/output 还能读到，于是输入被静默记成 0，比全 0 更隐蔽。
            # 这里用一条守恒关系兜底：total ≈ input_total + output，偏太多就是映射断了。
            mapped = v["input_total"] + v["output"]
            off = bool(v["total"]) and abs(mapped - v["total"]) > max(16, v["total"] * 0.02)
            blank = not any(v.values()) and self._has_positive(usage)
            if blank or off:
                if stats is not None:
                    stats["drift"] = stats.get("drift", 0) + 1
                continue
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
        drift_round = 0
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
            stats = {"drift": 0}
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
                            batch.extend(self.parse_line(text, path, pos, stats))
                        except Exception:
                            continue              # 坏行：跳过，不影响其他行
            except OSError:
                continue

            if batch:
                added += store.insert_records(batch)

            if stats["drift"]:
                drift_round += stats["drift"]
                # 水位原地不动，把这段留给「映射改对之后」重读。
                # 连续多轮都漂移说明不是临时问题，再回退就只是白扫，索性推进水位并靠 DEGRADED 报出来。
                if self._drift_streak < DRIFT_HOLD_MAX:
                    store.set_watermark(path, st.st_size, st.st_mtime, offset)
                    continue
                log.error("连续 %d 轮字段漂移，停止回退水位：%s", self._drift_streak + 1, path)
            store.set_watermark(path, st.st_size, st.st_mtime, new_offset)

        self._drift_streak = self._drift_streak + 1 if drift_round else 0
        self.drift_flag = drift_round > 0
        if drift_round:
            log.error(
                "trace 字段映射疑似失效：本轮 %d 条响应带 usage 但五个字段全映射为 0，"
                "已跳过不入库（去改 config.USAGE_MAP，不要改代码）", drift_round
            )
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
                st = {"drift": 0}
                try:
                    self.parse_line(line, path, 0, st)
                    # 字段漂移不能算「解析成功」，否则自检永远亮绿灯，而库里全是 0
                    if st["drift"]:
                        failed += 1
                    else:
                        parsed += 1
                except Exception:
                    failed += 1

        ok = parsed > 0 and (failed / max(parsed + failed, 1)) <= config.DEGRADED_FAIL_RATIO
        detail = f"抽样 {len(recent)} 个文件" if ok else "样本解析失败率过高，疑似格式变更（或 USAGE_MAP 失效）"
        return Capability(self.name, ok, detail, parsed, failed)
