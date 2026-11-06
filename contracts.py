#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据契约层。

Core、存储、HTTP、UI 全部只认这里的类型；任何 Adapter 都必须产出这些类型。
WorkBuddy 的字段名不允许出现在本文件里 —— 这是分层的硬边界。
"""

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, Optional

SCHEMA_VERSION = 1


@dataclass
class UsageRecord:
    """一次模型请求的用量。请求级最小事实单元，不可再拆。"""

    record_id: str                 # 幂等键（优先用上游响应 id）
    ts: str                        # ISO8601，带时区
    model: str
    input_new: int                 # 未命中缓存的输入
    input_cached: int              # 命中缓存的输入
    output: int                    # 输出总量（已含 reasoning）
    output_reasoning: int          # 输出中的思考部分，聚合时禁止二次相加
    total: int
    source: str = "trace"
    session_ref: Optional[str] = None
    raw_ref: Optional[str] = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SessionSnapshot:
    """会话级快照。不是累加量，是某一时刻的状态，每次刷新整体替换。"""

    session_ref: str
    title: str
    model: str
    ctx_used: int                  # 当前上下文占用 token
    ctx_size: int                  # 上下文窗口大小
    rounds: int                    # 已发生的轮次
    credit: float                  # 计费积分
    last_active: str               # ISO8601
    source: str = "sqlite"
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Capability:
    """Adapter 自检结果。ok=False 时 Core 进入降级，但继续提供最后有效数据。"""

    name: str
    ok: bool
    detail: str = ""
    parsed: int = 0
    failed: int = 0
    schema_version: int = SCHEMA_VERSION

    @property
    def fail_ratio(self) -> float:
        total = self.parsed + self.failed
        return (self.failed / total) if total else 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["fail_ratio"] = round(self.fail_ratio, 4)
        return d


@dataclass
class HealthState:
    """整体健康状态，直接喂给界面的黄条。"""

    status: str = "OK"             # OK | DEGRADED
    since: Optional[str] = None
    reasons: list = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
