"""零依赖 HTTP + SSE 服务器（只用 Python 标准库）。

选 SSE 而不是 WebSocket：标准库直接可用、手机 Termux 上也能跑，
而且在这里数据是单向推送，SSE 完全够用（控制走 POST）。
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
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


SSE_WRITE_TIMEOUT = 5.0


class Handler(BaseHTTPRequestHandler):
    app: RadarApp = None       # 由 create_server 注入
    root: str = ""

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
            return self._send_json(self._state())
        if p == "/api/protocol":
            # 协议演示：把当前配置编码成真实帧，回 hex（可给真机抓包比对）
            return self._send_json(self._protocol_demo())
        if p == "/api/snapshot":
            from urllib.parse import parse_qs
            q = parse_qs(url.query)
            n = max(1, min(80, int(q.get("n", ["12"])[0])))
            return self._send_json(self.app.snapshot(n))

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
        url = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            return self._send_json({"ok": False, "error": "bad json"}, 400)

        if url.path == "/api/control":
            return self._send_json(self._control(body))
        self._send_json({"ok": False, "error": "unknown endpoint"}, 404)

    # ---------------- 业务 ----------------
    def _state(self) -> dict:
        app = self.app
        sim = app.sim
        udp = app.source == "udp"
        with app.sim_lock, app.lock:
            data_status = app.udp_data_status() if udp else None
            return {
                "running": None if udp else sim.state.running,
                "rpm": None if udp else sim.state.rpm,
                "angle": None if udp else sim.state.angle,
                "scene": sim.scene.kind,
                "scenes": sim.scenes,
                "height_cm": None if udp else sim.state.height_cm,
                "frames": app.frame_count,
                "crc_ok": None if udp and data_status != "live" else app.last_crc_ok,
                "source": app.source,
                "motor_speed": app.last_motor,
                **({"data_status": data_status} if udp else {}),
            }

    def _control(self, body: dict) -> dict:
        sim = self.app.sim
        act = body.get("action")
        val = body.get("value")
        if act == "motor":
            # 转速走真机链路（VM 桥 → CAN → ESC）；dry=true 只回执不动电机
            return self.app.set_motor(float(val), bool(body.get("dry", False)))

        with self.app.sim_lock:
            if act == "start":
                sim.start()
            elif act == "stop":
                sim.stop()
            elif act == "toggle":
                sim.stop() if sim.state.running else sim.start()
            elif act == "rpm":
                sim.set_rpm(float(val))
            elif act == "scene":
                sim.set_scene(str(val))
            elif act == "height":
                sim.set_height(float(val))
            elif act == "range":
                sim.view_range = max(1.0, min(50.0, float(val)))
            else:
                return {"ok": False, "error": f"unknown action {act!r}"}
        state = self._state()
        return {"ok": True, **{k: state[k] for k in ("running", "rpm", "scene", "height_cm")}}

    def _protocol_demo(self) -> dict:
        """生成一组示例帧的 hex 说明，供与真机抓包比对。"""
        from .protocol import Record1E61, build_1e61, build_1e62, build_v2
        recs = [Record1E61.from_metres(1, 3.0, 0.4, -0.05, 32000),
                Record1E61.from_metres(1, 4.2, -0.9, -0.12, 28000),
                Record1E61.from_metres(2, 9.5, 1.2, 0.30, 21000)]
        p61 = build_1e61(recs, subtype=2, seq=1)
        f61 = build_v2(0x1E61, p61, seq=1)
        p62 = build_1e62(height_kalman_cm=152, height_filtered_cm=150,
                         height_confidence=88)
        f62 = build_v2(0x1E62, p62, seq=1)
        return {
            "1E61": {"len": len(f61), "hex": f61.hex(" "),
                     "layout": "V2 27B 头 + 1E61 69B 头 + 3×16B 记录 + CRC16"},
            "1E62": {"len": len(f62), "hex": f62.hex(" "),
                     "layout": "V2 27B 头 + 1E62 116B + CRC16"},
            "crc": {"crc8_init": "0x77", "crc16_init": "0x3692"},
        }

    # ---------------- SSE ----------------
    def _sse(self):
        client = self.app.add_client()
        try:
            self.connection.settimeout(SSE_WRITE_TIMEOUT)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    data = client.q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if data is None:
                    break
                self.wfile.write(b"data: " + data.encode() + b"\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.app.remove_client(client)
            self.close_connection = True


def create_server(app: RadarApp, root: str, host: str = "127.0.0.1",
                  port: int = 8000) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app, "root": root})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
