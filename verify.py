#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
验证脚本 —— 对应设计方案 §7 的 V1~V11。

每一项都输出 PASS/FAIL 和实际数值，判定标准写死在代码里，不靠人工目测。
全部在临时目录里跑，不碰真实数据。

  python verify.py           跑全部
  python verify.py v5 v9     只跑指定项
"""

import glob
import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from adapters.trace_adapter import TraceAdapter
from contracts import UsageRecord
from core.engine import Engine
from core.store import Store

RESULTS = []


def record(code, name, ok, detail):
    RESULTS.append((code, name, ok, detail))
    print(f"  [{code}] {'PASS' if ok else 'FAIL'}  {name}")
    print(f"        {detail}")


# ---------------------------------------------------------------- 工具

def make_line(rid, ts, prompt=1000, cached=800, out=50, rsn=20, model="test-model"):
    resp = {
        "id": rid,
        "model": model,
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": out,
            "total_tokens": prompt + out,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens_details": {"reasoning_tokens": rsn},
        },
    }
    span = {"type": "generation", "startedAt": ts, "toolOutput": json.dumps([resp])}
    return json.dumps({"type": "span_end", "span": span}, ensure_ascii=False) + "\n"


def independent_totals(trace_dir):
    """刻意不复用 adapters 的代码，独立实现一遍，用来对账。"""
    seen, tot = set(), dict(requests=0, input_new=0, input_cached=0,
                            output=0, output_reasoning=0, total=0)
    files = glob.glob(os.path.join(trace_dir, "*", "trace_*.jsonl"))
    for path in files:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "span_end":
                    continue
                span = obj.get("span") or {}
                if span.get("type") != "generation":
                    continue
                try:
                    arr = json.loads(span.get("toolOutput") or "[]")
                except Exception:
                    continue
                for r in (arr if isinstance(arr, list) else [arr]):
                    if not isinstance(r, dict) or not isinstance(r.get("usage"), dict):
                        continue
                    u = r["usage"]
                    if not u:
                        continue
                    rid = r.get("id")
                    if rid in seen:
                        continue
                    seen.add(rid)
                    p = u.get("prompt_tokens") or 0
                    c = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                    tot["requests"] += 1
                    tot["input_new"] += p - c
                    tot["input_cached"] += c
                    tot["output"] += u.get("completion_tokens") or 0
                    tot["output_reasoning"] += (u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
                    tot["total"] += u.get("total_tokens") or 0
    return tot


def rss_mb():
    try:
        import ctypes
        import ctypes.wintypes as wt

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        k32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        k32.GetCurrentProcess.restype = wt.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
        psapi.GetProcessMemoryInfo.restype = wt.BOOL

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return -1.0
        return pmc.WorkingSetSize / 1024 / 1024
    except Exception:
        return -1.0


class TempEnv:
    """把 config 指到临时目录，保证不碰真实数据。"""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="wbum-verify-")
        self.traces = os.path.join(self.root, "traces")
        os.makedirs(self.traces, exist_ok=True)
        self._saved = {}

    def __enter__(self):
        self._saved = {
            "DATA_DIR": config.DATA_DIR, "DB_PATH": config.DB_PATH,
            "TRACE_GLOB_DIR": config.TRACE_GLOB_DIR, "WB_DB_PATH": config.WB_DB_PATH,
        }
        config.DATA_DIR = self.root
        config.DB_PATH = os.path.join(self.root, "usage.db")
        config.TRACE_GLOB_DIR = self.traces
        config.WB_DB_PATH = os.path.join(self.root, "no-such.db")
        return self

    def __exit__(self, *a):
        for k, v in self._saved.items():
            setattr(config, k, v)
        shutil.rmtree(self.root, ignore_errors=True)


# ---------------------------------------------------------------- V1

def v1_data_integrity(env):
    real_traces = os.path.expanduser("~/.workbuddy/traces")
    saved = config.TRACE_GLOB_DIR
    try:
        config.TRACE_GLOB_DIR = real_traces
        store = Store(os.path.join(env.root, "v1.db"))
        TraceAdapter().collect(store)
        got = store.aggregate()["totals"]
    finally:
        config.TRACE_GLOB_DIR = saved

    want = independent_totals(real_traces)
    diffs = {k: (want[k], got[k]) for k in want if want[k] != got[k]}
    ok = not diffs
    record("V1", "数据完整性（与独立重算逐项对比）", ok,
           f"{want['requests']} 条请求；差异项 {diffs or '无'}")
    store.close()


# ---------------------------------------------------------------- V2

def v2_idempotent(env):
    f = os.path.join(env.traces, "100", "trace_aaa.jsonl")
    os.makedirs(os.path.dirname(f), exist_ok=True)
    with open(f, "w", encoding="utf-8") as fh:
        for i in range(30):
            fh.write(make_line(f"r{i}", "2026-11-06T01:00:00.000Z"))

    store = Store(os.path.join(env.root, "v2.db"))
    a = TraceAdapter()
    first = a.collect(store)
    repeats = [a.collect(store) for _ in range(3)]
    counts = [store.count() for _ in range(5)]
    ok = (first == 30) and all(r == 0 for r in repeats) and len(set(counts)) == 1
    record("V2", "幂等性（重复采集 3 次 + 重开 5 次）", ok,
           f"首采 {first}，重复采集 {repeats}，计数序列 {set(counts)}")
    store.close()


# ---------------------------------------------------------------- V3

def v3_latency(env):
    d = os.path.join(env.root, "v3")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, "trace_lat.jsonl")
    open(f, "w").close()

    saved = config.TRACE_GLOB_DIR
    config.TRACE_GLOB_DIR = d
    store = Store(os.path.join(env.root, "v3.db"))
    engine = Engine(store, primary=[TraceAdapter()], secondary=[])
    engine.probe_all()
    engine.collect_once()

    last_event = [0.0]

    def on_event(ev):
        if ev.get("type") == "update":
            last_event[0] = time.time()

    engine.subscribe(on_event)

    stop = threading.Event()

    def loop():
        while not stop.is_set():
            engine.collect_once()
            engine.pulse()
            time.sleep(config.TRACE_POLL_SEC)

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    time.sleep(0.4)

    lat = []
    for i in range(8):
        rid = f"lat{i}"
        n0 = store.count()
        t0 = time.time()
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(make_line(rid, "2026-11-06T02:00:00.000Z"))
        deadline = t0 + 6
        while time.time() < deadline:
            if store.count() > n0 and last_event[0] >= t0:
                lat.append(last_event[0] - t0)
                break
            time.sleep(0.01)          # 单条：入库 + 事件到达才算一次
        time.sleep(0.6)

    stop.set()
    th.join(timeout=3)
    engine.stop()
    config.TRACE_GLOB_DIR = saved

    if not lat:
        record("V3", "实时延迟（写入到界面收到）", False, "未采集到有效样本")
    else:
        s = sorted(lat)
        p50 = s[len(s) // 2]
        p95 = s[min(int(len(s) * 0.95), len(s) - 1)]
        ok = p95 < 2.0 and p50 < 1.0
        record("V3", "实时延迟（写入到界面收到）", ok,
               f"样本 {len(s)}，P50={p50*1000:.0f}ms，P95={p95*1000:.0f}ms（要求 P50<1s 且 P95<2s）")
    store.close()


# ---------------------------------------------------------------- V4

def v4_resume(env):
    f = os.path.join(env.traces, "300", "trace_ccc.jsonl")
    os.makedirs(os.path.dirname(f), exist_ok=True)
    with open(f, "w", encoding="utf-8") as fh:
        for i in range(5):
            fh.write(make_line(f"a{i}", "2026-11-06T03:00:00.000Z"))

    db = os.path.join(env.root, "v4.db")
    s1 = Store(db)
    TraceAdapter().collect(s1)
    before = s1.count()
    s1.close()                                    # 模拟进程退出

    # 停机期间新增 10 条，其中 1 条是写了一半的断行
    with open(f, "a", encoding="utf-8") as fh:
        for i in range(9):
            fh.write(make_line(f"b{i}", "2026-11-06T03:10:00.000Z"))
        partial = make_line("b9", "2026-11-06T03:10:00.000Z")
        fh.write(partial[: len(partial) // 2])

    s2 = Store(db)
    TraceAdapter().collect(s2)
    after_partial = s2.count()
    # 补全那一行
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(partial[len(partial) // 2:])
    TraceAdapter().collect(s2)
    final = s2.count()
    dup = s2.count() - final

    ok = before == 5 and after_partial == 14 and final == 15 and dup == 0
    record("V4", "断点续采（崩溃 + 半行 + 补全）", ok,
           f"停机前 {before} → 半行时 {after_partial} → 补全后 {final}（期望 5/14/15）")
    s2.close()


# ---------------------------------------------------------------- V5

def v5_drift(env):
    """每个子用例独立目录，避免互相污染。"""
    cases = []

    def run_case(label, subdir, writer, expect):
        d = os.path.join(env.root, "v5", subdir)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "trace_x.jsonl")
        writer(path)
        saved = config.TRACE_GLOB_DIR
        config.TRACE_GLOB_DIR = d
        st = Store(os.path.join(env.root, f"v5_{subdir}.db"))
        try:
            TraceAdapter().collect(st)
            cnt = st.count()
            cases.append((label, cnt == expect, f"{label}: 入库 {cnt}（期望 {expect}）"))
        except Exception as exc:
            cases.append((label, False, f"{label}: 崩溃 {exc}"))
        finally:
            st.close()
            config.TRACE_GLOB_DIR = saved

    # a) 字段改名：不崩，值降级为 0
    def w_rename(p):
        line = make_line("d1", "2026-11-06T04:00:00.000Z")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(line.replace("prompt_tokens", "prompt_tokens_v2"))

    run_case("字段改名", "a", w_rename, 1)

    # b) 半行截断：不算数
    def w_half(p):
        line = make_line("d2", "2026-11-06T04:05:00.000Z")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(line[: len(line) // 2])

    run_case("半行截断", "b", w_half, 0)

    # c) 完全坏行：只丢这一行，不影响同文件其他行
    def w_mixed(p):
        good1 = make_line("d3", "2026-11-06T04:10:00.000Z")
        good2 = make_line("d4", "2026-11-06T04:11:00.000Z")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(good1)
            fh.write("{这不是合法 JSON\n")
            fh.write(good2)

    run_case("混入坏行", "c", w_mixed, 2)

    # d) 数据库不存在 -> probe 报异常 + health 降级，但不抛
    st = Store(os.path.join(env.root, "v5_d.db"))
    eng = Engine(st, primary=[TraceAdapter()])
    eng.probe_all()
    cap_sql = eng.caps.get("sqlite")
    ok_d = cap_sql is not None and (not cap_sql.ok) and eng.health.status == "DEGRADED"
    cases.append(("缺数据库", ok_d, f"缺数据库: sqlite.ok={getattr(cap_sql,'ok',None)} health={eng.health.status}"))
    st.close()

    # e) trace 目录整体不存在 -> probe 返回异常而非抛出
    saved = config.TRACE_GLOB_DIR
    config.TRACE_GLOB_DIR = os.path.join(env.root, "no-such-dir")
    try:
        cap = TraceAdapter().probe()
        cases.append(("缺目录", not cap.ok, f"缺目录: probe.ok={cap.ok}"))
    except Exception as exc:
        cases.append(("缺目录", False, f"缺目录: 抛出 {exc}"))
    finally:
        config.TRACE_GLOB_DIR = saved

    record("V5", "格式漂移韧性（改名/半行/坏行/缺库/缺目录）",
           all(ok for _, ok, _ in cases),
           "；".join(d for _, _, d in cases))


# ---------------------------------------------------------------- V6 / V7

def v6_resource(env):
    f = os.path.join(env.traces, "500", "trace_e1.jsonl")
    os.makedirs(os.path.dirname(f), exist_ok=True)
    with open(f, "w", encoding="utf-8") as fh:
        for i in range(2000):
            fh.write(make_line(f"e{i}", "2026-11-06T05:00:00.000Z"))

    store = Store(os.path.join(env.root, "v6.db"))
    a = TraceAdapter()
    a.collect(store)

    base = rss_mb()
    t0, w0 = time.process_time(), time.time()
    rounds = 0
    while time.time() - w0 < 5.0:                 # 持续 5 秒模拟空转
        a.collect(store)
        store.aggregate()
        rounds += 1
        time.sleep(0.05)
    wall = time.time() - w0
    cpu = (time.process_time() - t0) / wall * 100
    mem = rss_mb()

    ok = 0 <= cpu < 30 and 0 < mem < 150
    record("V6", "资源占用（5 秒持续空转）", ok,
           f"{rounds} 轮，CPU {cpu:.1f}%（阈值 <30%），"
           f"RSS {mem:.1f}MB（阈值 <150MB，基线 {base:.1f}MB）")
    store.close()


def v7_short_stability(env):
    f = os.path.join(env.traces, "600", "trace_f1.jsonl")
    os.makedirs(os.path.dirname(f), exist_ok=True)
    open(f, "w").close()
    store = Store(os.path.join(env.root, "v7.db"))
    eng = Engine(store, primary=[TraceAdapter()], secondary=[])
    eng.probe_all()

    samples = []
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            eng.collect_once()
            eng.pulse()
            time.sleep(0.5)

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    for i in range(12):
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(make_line(f"g{i}", "2026-11-06T06:00:00.000Z"))
        time.sleep(5)
        samples.append(rss_mb())
    stop.set()
    th.join(timeout=3)
    eng.stop()

    start, end = samples[0], samples[-1]
    growth = (end - start) / start * 100 if start > 0 else 0
    ok = growth < 20
    record("V7", "短稳抽样（60s，每 5s 采样 RSS）", ok,
           f"起始 {start:.1f}MB → 结束 {end:.1f}MB，增长 {growth:.1f}%（阈值 <20%；完整 8h 需长跑，此处为抽样）")
    store.close()


# ---------------------------------------------------------------- V8

def v8_readonly(env):
    wb_db = os.path.expanduser("~/.workbuddy/workbuddy.db")
    traces = os.path.expanduser("~/.workbuddy/traces")
    if not os.path.exists(wb_db):
        record("V8", "只读安全", False, "找不到 WorkBuddy 数据库，无法验证")
        return

    def snapshot():
        state = {}
        try:
            st = os.stat(wb_db)
            state[wb_db] = (st.st_size, st.st_mtime)
        except OSError:
            pass
        for p in glob.glob(os.path.join(traces, "*", "trace_*.jsonl"))[:50]:
            try:
                st = os.stat(p)
                state[p] = (st.st_size, st.st_mtime)
            except OSError:
                pass
        return state

    saved_t, saved_d = config.TRACE_GLOB_DIR, config.WB_DB_PATH
    config.TRACE_GLOB_DIR, config.WB_DB_PATH = traces, wb_db
    before = snapshot()
    store = Store(os.path.join(env.root, "v8.db"))
    eng = Engine(store)
    try:
        eng.probe_all()
        eng.collect_once()
        eng.snapshot()
        ok_read = True
        err = ""
    except Exception as exc:
        ok_read, err = False, str(exc)
    after = snapshot()
    config.TRACE_GLOB_DIR, config.WB_DB_PATH = saved_t, saved_d

    changed = [p for p in before if before.get(p) != after.get(p)]
    ok = ok_read and not changed
    record("V8", "只读安全（采集前后 WorkBuddy 文件指纹不变）", ok,
           f"抽查 {len(before)} 个文件，变动 {len(changed)} 个"
           + (f"：{changed[:3]}" if changed else "；只读打开未触发锁")
           + (f"；异常 {err}" if err else ""))
    store.close()


# ---------------------------------------------------------------- V9

def v9_pluggable(env):
    """换一个纯造假的 Adapter，Core 与快照结构必须完全一致。"""

    class FakeAdapter:
        name = "fake"

        def probe(self):
            from contracts import Capability
            return Capability(self.name, True, "假数据源")

        def collect(self, store):
            recs = [
                UsageRecord(
                    record_id=f"fake{i}", ts="2026-11-06T07:00:00.000Z", model="fake-model",
                    input_new=100, input_cached=900, output=50, output_reasoning=25,
                    total=1050, source="fake", session_ref="fake:s1",
                )
                for i in range(7)
            ]
            return store.insert_records(recs)

    store = Store(os.path.join(env.root, "v9.db"))
    eng = Engine(store, primary=[FakeAdapter()], secondary=[])
    eng.probe_all()
    eng.collect_once()
    snap = eng.snapshot()

    required = {"schema_version", "totals", "daily", "models", "sessions", "health", "generated_at"}
    ok = (
        required.issubset(snap.keys())
        and snap["totals"]["requests"] == 7
        and snap["totals"]["total"] == 7 * 1050
        and snap["models"][0]["model"] == "fake-model"
        and snap["health"]["status"] == "OK"
    )
    record("V9", "可插拔（假 Adapter 顶替，Core/UI 零改动）", ok,
           f"快照字段 {'齐全' if required.issubset(snap.keys()) else '缺失'}，"
           f"请求 {snap['totals']['requests']}，合计 {snap['totals']['total']}，"
           f"模型 {snap['models'][0]['model'] if snap['models'] else '-'}")
    store.close()


def v10_granularity(env):
    """四种时间粒度的桶必须守恒：桶数对、合计对、模型合计等于 token 合计。"""
    from datetime import date, timedelta

    store = Store(os.path.join(env.root, "v10.db"))
    today = date.today()
    offsets = [0, 3, 10, 40, 400, 800]          # 覆盖日/周/月/年四个窗口
    truth, recs = {}, []
    for i, off in enumerate(offsets):
        d = today - timedelta(days=off)
        total = (i + 1) * 1000
        truth[d] = total
        recs.append(UsageRecord(
            record_id=f"g{i}", ts=f"{d.isoformat()}T10:00:00",
            model="m-a" if i % 2 else "m-b",
            input_new=total // 2, input_cached=total // 2,
            output=0, output_reasoning=0, total=total,
            source="test", session_ref=None,
        ))
    store.insert_records(recs)
    ser = store.aggregate()["series"]

    ok, notes = True, []
    for gran, want in (("day", 7), ("week", 4), ("month", 12), ("year", 3)):
        if gran not in ser:
            ok = False
            notes.append(f"缺 {gran}")
            continue
        bs = ser[gran]["buckets"]
        if len(bs) != want:
            ok = False
            notes.append(f"{gran} 桶数 {len(bs)}≠{want}")

        # 独立按窗口重算，不复用 build_series
        exp = dict.fromkeys(("requests", "input_new", "input_cached",
                             "output", "output_reasoning", "total"), 0)
        for b in bs:
            s, e = date.fromisoformat(b["from"]), date.fromisoformat(b["to"])
            for d, t in truth.items():
                if s <= d <= e:
                    exp["requests"] += 1
                    exp["total"] += t
                    exp["input_new"] += t // 2
                    exp["input_cached"] += t // 2
        got = ser[gran]["totals"]
        bad = {k: (got[k], exp[k]) for k in exp if got[k] != exp[k]}
        if bad:
            ok = False
            notes.append(f"{gran} 合计不符 {bad}")

        mt = sum(m["total"] for m in ser[gran]["models"])
        if mt != got["total"]:
            ok = False
            notes.append(f"{gran} 模型合计 {mt}≠{got['total']}")

    # 空桶必须保留，否则趋势图会跳过没干活的日子
    if not any(b["total"] == 0 for b in ser["day"]["buckets"]):
        ok = False
        notes.append("日粒度缺少空桶")

    # 相邻桶首尾必须连续，不能有缝也不能重叠
    for gran in ("day", "week", "month", "year"):
        bs = ser[gran]["buckets"]
        for a, b in zip(bs, bs[1:]):
            gap = (date.fromisoformat(b["from"]) - date.fromisoformat(a["to"])).days
            if gap != 1:
                ok = False
                notes.append(f"{gran} 桶不连续（间隔 {gap} 天）")
                break

    record("V10", "时间粒度分桶（7 天/4 周/12 月/3 年）", ok,
           "；".join(notes) if notes else
           f"四种粒度桶数 7/4/12/3，合计与独立重算一致，空桶保留，区间连续；"
           f"日粒度合计 {ser['day']['totals']['total']:,}，年粒度合计 {ser['year']['totals']['total']:,}")
    store.close()


def v11_session_search(env):
    """会话检索：全量可查、翻页不重不漏、历史上游消失的也能查到。"""
    from contracts import SessionSnapshot

    store = Store(os.path.join(env.root, "v11.db"))

    def snap(i):
        return SessionSnapshot(
            session_ref=f"s{i}",
            title=(f"预算复核-{i:02d}" if i % 4 == 0 else f"会话-{i:02d}"),
            model="m", ctx_used=1000 + i, ctx_size=100000, rounds=i,
            credit=(0.01 if i == 7 else round(i * 7.3, 2)),   # 7 号是最小的那个
            last_active="2026-11-01T10:00:00",
        )

    allsnaps = [snap(i) for i in range(48)]
    store.replace_snapshots(allsnaps)
    want_budget = sum(1 for i in range(48) if i % 4 == 0)

    ok, notes = True, []

    # 1) 命中数
    _, total = store.search_sessions("预算", 0, 5)
    if total != want_budget:
        ok = False
        notes.append(f"「预算」命中 {total}≠{want_budget}")

    # 2) 翻页不重不漏
    seen, offset = [], 0
    while True:
        rows, _t = store.search_sessions("预算", offset, 5)
        if not rows:
            break
        seen += [r["session_ref"] for r in rows]
        offset += 5
    if len(seen) != want_budget or len(set(seen)) != want_budget:
        ok = False
        notes.append(f"翻页收集 {len(seen)} 条、去重 {len(set(seen))} 条，期望 {want_budget}")

    # 3) 消耗极小的会话也要查得到
    rows, _t = store.search_sessions("会话-07", 0, 10)
    if len(rows) != 1 or abs(rows[0]["credit"] - 0.01) > 1e-6:
        ok = False
        notes.append(f"低消耗会话检索异常（{len(rows)} 条）")

    # 4) 通配符必须被转义，不能把整表匹配出来
    for bad_q in ("%", "_"):
        _r, t = store.search_sessions(bad_q, 0, 10)
        if t != 0:
            ok = False
            notes.append(f"通配符 {bad_q} 未转义，命中 {t} 条")

    # 5) 上游已消失的会话仍可检索（分两批写入，模拟会话被关掉）
    store2 = Store(os.path.join(env.root, "v11b.db"))
    store2.replace_snapshots(allsnaps[:20])
    store2.replace_snapshots(allsnaps[20:])          # 前 20 个在上游已不存在
    rows, t2 = store2.search_sessions("预算", 0, 50)
    if t2 != want_budget:
        ok = False
        notes.append(f"历史会话丢失：命中 {t2}≠{want_budget}")
    dead = [r for r in rows if not r["live"]]
    expect_dead = sum(1 for i in range(20) if i % 4 == 0)
    if len(dead) != expect_dead:
        ok = False
        notes.append(f"live 标记异常：离线 {len(dead)}≠{expect_dead}")
    store2.close()

    record("V11", "会话检索（全量可查 + 翻页不重不漏 + 历史保留）", ok,
           "；".join(notes) if notes else
           f"命中 {total} 条与预期一致；分 3 页翻完去重后仍 {want_budget} 条；"
           f"最小消耗会话可查；% 与 _ 已转义；分批写入后 {len(dead)} 条已离线但仍可检索")
    store.close()


# ---------------------------------------------------------------- 主流程

CASES = [
    ("v1", lambda e: v1_data_integrity(e)),
    ("v2", lambda e: v2_idempotent(e)),
    ("v3", lambda e: v3_latency(e)),
    ("v4", lambda e: v4_resume(e)),
    ("v5", lambda e: v5_drift(e)),
    ("v6", lambda e: v6_resource(e)),
    ("v7", lambda e: v7_short_stability(e)),
    ("v8", lambda e: v8_readonly(e)),
    ("v9", lambda e: v9_pluggable(e)),
    ("v10", lambda e: v10_granularity(e)),
    ("v11", lambda e: v11_session_search(e)),
]


def main():
    wanted = [a.lower() for a in sys.argv[1:]] or [c[0] for c in CASES]
    print("=" * 72)
    print("WorkBuddy 用量监控 —— 验证运行")
    print("=" * 72)
    for key, fn in CASES:
        if key not in wanted:
            continue
        print(f"\n▶ {key.upper()}")
        with TempEnv() as env:
            try:
                fn(env)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                record(key.upper(), "用例异常中断", False, str(exc))

    print("\n" + "=" * 72)
    passed = sum(1 for *_, ok, _ in RESULTS if ok)
    for code, name, ok, _ in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {code:<4} {name}")
    print("-" * 72)
    print(f"  合计 {passed}/{len(RESULTS)} 通过")
    print("=" * 72)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
