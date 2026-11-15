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
import math
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass

from .protocol import (
    MSG_PERCEPTION, Record1E61, UNIT_DM, build_1e61, build_1e62, build_v2,
    parse_1e61, parse_1e62, parse_v2,
)
from .sim.radar import RadarSimulator
from .track import Tracker


# 机械扫描雷达每帧只覆盖一个扇区，单帧点云里同一个目标往往只有 1–2 个点，
# 达不到聚类的最低点数。所以跟踪器吃的是「最近一圈左右」的滑动窗口点云。
TRACK_WINDOW = 0.35      # 秒（约 2–3 帧；真雷达每帧已是完整一圈）


@dataclass
class Client:
    q: queue.Queue
    born: float


class RadarApp:
    """单例应用：一个 tick 线程 + 多个 SSE 客户端。"""

    def __init__(self, scene: str = "room", source: str = "sim", fps: float = 10.0):
        self.sim = RadarSimulator(scene)
        self.source = source
        self.fps = fps
        self.clients: list[Client] = []
        self.lock = threading.Lock()
        self.seq = 0
        self.frame_count = 0
        self.last_crc_ok = True
        self.last_frame_hex = ""
        self.last_frame_len = 0
        self.last_count = 0
        self.t0 = time.time()
        self._pps_window: list[tuple[float, int]] = []
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.udp_source = None
        self.tracker = Tracker()
        self._last_udp_ts = 0.0
        self._pt_win: list[tuple] = []
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
            # 真机模式：只接收 UDP 40204 上行，不产生任何模拟帧
            return
        self.sim.set_rpm(900.0)
        self.sim.start()
        self.thread = threading.Thread(target=self._loop, name="radar-tick", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()

    # ---------------- 客户端 ----------------
    def add_client(self) -> Client:
        c = Client(queue.Queue(maxsize=32), time.time())
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
            dead = []
            for c in self.clients:
                try:
                    c.q.put_nowait(data)
                except queue.Full:
                    dead.append(c)
            for c in dead:
                self.clients.remove(c)

    # ---------------- 跟踪（滑动窗口） ----------------
    def track_step(self, pts: list, dt: float) -> list:
        """把新点云并入滑动窗口，再用窗口内的点跑跟踪。

        扫描式雷达一圈才铺满 360°，单帧只有一条弧；用窗口累积才能形成
        可聚类的目标形状。窗口长度按「1 圈」量级取，兼顾拖尾与点数。
        """
        now = time.time()
        self._pt_win.extend((now, p) for p in pts)
        cut = now - TRACK_WINDOW
        if self._pt_win and self._pt_win[0][0] < cut:
            self._pt_win = [x for x in self._pt_win if x[0] >= cut]
        # 只把「场景回波」喂给跟踪器：subtype==0 是地面点，它们均匀铺成环，
        # 一旦参与聚类就会被切成几十个假航迹（屏幕上那圈方框就是这么来的）。
        window = [x[1] for x in self._pt_win if x[1][4] != 0]
        return self.tracker.update(window, dt)

    # ---------------- 主循环 ----------------
    def _loop(self):
        dt = 1.0 / self.fps
        while not self._stop.is_set():
            t_start = time.time()
            try:
                self._tick(dt)
            except Exception as e:  # 单帧异常不能杀掉整个推送
                self._broadcast({"type": "error", "msg": f"{type(e).__name__}: {e}"})
            elapsed = time.time() - t_start
            time.sleep(max(0.0, dt - elapsed))

    def _tick(self, dt: float):
        raw = self.sim.tick(dt)
        tracks = self.track_step(raw["points"], dt)
        self._broadcast(self.build_frame_message(raw, raw["points"], tracks))

    def build_frame_message(self, raw: dict, pts: list, tracks: list | None = None) -> dict:
        """把一帧原始点云编码成真实 1E61/V2 帧后再组装成前端消息。"""
        self.seq += 1

        # --- 编码成真实 1E61 payload 并套 V2 帧 ---
        # 单帧 UDP 上限 65535 B，1E61 每点 16 B + 69 B 头，理论最多 4089 点；
        # snapshot 累积多帧时只对前 3900 点编码（协议统计仍有意义）。
        enc_pts = pts if len(pts) <= 3900 else pts[:3900]
        recs = [
            Record1E61(
                id=(i % 22) + 1 if sub == 2 else (i % 65535),
                x=r * math.cos(math.radians(az)),
                y=r * math.sin(math.radians(az)),
                z=z,
                metric=int(max(0, min(65535, snr * 65535))),
            )
            for i, (az, r, z, snr, sub) in enumerate(enc_pts)
        ]
        payload = build_1e61(recs, subtype=2, seq=self.seq)
        frame = build_v2(MSG_PERCEPTION, payload, seq=self.seq)

        # --- 往返校验（每 10 帧全量校验一次，其余只校验头部） ---
        if self.seq % 10 == 0:
            parsed = parse_v2(frame)
            d = parse_1e61(parsed.payload)
            self.last_crc_ok = parsed.crc_ok and d["count"] == len(recs)
        else:
            self.last_crc_ok = True
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
            # 真机（udp）模式：V2 上行帧不含转速，返回 None 让前端显示「—」
            "rpm": raw["state"]["rpm"] if self.source != "udp" else None,
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
    def ingest_udp(self, data: bytes) -> dict | None:
        """解析一条真机 V2/0x1E61 报文，转成与模拟模式同构的前端消息。

        真机不提供转速/高度字段（那些在 1E62 与 DUML 状态里），这里按已知
        协议置 0/False，前端会显示为「—」。
        """
        frame = parse_v2(data)
        if frame.msg != MSG_PERCEPTION:
            return None
        d = parse_1e61(frame.payload)
        recs = d["records"]
        pts = [[round(r.az, 2), round(r.rng, 3), round(r.z * UNIT_DM, 3),
                round(r.metric / 65535.0, 3), d["subtype"]] for r in recs]

        now = time.time()
        dt_udp = (now - self._last_udp_ts) if self._last_udp_ts else 0.1
        dt_udp = max(0.02, min(2.0, dt_udp))
        tracks = self.track_step(pts, dt_udp)
        self.running = True          # UDP 侧收到帧即视为雷达在工作
        self._last_udp_ts = now
        self._pps_window.append((now, len(pts)))
        self._pps_window = [x for x in self._pps_window if now - x[0] <= 3.0]
        pps = sum(n for _, n in self._pps_window) / max(
            1e-6, (now - self._pps_window[0][0]) if len(self._pps_window) > 1 else 1.0)

        self.frame_count += 1
        self.seq = frame.seq
        self.last_crc_ok = frame.crc_ok
        self.last_count = d["count"]
        self.last_frame_len = len(data)
        self.last_frame_hex = data[:40].hex(" ")

        return {
            "type": "frame",
            "source": "udp",
            "t": round(now - self.t0, 3),
            "angle": 0.0,
            "rpm": 0.0,
            "running": True,
            "height_cm": 0,
            "gated": False,
            "points": pts,
            "tracks": tracks,
            "sectors": [],
            "proto": {
                "msg": "0x1E61",
                "seq": frame.seq,
                "len": len(data),
                "count": d["count"],
                "crc_ok": frame.crc_ok,
                "hex": self.last_frame_hex,
            },
            "stats": {
                "frames": self.frame_count,
                "fps": self.fps,
                "pps": round(pps, 1),
                "uptime": round(now - self.t0, 1),
            },
        }


# --------------------------------------------------------------------------
# 真机 UDP 源（雷达到货后启用）：监听 40204，解析 1E61
# --------------------------------------------------------------------------

class UdpSource(threading.Thread):
    """监听 UDP 40204，把真机的 1E61 帧交给回调。"""

    def __init__(self, app: RadarApp, bind_port: int = 40204):
        super().__init__(daemon=True, name="udp-40204")
        self.app = app
        self.bind_port = bind_port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", bind_port))
        self.sock.settimeout(1.0)
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = self.app.ingest_udp(data)
                if msg:
                    self.app._broadcast(msg)
            except Exception:
                continue

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
