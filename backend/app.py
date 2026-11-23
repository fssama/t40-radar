"""应用层：把模拟器 / 真机 UDP 的数据统一成「帧」广播给前端。

数据路径刻意走真实协议栈：

    RadarSimulator.tick()
        -> Record1E61 列表
        -> build_1e61()   生成 69 B 头 + N×16 B 的真实 payload
        -> build_v2()     套上 27 B V2 头 + CRC8/CRC16
        -> parse_v2()/parse_1e61()  往返校验
        -> JSON 广播

这样接上真雷达时，只需要把 SimSource 换成 UdpSource，前端与协议层零改动。
"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import socket
import threading
import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass

from .protocol import (
    MSG_PERCEPTION, SUBTYPE_GROUND, SUBTYPE_PCLA, Record1E61, UNIT_DM,
    build_1e61, build_1e62, build_v2,
    parse_1e61, parse_1e62, parse_v2,
)
from .sim.radar import RadarSimulator
from .track import Tracker


logger = logging.getLogger(__name__)
TRACK_WINDOW = 0.35       # Seconds of historical support for new returns.
TRACK_MAX_POINTS = 4096   # Enough for one maximum-size V2 perception frame.
UDP_DATA_TIMEOUT = 2.0


@dataclass
class Client:
    q: queue.Queue

    def offer(self, data: str | None) -> None:
        """Replace the pending frame while the application holds its client lock."""
        try:
            self.q.get_nowait()
        except queue.Empty:
            pass
        self.q.put_nowait(data)


class RadarApp:
    """单例应用：一个 tick 线程 + 多个 SSE 客户端。"""

    def __init__(self, scene: str = "room", source: str = "sim", fps: float = 10.0):
        self.sim = RadarSimulator(scene)
        self.source = source
        self.fps = fps
        self.clients: list[Client] = []
        self.lock = threading.Lock()
        self.sim_lock = threading.Lock()
        self.seq = 0
        self.frame_count = 0
        self.last_crc_ok = None if source == "udp" else True
        self.last_frame_hex = ""
        self.last_frame_len = 0
        self.last_count = 0
        self.t0 = time.time()
        self._pps_window: list[tuple[float, int]] = []
        self._stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.udp_source: UdpSource | None = None
        self.tracker = Tracker()
        self._last_udp_ts: float | None = None
        self._last_track_ts: float | None = None
        self._udp_stale = False
        self._udp_epoch = 0
        self._pt_win: deque = deque(maxlen=TRACK_MAX_POINTS)
        self.last_motor: int | None = None
        self._motor_state_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".motor_state")
        # 后端重启不应让「已下发」的转速显示丢失：ESC 会一直保持最后一次目标速度。
        try:
            with open(self._motor_state_path, "r", encoding="utf-8") as fh:
                self.last_motor = int(json.load(fh).get("speed"))
        except Exception:  # noqa: BLE001
            pass

    def _save_motor(self, speed: int | None) -> None:
        try:
            with open(self._motor_state_path, "w", encoding="utf-8") as fh:
                json.dump({"speed": speed, "ts": time.time()}, fh)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 转速控制 ----------------
    def set_motor(self, speed: float, dry: bool = False) -> dict:
        """下发转速：UDP → VM motor_bridge.py → duml_can_tool → CAN → ESC。

        环境变量 T40_MOTOR_BRIDGE 指定桥地址，默认 192.168.59.140:50009。
        dry=True 时桥只回执不执行，用于验证链路而不动电机。
        """
        addr = os.environ.get("T40_MOTOR_BRIDGE", "192.168.59.140:50009")
        host, _, port = addr.partition(":")
        speed = max(0, min(1800, int(round(speed))))
        payload = json.dumps({"speed": speed, "dry": dry}).encode("utf-8")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5.0)
        try:
            sock.sendto(payload, (host, int(port or 50009)))
            data, _ = sock.recvfrom(8192)
            res = json.loads(data.decode("utf-8", "replace"))
            res.setdefault("speed", speed)
            if res.get("ok"):
                self.last_motor = speed
                self._save_motor(speed)
            return res
        except socket.timeout:
            return {"ok": False, "error": f"motor bridge timeout ({addr})", "speed": speed}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"motor bridge: {exc}", "speed": speed}
        finally:
            sock.close()

    # ---------------- 生命周期 ----------------
    def start(self):
        if self.source == "udp":
            self.udp_source = UdpSource(self)
            self.udp_source.start()
            return
        self.sim.set_rpm(900.0)
        self.sim.start()
        self.thread = threading.Thread(target=self._loop, name="radar-tick", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop_event.set()
        if self.udp_source is not None:
            self.udp_source.stop()
            if self.udp_source.ident is not None:
                self.udp_source.join()
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join()
        with self.lock:
            for client in self.clients:
                client.offer(None)
            self.clients.clear()

    # ---------------- 客户端 ----------------
    def add_client(self) -> Client:
        c = Client(queue.Queue(maxsize=1))
        with self.lock:
            if self._stop_event.is_set():
                c.offer(None)
                return c
            self.clients.append(c)
            if self.source == "udp":
                status = self.udp_data_status()
                if status != "live":
                    c.offer(json.dumps(self._udp_message(status)))
        return c

    def remove_client(self, c: Client):
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)

    def _broadcast(self, obj: dict):
        data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        with self.lock:
            for client in self.clients:
                client.offer(data)

    # ---------------- 跟踪（滑动窗口） ----------------
    def track_step(self, pts: list, dt: float, *, now: float | None = None) -> list:
        """Keep a bounded history of non-ground returns and mark the current frame."""
        now = time.monotonic() if now is None else now
        cut = now - TRACK_WINDOW
        while self._pt_win and self._pt_win[0][0] < cut:
            self._pt_win.popleft()
        fresh = [p for p in pts if p[4] != SUBTYPE_GROUND]
        self._pt_win.extend((now, p) for p in fresh)
        if not fresh:
            return self.tracker.update([], dt)

        window = [p for _, p in self._pt_win]
        fresh_start = max(0, len(window) - len(fresh))
        return self.tracker.update(window, dt, fresh_start=fresh_start)

    def snapshot(self, n: int) -> dict:
        """Generate an offline snapshot with isolated simulation state and statistics."""
        offline = RadarApp(fps=self.fps)
        with self.sim_lock:
            offline.sim = deepcopy(self.sim)
        dt = 1.0 / self.fps
        pts = []
        for i in range(n):
            raw = offline.sim.tick(dt)
            pts.extend(raw["points"])
            tracks = offline.track_step(raw["points"], dt, now=i * dt)
        return offline.build_frame_message(raw, pts, tracks)

    # ---------------- 主循环 ----------------
    def _loop(self):
        dt = 1.0 / self.fps
        while not self._stop_event.is_set():
            t_start = time.monotonic()
            try:
                self._tick(dt)
            except Exception as e:  # 单帧异常不能杀掉整个推送
                self._broadcast({"type": "error", "msg": f"{type(e).__name__}: {e}"})
            elapsed = time.monotonic() - t_start
            self._stop_event.wait(max(0.0, dt - elapsed))

    def _tick(self, dt: float):
        with self.sim_lock:
            raw = self.sim.tick(dt)
        tracks = self.track_step(raw["points"], dt)
        self._broadcast(self.build_frame_message(raw, raw["points"], tracks))

    def build_frame_message(self, raw: dict, pts: list, tracks: list | None = None) -> dict:
        """把一帧原始点云编码成真实 1E61/V2 帧后再组装成前端消息。"""
        self.seq += 1

        # --- 编码成真实 1E61 payload 并套 V2 帧 ---
        # 单帧 UDP 上限 65535 B，1E61 每点 16 B + 69 B 头，理论最多 4089 点；
        # snapshot 累积多帧时只对前 3900 点编码（协议统计仍有意义）。
        # One 1E61 frame can carry only one subtype. The protocol panel
        # shows a representative obstacle frame when obstacle returns exist.
        subtype = SUBTYPE_PCLA
        enc_pts = [p for p in pts if p[4] == subtype]
        if not enc_pts and pts:
            subtype = pts[0][4]
            enc_pts = [p for p in pts if p[4] == subtype]
        recs = [
            Record1E61.from_metres(
                i % 22 + 1 if subtype == SUBTYPE_PCLA else i,
                r * math.cos(math.radians(az)),
                r * math.sin(math.radians(az)),
                z,
                int(max(0, min(65535, snr * 65535))),
            )
            for i, (az, r, z, snr, _) in enumerate(enc_pts[:3900])
        ]
        payload = build_1e61(recs, subtype=subtype, seq=self.seq)
        frame = build_v2(MSG_PERCEPTION, payload, seq=self.seq)

        parsed = parse_v2(frame)
        self.last_crc_ok = parsed.crc_ok and parse_1e61(parsed.payload)["count"] == len(recs)
        self.last_frame_hex = frame[:40].hex(" ")
        self.last_frame_len = len(frame)
        self.last_count = len(recs)

        # --- 1E62 融合结果（每 6 帧一次） ---
        sectors = raw["sectors"]
        if self.seq % 6 == 0:
            e62 = build_1e62(
                sectors=sectors,
                height_kalman_cm=raw["height_cm"],
                height_filtered_cm=raw["height_cm"],
                height_confidence=88,
                counter=self.seq % 256,
            )
            frame62 = build_v2(0x1E62, e62, seq=self.seq)
            p62 = parse_1e62(parse_v2(frame62).payload)
            self.last_62 = p62

        # --- 帧率统计 ---
        now = time.time()
        self._pps_window.append((now, len(pts)))
        self._pps_window = [x for x in self._pps_window if now - x[0] <= 3.0]
        pps = sum(n for _, n in self._pps_window) / max(1e-6, (now - self._pps_window[0][0]) if len(self._pps_window) > 1 else 1.0)

        self.frame_count += 1
        return {
            "type": "frame",
            "t": round(now - self.t0, 3),
            "angle": raw["state"]["angle"],
            "source": "sim",
            "rpm": raw["state"]["rpm"],
            "running": raw["state"]["running"],
            "height_cm": raw["height_cm"],
            "gated": raw["gated"],
            "points": [[round(az, 2), round(r, 3), round(z, 3), round(snr, 3), sub]
                       for (az, r, z, snr, sub) in pts],
            "tracks": tracks or [],
            "sectors": sectors,
            "proto": {
                "msg": "0x1E61",
                "seq": self.seq,
                "len": self.last_frame_len,
                "count": self.last_count,
                "crc_ok": self.last_crc_ok,
                "hex": self.last_frame_hex,
            },
            "stats": {
                "frames": self.frame_count,
                "fps": self.fps,
                "pps": round(pps, 1),
                "uptime": round(now - self.t0, 1),
            },
        }

    # ---------------- 真机 UDP 上行 ----------------
    def udp_data_status(self, now: float | None = None) -> str:
        last = self._last_udp_ts
        if last is None:
            return "waiting"
        now = time.monotonic() if now is None else now
        return "stale" if self._udp_stale or now - last >= UDP_DATA_TIMEOUT else "live"

    def _udp_message(self, status: str, *, points: list | None = None,
                     tracks: list | None = None, proto: dict | None = None,
                     fps: float = 0.0, pps: float = 0.0) -> dict:
        uptime = time.time() - self.t0
        return {
            "type": "frame",
            "source": "udp",
            "data_status": status,
            "data_epoch": self._udp_epoch,
            "t": round(uptime, 3),
            "angle": None,
            "rpm": None,
            "running": None,
            "height_cm": None,
            "gated": None,
            "points": points if points is not None else [],
            "tracks": tracks if tracks is not None else [],
            "sectors": [],
            "proto": proto,
            "stats": {
                "frames": self.frame_count,
                "fps": round(fps, 1),
                "pps": round(pps, 1),
                "uptime": round(uptime, 1),
            },
        }

    def expire_udp(self, now: float | None = None, *, disconnected: bool = False):
        """Send a single clearing frame when valid 1E61 traffic expires."""
        if self._last_udp_ts is None or self._udp_stale:
            return
        if disconnected or self.udp_data_status(now) == "stale":
            self._udp_stale = True
            self._udp_epoch += 1
            self._clear_tracking()
            self._pps_window.clear()
            self._broadcast(self._udp_message("stale"))

    def _clear_tracking(self):
        self._last_track_ts = None
        self._pt_win.clear()
        self.tracker.tracks.clear()

    def ingest_udp(self, data: bytes) -> dict | None:
        """Accept only complete 1E61 datagrams that pass both CRC checks."""
        frame = parse_v2(data)
        if not frame.crc_ok:
            raise ValueError("V2 frame CRC16 mismatch")
        if frame.msg != MSG_PERCEPTION:
            return None
        decoded = parse_1e61(frame.payload)
        pts = [[round(r.az, 2) % 360.0, round(r.rng, 3), round(r.z * UNIT_DM, 3),
                round(r.metric / 65535.0, 3), decoded["subtype"]]
               for r in decoded["records"]]

        now = time.monotonic()
        self.expire_udp(now)
        if self._last_track_ts is not None and now - self._last_track_ts >= UDP_DATA_TIMEOUT:
            self._clear_tracking()
        if decoded["subtype"] == SUBTYPE_GROUND:
            tracks = self.tracker.snapshot()
        else:
            dt = 0.1 if self._last_track_ts is None else now - self._last_track_ts
            tracks = self.track_step(pts, max(0.02, dt), now=now)
            self._last_track_ts = now
        with self.lock:
            self._last_udp_ts = now
            self._udp_stale = False
            self._pps_window.append((now, len(pts)))
            self._pps_window = [x for x in self._pps_window if now - x[0] <= 3.0]
            elapsed = now - self._pps_window[0][0]
            fps = (len(self._pps_window) - 1) / elapsed if elapsed > 0 else 0.0
            pps = sum(n for _, n in self._pps_window) / max(1.0, elapsed)

            self.frame_count += 1
            self.seq = frame.seq
            self.last_crc_ok = True
            self.last_count = decoded["count"]
            self.last_frame_len = len(data)
            self.last_frame_hex = data[:40].hex(" ")
            proto = {
                "msg": "0x1E61",
                "seq": frame.seq,
                "len": self.last_frame_len,
                "count": self.last_count,
                "crc_ok": self.last_crc_ok,
                "hex": self.last_frame_hex,
            }
            return self._udp_message("live", points=pts, tracks=tracks, proto=proto,
                                     fps=fps, pps=pps)


class UdpSource(threading.Thread):
    """Own the socket used to receive and dispatch radar datagrams."""

    def __init__(self, app: RadarApp, bind_port: int = 40204, *,
                 bind_host: str = "0.0.0.0"):
        super().__init__(daemon=True, name="radar-udp")
        self.app = app
        self.bind_port = bind_port
        self._stop_event = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((bind_host, bind_port))
            self.sock.settimeout(0.25)
        except BaseException:
            self.sock.close()
            raise

    def run(self):
        try:
            while not self._stop_event.is_set():
                self.app.expire_udp()
                try:
                    data, _ = self.sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except (InterruptedError, ConnectionResetError):
                    continue
                except OSError:
                    if not self._stop_event.is_set():
                        logger.exception("UDP radar receive failed")
                    break
                if self._stop_event.is_set():
                    break
                try:
                    msg = self.app.ingest_udp(data)
                except ValueError as exc:
                    logger.debug("Discarded UDP radar frame: %s", exc)
                    continue
                except Exception:
                    logger.exception("Unexpected UDP radar frame processing error")
                    continue
                if msg is not None:
                    self.app._broadcast(msg)
        finally:
            self.sock.close()
            if not self._stop_event.is_set():
                self.app.expire_udp(disconnected=True)

    def stop(self):
        self._stop_event.set()
        # recvfrom has a deadline. Let the running thread close its own socket.
        if self.ident is None:
            self.sock.close()
