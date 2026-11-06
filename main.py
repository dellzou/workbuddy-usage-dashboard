#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
WorkBuddy 实时用量监控 —— 入口。

双击 exe（或 python main.py）即启动，自动打开浏览器。
关掉控制台窗口 = 停止服务。

  main.exe                 启动看板
  main.exe --probe         只做数据源自检
  main.exe --once          只采一轮并打印
  main.exe --port 9000     指定端口（被占会自动往后找）
  main.exe --no-browser    不自动开浏览器
"""

import argparse
import logging
import os
import socket
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台默认 GBK，中文会乱码；统一成 UTF-8（Win10+ 均支持）
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import config                       # noqa: E402
from core.engine import Engine      # noqa: E402
from core.store import Store        # noqa: E402

APP_NAME = "WorkBuddy 用量看板"


def setup_logging(verbose: bool = False):
    config.ensure_dirs()
    fmt = "%(asctime)s %(levelname)-7s %(name)-14s %(message)s"
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(os.path.join(config.LOG_DIR, "wbum.log"), encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=fmt, datefmt="%H:%M:%S", handlers=handlers,
    )


def say(text: str = ""):
    """所有面向用户的输出都走这里，强制 flush，避免被缓冲吞掉。"""
    print(text, flush=True)


def _is_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    """端口上是不是有人在监听（连得上就是有）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _is_our_service(port: int, host: str = "127.0.0.1") -> bool:
    """监听者是不是本程序（靠 /api/health 的特征字段判断）。"""
    try:
        import json as _json
        import urllib.request
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=0.8) as r:
            obj = _json.loads(r.read().decode("utf-8"))
        return isinstance(obj, dict) and "health" in obj and "capabilities" in obj
    except Exception:
        return False


def choose_port(preferred: int, host: str = "127.0.0.1", tries: int = 15):
    """挑端口，并判断该复用一个已有实例还是新起一个。

    返回 (port, action)，action 为 "attach"（已有实例）或 "bind"（自己起）。

    这样处理三种情况：
      1. 端口空闲                  -> bind
      2. 端口被本程序占着          -> attach，直接开浏览器（双击两次不会开两个服务）
      3. 端口被别的程序占着        -> 跳到下一个（微信等常占 8787）
    """
    for offset in range(tries):
        port = preferred + offset
        if _is_listening(port, host):
            if _is_our_service(port, host):
                return port, "attach"
            continue
        return port, "bind"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1], "bind"


def open_browser(url: str):
    def _open():
        time.sleep(0.6)
        try:
            import webbrowser
            if not webbrowser.open(url):
                say(f"  （没能自动打开浏览器，请手动访问 {url}）")
        except Exception:
            say(f"  （没能自动打开浏览器，请手动访问 {url}）")
    threading.Thread(target=_open, daemon=True).start()


def banner(url: str, engine: Engine, port_changed: bool, preferred: int):
    line = "=" * 60
    say()
    say(line)
    say(f"  {APP_NAME}")
    say(line)
    say(f"  地址      {url}")
    if port_changed:
        say(f"            （{preferred} 被占用，已自动换到这个端口）")
    say(f"  数据目录  {os.path.normpath(config.DATA_DIR)}")
    for name, cap in engine.caps.items():
        say(f"  数据源    {name:<8} {'正常' if cap.ok else '异常'}  {cap.detail}")
    say(line)
    if engine.health.status != "OK":
        say("  注意：有数据源不可用，看板会显示最后一次有效数据并给出黄条提示。")
    say("  浏览器会自动打开；关掉这个窗口即停止服务。")
    say(line)
    say()


def cmd_probe(engine: Engine) -> int:
    engine.probe_all()
    say(f"{'数据源':<10}{'状态':<8}详情")
    say("-" * 60)
    for name, cap in engine.caps.items():
        say(f"{name:<10}{'正常' if cap.ok else '异常':<8}{cap.detail}")
    say()
    say(f"整体: {engine.health.status}")
    return 0 if engine.health.status == "OK" else 1


def cmd_once(engine: Engine) -> int:
    stats = engine.collect_once()
    snap = engine.snapshot()
    t = snap["totals"]
    say("采集结果: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    say("-" * 60)
    say(f"请求数   {t['requests']:,}")
    say(f"新增输入 {t['input_new']:,}")
    say(f"命中缓存 {t['input_cached']:,}")
    say(f"输出     {t['output']:,}（思考 {t['output_reasoning']:,}）")
    say(f"合计     {t['total']:,}")
    say(f"范围     {snap['range']['from']} → {snap['range']['to']}")
    say(f"健康     {snap['health']['status']}")
    return 0


def cmd_serve(engine: Engine, open_browser_flag: bool = True) -> int:
    import server

    preferred = config.PORT
    port, action = choose_port(preferred)

    if action == "attach":
        url = f"http://{config.HOST}:{port}/"
        say()
        say("=" * 60)
        say(f"  已经在运行了：{url}")
        say("=" * 60)
        say("  已直接打开浏览器。要重新启动，请先关掉之前的窗口。")
        say()
        if open_browser_flag:
            open_browser(url)
        try:
            time.sleep(1.5)
        except KeyboardInterrupt:
            pass
        return 0

    config.PORT = port
    port_changed = port != preferred

    engine.start()
    httpd = server.serve(engine)
    url = f"http://{config.HOST}:{port}/"

    banner(url, engine, port_changed, preferred)

    if open_browser_flag:
        open_browser(url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        say("\n正在停止…")
    finally:
        engine.stop()
        httpd.shutdown()
        engine.store.close()
    return 0


def _setup_demo_env():
    """演示模式：换到临时数据库，绝不碰真实的 usage.db。"""
    import tempfile
    base = tempfile.mkdtemp(prefix="wbum-demo-")
    config.DATA_DIR = base
    config.DB_PATH = os.path.join(base, "demo.db")
    config.LOG_DIR = os.path.join(base, "logs")


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--once", action="store_true", help="只采一轮并打印")
    ap.add_argument("--probe", action="store_true", help="只做数据源自检")
    ap.add_argument("--demo", action="store_true", help="用演示数据启动，不读取任何真实文件")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if a.port:
        config.PORT = a.port
    if a.demo:
        _setup_demo_env()
    setup_logging(a.verbose)

    store = Store()
    if a.demo:
        from adapters.demo_adapter import DemoAdapter
        engine = Engine(store, primary=[DemoAdapter()], secondary=[])
        say("演示模式：数据是生成的，不会读取你的 WorkBuddy 记录。")
    else:
        engine = Engine(store)

    try:
        if a.probe:
            return cmd_probe(engine)
        if a.once:
            return cmd_once(engine)
        return cmd_serve(engine, open_browser_flag=not a.no_browser)
    finally:
        if a.once or a.probe:
            store.close()


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        say()
        say("启动失败，错误信息如下：")
        traceback.print_exc()
        say()
        say(f"如果反复失败，可以把 {config.LOG_DIR} 下的日志发出来排查。")
        code = 1
        # 双击运行（打包成 exe）时别让窗口一闪而过
        if getattr(sys, "frozen", False):
            try:
                input("按回车键退出…")
            except EOFError:
                pass
    sys.exit(code)
