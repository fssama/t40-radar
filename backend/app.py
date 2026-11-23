"""接收实机 UDP 1E61、1E62 和侧阵 OMNI v2，推送到网页。"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass

from tools.read_omni_sectors import frame_points

from .protocol import (
    MSG_PERCEPTION, SUBTYPE_PCLA, UNIT_DM, parse_1e61, parse_1e62, parse_v1, parse_v2,
)
from .track import Tracker


RADAR_TIMEOUT_S = 5.0
TRACK_REFRESH_S = 0.25
log = logging.getLogger(__name__)


@dataclass
class Client:
    q: queue.Queue


class RadarApp:
    """维护实机上报的最近数据与 SSE 客户端。"""

    def __init__(self):
        self.clients: list[Client] = []
        self.lock = threading.Lock()
        self.frame_count = 0
        self.last_point_protocol: dict | None = None
        self.t0 = time.time()
        self._pps_window: list[tuple[float, int]] = []
        self.tracker = Tracker()
        self._tracks_visible = False
        self._last_udp_ts: float | None = None
        self.last_avoidance: tuple[float, dict] | None = None
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
            if res.get("ok") and not dry:
                self.last_motor = speed
                self._save_motor(speed)
            return res
        except socket.timeout:
            return {"ok": False, "error": f"motor bridge timeout ({addr})", "speed": speed}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"motor bridge: {exc}", "speed": speed}
        finally:
            sock.close()

    # ---------------- 客户端 ----------------
    def add_client(self) -> Client:
        c = Client(queue.Queue(maxsize=32))
        with self.lock:
            self.clients.append(c)
        return c

    def remove_client(self, c: Client):
        with self.lock:
            if c in self.clients:
                self.clients.remove(c)

    def _broadcast(self, obj: dict):
        data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        with self.lock:
            for c in self.clients:
                try:
                    c.q.put_nowait(data)
                except queue.Full:
                    # 慢客户端跳过积压的旧帧，继续接收新数据。
                    try:
                        c.q.get_nowait()
                    except queue.Empty:
                        pass
                    c.q.put_nowait(data)

    def refresh_tracks(self, timestamp: float) -> dict | None:
        """点云暂停时，让页面按时显示预测状态并清除过期航迹。"""
        if not self._tracks_visible:
            return None
        if self._last_udp_ts is not None and timestamp - self._last_udp_ts < TRACK_REFRESH_S:
            return None
        tracks = self.tracker.snapshot(timestamp)
        self._tracks_visible = bool(tracks)
        return {"type": "tracks", "tracks": tracks}

    # ---------------- 真机 UDP 上行 ----------------
    def radar_status(self) -> dict:
        now = time.monotonic()

        def status(timestamp):
            if timestamp is None:
                return "waiting"
            return "live" if now - timestamp < RADAR_TIMEOUT_S else "stale"

        avoidance = self.last_avoidance
        return {"point_cloud": status(self._last_udp_ts),
                "avoidance": status(avoidance[0] if avoidance else None)}

    @property
    def running(self) -> bool:
        return self.radar_status()["point_cloud"] == "live"

    def current_avoidance(self) -> dict | None:
        last = self.last_avoidance
        if last is None:
            return None
        ts, data = last
        return {**data, "age_s": round(time.monotonic() - ts, 3)}

    def ingest_udp(self, data: bytes) -> dict | None:
        """接收雷达业务报文与原始侧阵回波。"""
        if data.startswith(b"OMNI"):
            raw = frame_points(data, offset=180, min_range=1.5, max_range=50)
            if raw is not None:
                return {"type": "omni", "points": [[az, round(distance, 3)] for _, az, distance, _ in raw]}
            return None
        if len(data) >= 13 and (data[2] & 0xFC) == 0x04:
            frame_v1 = parse_v1(data)
            if frame_v1.msg != 0x1E62 or not frame_v1.crc_ok:
                return None
            avoidance = parse_1e62(frame_v1.payload)
            avoidance.update(seq=frame_v1.seq, msg="0x1E62",
                             proto={"msg": "0x1E62", "seq": frame_v1.seq, "len": len(data),
                                    "crc_ok": True, "hex": data[:40].hex(" ")})
            self.last_avoidance = (time.monotonic(), avoidance)
            return {"type": "avoidance", "avoidance": avoidance}

        frame = parse_v2(data)
        if frame.msg != MSG_PERCEPTION or not frame.crc_ok:
            return None
        d = parse_1e61(frame.payload)
        recs = [r for r in d["records"] if all(map(math.isfinite, (r.x, r.y, r.z)))]
        pts = [[round(r.az, 2), round(r.rng, 3), round(r.z * UNIT_DM, 3),
                round(r.metric / 65535.0, 3), d["subtype"]] for r in recs]

        received_at = time.monotonic()
        if self._last_udp_ts is not None and received_at - self._last_udp_ts >= RADAR_TIMEOUT_S:
            self._pps_window.clear()
        if d["subtype"] == SUBTYPE_PCLA:
            # 跟踪器直接使用原始 XYZ，避免用于显示的极坐标取整误差。
            points = [(r.x * UNIT_DM, r.y * UNIT_DM, r.z * UNIT_DM, r.metric / 65535.0)
                      for r in recs]
            tracks = self.tracker.observe(points, received_at, frame.seq)
        else:
            tracks = self.tracker.snapshot(received_at)
        self._tracks_visible = bool(tracks)
        now = time.time()
        self._last_udp_ts = received_at
        self._pps_window.append((now, len(pts)))
        self._pps_window = [x for x in self._pps_window if now - x[0] <= 3.0]
        pps = sum(n for _, n in self._pps_window) / max(
            1e-6, (now - self._pps_window[0][0]) if len(self._pps_window) > 1 else 1.0)

        self.frame_count += 1
        self.last_point_protocol = {"msg": "0x1E61", "seq": frame.seq, "len": len(data),
                                    "count": d["count"], "subtype": d["subtype"],
                                    "crc_ok": True, "hex": data[:40].hex(" ")}

        return {
            "type": "frame",
            "source": "udp",
            "t": round(now - self.t0, 3),
            "points": pts,
            "tracks": tracks,
            "proto": self.last_point_protocol,
            "stats": {
                "frames": self.frame_count,
                "pps": round(pps, 1),
                "uptime": round(now - self.t0, 1),
            },
        }


# --------------------------------------------------------------------------
# 真机 UDP 源：监听 40204，接收 VM 转发的 1E61 和 1E62
# --------------------------------------------------------------------------

class UdpSource(threading.Thread):
    """监听 UDP 40204，把真机报文交给回调。"""

    def __init__(self, app: RadarApp, bind_port: int = 40204, bind_host: str = "0.0.0.0"):
        super().__init__(daemon=True, name="udp-40204")
        self.app = app
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((bind_host, bind_port))
        except OSError:
            self.sock.close()
            raise
        self.bind_port = self.sock.getsockname()[1]
        self.sock.settimeout(TRACK_REFRESH_S)
        self._stopping = threading.Event()

    def run(self):
        previous = self.app.radar_status()
        next_track_refresh = time.monotonic() + TRACK_REFRESH_S
        while not self._stopping.is_set():
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                pass
            except OSError:
                break
            else:
                try:
                    msg = self.app.ingest_udp(data)
                    if msg:
                        self.app._broadcast(msg)
                except ValueError:
                    pass   # malformed datagram
                except Exception:
                    log.exception("Failed to process radar datagram")
            now = time.monotonic()
            if now >= next_track_refresh:
                update = self.app.refresh_tracks(now)
                if update is not None:
                    self.app._broadcast(update)
                next_track_refresh = now + TRACK_REFRESH_S
            status = self.app.radar_status()
            if status != previous:
                self.app._broadcast({"type": "radar_status", "radar": status})
                previous = status

    def stop(self):
        self._stopping.set()
        self.sock.close()
