#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
HTTP + SSE 服务（纯标准库，无第三方依赖）。

路由：
  GET /               看板页面
  GET /api/snapshot   全量快照 JSON
  GET /api/health     健康状态
  GET /api/stream     SSE 增量推送

SSE 而非 WebSocket：单向推送够用，浏览器原生自动重连，实现只需几十行。
"""

import json
import logging
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import config
from core.engine import Engine

log = logging.getLogger("wbum.server")

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "wbum/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 工具 ----------

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, rel: str):
        path = os.path.join(config.WEB_DIR, rel)
        if not os.path.isfile(path):
            self._json({"error": "not found", "path": rel}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---------- 路由 ----------

    def do_GET(self):
        route = urlparse(self.path).path
        engine: Engine = self.server.engine

        if route in ("/", "/index.html"):
            return self._file("index.html")
        if route == "/api/snapshot":
            return self._json(engine.snapshot())
        if route == "/api/health":
            return self._json({"health": engine.health.to_dict(),
                               "capabilities": {k: v.to_dict() for k, v in engine.caps.items()}})
        if route == "/api/stream":
            return self._stream(engine)
        if route.startswith("/static/"):
            return self._file(route[len("/static/"):])
        return self._json({"error": "not found", "route": route}, 404)

    # ---------- SSE ----------

    def _stream(self, engine: Engine):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q: "queue.Queue[dict]" = queue.Queue(maxsize=64)

        def push(event: dict):
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

        def write_chunk(text: str) -> bool:
            try:
                self.wfile.write(text.encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        engine.subscribe(push)
        try:
            # 首帧一次性补齐
            if not write_chunk(
                "event: snapshot\ndata: "
                + json.dumps(engine.snapshot(), ensure_ascii=False)
                + "\n\n"
            ):
                return

            self.close_connection = True
            while True:
                try:
                    ev = q.get(timeout=config.SSE_HEARTBEAT_SEC)
                except queue.Empty:
                    if not write_chunk(": ping\n\n"):
                        break
                    continue
                payload = "event: %s\ndata: %s\n\n" % (
                    ev.get("type", "message"),
                    json.dumps(ev.get("data"), ensure_ascii=False),
                )
                if not write_chunk(payload):
                    break
        finally:
            engine.unsubscribe(push)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # 走到这里时已通过 choose_port 确认没有其他实例在跑，
    # 允许复用是为了覆盖进程被强杀后残留的 TIME_WAIT，避免端口一路往后跳。
    allow_reuse_address = True

    def __init__(self, addr, handler, engine: Engine):
        self.engine = engine
        super().__init__(addr, handler)


def serve(engine: Engine):
    httpd = Server((config.HOST, config.PORT), Handler, engine)
    t = threading.Thread(target=httpd.serve_forever, name="http", daemon=True)
    t.start()
    log.info("看板已就绪: http://%s:%d", config.HOST, config.PORT)
    return httpd
