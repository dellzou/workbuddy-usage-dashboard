#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
所有可调常量集中在这里。

设计原则：这是配置层，不含任何解析逻辑。
修改路径 / 端口 / 轮询间隔 / 字段映射，只动本文件或同目录的 config.json，
不需要碰 Adapter、Core、UI。
"""

import json
import os
import sys

# PyInstaller 打包后 __file__ 指向临时解压目录，静态资源要从 sys._MEIPASS 取
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

# ---- 运行时数据目录（与 WorkBuddy 目录物理隔离，互不影响）----
DATA_DIR = os.path.normpath(os.path.expanduser(os.environ.get("WBUM_DATA_DIR", "~/.workbuddy-usage")))
DB_PATH = os.path.join(DATA_DIR, "usage.db")
LOG_DIR = os.path.join(DATA_DIR, "logs")

# ---- 服务 ----
HOST = "127.0.0.1"
PORT = int(os.environ.get("WBUM_PORT", "17787"))
SSE_HEARTBEAT_SEC = 15

# ---- 采集节奏 ----
TRACE_POLL_SEC = 1.0        # trace 增量扫描间隔
SQLITE_POLL_SEC = 5.0       # 会话快照刷新间隔
PROBE_INTERVAL_SEC = 300    # 能力自检间隔
DEGRADED_FAIL_RATIO = 0.10  # 解析失败行占比超过此值即降级

# ---- WorkBuddy 数据源位置（升级若变目录，改这里）----
WB_ROOT = os.path.normpath(os.path.expanduser(os.environ.get("WBUM_WB_ROOT", "~/.workbuddy")))
TRACE_GLOB_DIR = os.path.join(WB_ROOT, "traces")
TRACE_PATTERNS = ("*/trace_*.jsonl", "trace_*.jsonl")
WB_DB_PATH = os.path.join(WB_ROOT, "workbuddy.db")

# ---- 字段映射（升级若只改字段名，改这里，不动代码）----
# 路径一律相对 usage 对象；写多个候选，按顺序取第一个存在的。
USAGE_MAP = {
    "input_total": ["prompt_tokens", "input_tokens"],
    "input_cached": [
        "prompt_tokens_details.cached_tokens",
        "prompt_tokens_details.cache_read_input_tokens",
        "cache_read_input_tokens",
    ],
    "output": ["completion_tokens", "output_tokens"],
    "output_reasoning": [
        "completion_tokens_details.reasoning_tokens",
        "completion_tokens_details.thinking_tokens",
        "output_tokens_details.thinking_tokens",
    ],
    "total": ["total_tokens"],
}

# 会话快照的列名候选（WorkBuddy 改列名时在这里加）
SQLITE_COLUMNS = {
    "table_usage": ["session_usage"],
    "table_sessions": ["sessions"],
    "usage_session_id": ["session_id"],
    "usage_used": ["used"],
    "usage_size": ["size"],
    "usage_credit": ["credit_json"],
    "session_id": ["id"],
    "session_title": ["custom_title", "title"],
    "session_model": ["model"],
    "session_last": ["last_activity_at", "updated_at"],
}


def user_config_path():
    return os.path.join(DATA_DIR, "config.json")


def load_overrides():
    """允许在数据目录放 config.json 覆盖上面的默认值，改配置无需改代码。"""
    path = user_config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def ensure_dirs():
    for d in (DATA_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)


def apply_overrides():
    """把 config.json 里的值真正生效。只接受本模块已有的全大写常量。"""
    ov = load_overrides()
    if not ov:
        return
    g = globals()
    for key, val in ov.items():
        if key.isupper() and key in g:
            if key == "DATA_DIR" and isinstance(val, str):
                val = os.path.expanduser(val)
            g[key] = val
    # DATA_DIR 变了，派生路径要跟着重算
    if "DATA_DIR" in ov:
        g["DB_PATH"] = os.path.join(g["DATA_DIR"], "usage.db")
        g["LOG_DIR"] = os.path.join(g["DATA_DIR"], "logs")
    # WB_ROOT 变了同理：TRACE_GLOB_DIR / WB_DB_PATH 是在模块顶部算死的，
    # 不重算的话 config.json 里改 WB_ROOT 完全没效果（env 变量覆盖才会生效）
    if "WB_ROOT" in ov:
        g["WB_ROOT"] = os.path.normpath(os.path.expanduser(g["WB_ROOT"]))
        g["TRACE_GLOB_DIR"] = os.path.join(g["WB_ROOT"], "traces")
        g["WB_DB_PATH"] = os.path.join(g["WB_ROOT"], "workbuddy.db")


apply_overrides()
