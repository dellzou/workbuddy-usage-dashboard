#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Adapter 基类。

约定：
  - collect(store) 负责把原始数据变成 contracts 里的类型并落库，返回新增条数
  - probe() 只做只读体检，不产生副作用，不许抛异常
  - 任何 Adapter 都不得向上抛出未处理异常，降级由 Capability 表达
"""

from core.store import Store
from contracts import Capability


class Adapter:
    name = "base"

    def probe(self) -> Capability:
        raise NotImplementedError

    def collect(self, store: Store) -> int:
        raise NotImplementedError
