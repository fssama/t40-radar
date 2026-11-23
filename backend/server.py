"""零依赖 HTTP + SSE 服务器（只用 Python 标准库）。

选 SSE 而不是 WebSocket：标准库直接可用、手机 Termux 上也能跑，
而且在这里数据是单向推送，SSE 完全够用（控制走 POST）。
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .app import RadarApp

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class Handler(BaseHTTPRequestHandler):
    app: RadarApp = None       # 由 create_server 注入
    root: str = ""

    def handle(self):
        try:
            super().handle()
        except ConnectionError:
            pass  # 浏览器刷新或取消请求时可能提前关闭连接

    # 关掉默认的 stderr 噪声
    def log_message(self, fmt, *args):
        pass

    # ---------------- 工具 ----------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), MIME[".json"])

    def _resolve(self, path: str) -> str | None:
        path = path.split("?")[0].split("#")[0]
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        full = os.path.normpath(os.path.join(self.root, rel))
        # 防路径穿越
        if not os.path.abspath(full).startswith(os.path.abspath(self.root) + os.sep):
            return None
        return full if os.path.isfile(full) else None

    # ---------------- 路由 ----------------
    def do_GET(self):
        url = urlparse(self.path)
        p = url.path

        if p == "/events":
            return self._sse()
        if p == "/api/state":
            radar = self.app.radar_status()
            return self._send_json({
                "running": radar["point_cloud"] == "live",
                "frames": self.app.frame_count,
                "crc_ok": True if radar["point_cloud"] == "live" else None,
                "source": "udp",
                "motor_speed": self.app.last_motor,
                "avoidance": self.app.current_avoidance(),
                "radar": radar,
            })
        if p == "/api/protocol":
            avoidance = self.app.current_avoidance()
            return self._send_json({
                "1E61": self.app.last_point_protocol,
                "1E62": avoidance["proto"] if avoidance else None,
                "radar": self.app.radar_status(),
            })

        full = self._resolve(p)
        if not full:
            return self._send(404, b"404 not found", "text/plain; charset=utf-8")
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            return self._send(404, b"404 not found", "text/plain; charset=utf-8")
        self._send(200, body, ctype)

    def do_POST(self):
        if urlparse(self.path).path != "/api/control":
            return self._send_json({"ok": False, "error": "unknown endpoint"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("invalid request length")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or body.get("action") != "motor":
                raise ValueError("unknown action")
            speed = float(body["value"])
            dry = body.get("dry", False)
            if not math.isfinite(speed) or not 0 <= speed <= 1800 or not isinstance(dry, bool):
                raise ValueError("invalid motor command")
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 400)
        return self._send_json(self.app.set_motor(speed, dry))

    # ---------------- SSE ----------------
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        client = self.app.add_client()
        try:
            self.wfile.write(b": connected\n\n")
            status = {"type": "radar_status", "radar": self.app.radar_status(),
                      "avoidance": self.app.current_avoidance()}
            self.wfile.write(b"data: " + json.dumps(status).encode() + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    data = client.q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + data.encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.remove_client(client)


def create_server(app: RadarApp, root: str, host: str = "127.0.0.1",
                  port: int = 8000) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app, "root": root})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
