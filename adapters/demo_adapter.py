#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DemoAdapter —— 生成一份确定的假数据，用于演示和截图。

用途：
  1. `main.py --demo` 起一个不读任何真实数据的看板
  2. 给仓库做截图时，不会把真实的会话标题、项目名带出去

数据用固定种子生成，每次结果一致，方便截图对比。
"""

import random
from datetime import datetime, timedelta, timezone

from adapters.base import Adapter
from contracts import Capability, SessionSnapshot, UsageRecord
from core.store import Store

MODELS = [
    ("model-large", 0.72),
    ("model-fast", 0.24),
    ("model-reasoning", 0.04),
]

SESSIONS = [
    ("重构数据同步模块", "model-large", 46, 512_000, 1_000_000),
    ("排查接口超时问题", "model-large", 31, 268_000, 600_000),
    ("整理季度分析报告", "model-fast", 24, 154_000, 600_000),
    ("编写部署脚本", "model-fast", 18, 96_000, 300_000),
    ("梳理日志字段口径", "model-large", 15, 402_000, 1_000_000),
    ("对比两版算法效果", "model-reasoning", 12, 188_000, 600_000),
    ("准备技术分享材料", "model-fast", 9, 72_000, 300_000),
    ("清理历史遗留配置", "model-large", 7, 341_000, 1_000_000),
    ("设计接口鉴权流程", "model-large", 27, 296_000, 600_000),
    ("优化列表渲染性能", "model-large", 22, 455_000, 1_000_000),
    ("补写单元测试", "model-fast", 14, 88_000, 300_000),
    ("整理数据库迁移方案", "model-large", 19, 372_000, 1_000_000),
    ("校对发布说明", "model-fast", 6, 41_000, 300_000),
    ("分析慢查询日志", "model-reasoning", 11, 233_000, 600_000),
    ("搭建本地调试环境", "model-fast", 8, 57_000, 300_000),
    ("复现线上异常堆栈", "model-reasoning", 16, 268_000, 600_000),
    ("规划模块拆分方案", "model-large", 25, 431_000, 1_000_000),
    ("写数据字典文档", "model-fast", 5, 34_000, 300_000),
]


class DemoAdapter(Adapter):
    name = "demo"

    def __init__(self, days: int = 120, per_day: int = 70, seed: int = 42):
        self.days = days
        self.per_day = per_day
        self.seed = seed

    def probe(self) -> Capability:
        return Capability(self.name, True, "演示数据（不读取真实文件）", parsed=1)

    def collect(self, store: Store) -> int:
        rng = random.Random(self.seed)
        now = datetime.now(timezone.utc).astimezone()
        records = []

        weights = [w for _, w in MODELS]
        names = [m for m, _ in MODELS]

        for d in range(self.days - 1, -1, -1):
            day = now - timedelta(days=d)
            # 周末少一点，工作日多一点
            factor = 0.45 if day.weekday() >= 5 else 1.0
            n = max(20, int(self.per_day * factor * rng.uniform(0.75, 1.25)))

            for i in range(n):
                hour = rng.choices(
                    range(9, 23),
                    weights=[1, 1, 2, 3, 4, 5, 5, 4, 4, 5, 6, 6, 4, 2],
                )[0]
                ts = day.replace(hour=hour, minute=rng.randrange(60),
                                 second=rng.randrange(60), microsecond=0)
                model = rng.choices(names, weights=weights)[0]

                # 上下文随会话推进变长，所以输入远大于输出
                base = rng.randint(28_000, 92_000) + i * rng.randint(200, 900)
                cached = int(base * rng.uniform(0.93, 0.995))
                output = rng.randint(180, 1_400)
                reasoning = int(output * rng.uniform(0.35, 0.6)) if "reasoning" in model or rng.random() < 0.5 else 0

                records.append(
                    UsageRecord(
                        record_id=f"demo-{day:%Y%m%d}-{i}",
                        ts=ts.isoformat(),
                        model=model,
                        input_new=base - cached,
                        input_cached=cached,
                        output=output,
                        output_reasoning=reasoning,
                        total=base + output,
                        source="demo",
                        session_ref="demo",
                    )
                )

        inserted = store.insert_records(records)

        snaps = []
        for name, model, rounds, used, size in SESSIONS:
            snaps.append(
                SessionSnapshot(
                    session_ref=f"demo:{name}",
                    title=name,
                    model=model,
                    ctx_used=used,
                    ctx_size=size,
                    rounds=rounds,
                    credit=round(rounds * rng.uniform(4, 26), 2),
                    last_active=(now - timedelta(hours=rng.randint(1, 72))).isoformat(),
                )
            )
        store.replace_snapshots(snaps)
        return inserted
