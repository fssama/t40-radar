#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T40 RD2484R 雷达 UDP 上行模拟器（单文件 · 零依赖）。

用途：在没有真机（或真机还没到货）的情况下，用另一台机器/手机扮演雷达，
      向监控端发送**完全符合 DJI V2 协议**的 0x1E61 感知帧，
      从而端到端演练 "UDP 40204 收帧 -> 解析 -> 显示" 这条真机链路。

用法：
    python radar_udp_sender.py --host 192.168.1.10          # 发到电脑
    python radar_udp_sender.py --host 127.0.0.1 --fps 10    # 本机自测
    python radar_udp_sender.py --host 192.168.1.10 --scene wire --rpm 300

手机 Termux：
    pkg install python
    python radar_udp_sender.py --host <电脑IP> --scene obstacle

协议要点（与 leommxj/dji-agras-radar 的 radar_link_protocols.md 一致）：
    V2 帧 = 27B 头 + payload + 2B CRC16
      +0 0x55
      +1 低 6 位 = 0x18
      +2 (b & 0xFC) == 0x08
      +3..4 total_len (u16, 含 27B 头与 2B CRC)
      +7..10 seq (u32)
      +12..13 ep1, +18..19 ep2
      +23 header CRC8（覆盖 +0..+22）
      +24..25 msg 大端（0x1E61）
    1E61 payload = 69B 头 + N×16B，记录 <H f f f H> = id, x, y, z, metric
      x/y/z 使用分米，场景几何使用米
      头 +3..4 = 记录数 u16，头 +65..66 = subtype
    CRC8  : 反射，poly 0x8C，init 0x77
    CRC16 : 反射，poly 0x8408，init 0x3692
"""
from __future__ import annotations

import argparse
import math
import random
import socket
import struct
import sys
import time

# --------------------------------------------------------------------------
# CRC（用算法生成表，结果与 duml_gen.py 内嵌的 512 项 ROM 表逐字节一致）
# --------------------------------------------------------------------------

def _mk8(poly: int = 0x8C) -> list[int]:
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ poly if c & 1 else c >> 1
        t.append(c & 0xFF)
    return t


def _mk16(poly: int = 0x8408) -> list[int]:
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ poly if c & 1 else c >> 1
        t.append(c & 0xFFFF)
    return t


T8 = _mk8()
T16 = _mk16()


def crc8(data: bytes, crc: int = 0x77) -> int:
    for b in data:
        crc = T8[(crc ^ b) & 0xFF]
    return crc & 0xFF


def crc16(data: bytes, crc: int = 0x3692) -> int:
    for b in data:
        crc = (crc >> 8) ^ T16[(crc ^ b) & 0xFF]
    return crc & 0xFFFF


# --------------------------------------------------------------------------
# 协议组装
# --------------------------------------------------------------------------
MSG_PERCEPTION = 0x1E61
SUBTYPE_PCLA = 2
UNIT_DM = 0.1  # Wire coordinate scale in metres, same as backend/protocol/v2.py.

EP1 = 0x0012   # 与 backend/protocol/v2.py 的 EP_PERCEPTION=(0x12, 0x04) 一致
EP2 = 0x0004


def build_v2(msg: int, payload: bytes, seq: int) -> bytes:
    total = 27 + len(payload) + 2
    b = bytearray(total)
    b[0] = 0x55
    b[1] = 0x18
    b[2] = 0x08
    struct.pack_into("<H", b, 3, total)
    struct.pack_into("<I", b, 7, seq & 0xFFFFFFFF)
    struct.pack_into("<H", b, 12, EP1)
    struct.pack_into("<H", b, 18, EP2)
    b[24] = (msg >> 8) & 0xFF   # msg 是大端：0x1E61 -> 1E 61
    b[25] = msg & 0xFF
    b[23] = crc8(bytes(b[0:23]))
    b[27:27 + len(payload)] = payload
    struct.pack_into("<H", b, total - 2, crc16(bytes(b[0:total - 2])))
    return bytes(b)


def build_1e61(records: list[tuple[int, float, float, float, int]],
               subtype: int = SUBTYPE_PCLA, seq: int = 0) -> bytes:
    hdr = bytearray(69)
    hdr[0] = seq & 0xFF
    hdr[1] = (seq >> 8) & 0xFF
    hdr[2] = (seq >> 16) & 0xFF
    struct.pack_into("<H", hdr, 3, len(records))
    hdr[5] = 0x01
    struct.pack_into("<H", hdr, 65, subtype)
    body = b"".join(struct.pack("<HfffH", r[0] & 0xFFFF, r[1], r[2], r[3], r[4] & 0xFFFF)
                    for r in records)
    return bytes(hdr) + body


# --------------------------------------------------------------------------
def encode_scan(points: list, seq: int) -> list[bytes]:
    """Produce one V2 datagram per subtype present in the scan."""
    groups = {}
    for az, distance, z, snr, subtype in points:
        records = groups.setdefault(subtype, [])
        i = len(records)
        angle = math.radians(az)
        rid = i % 22 + 1 if subtype == SUBTYPE_PCLA else i
        records.append((rid, distance * math.cos(angle) / UNIT_DM,
                        distance * math.sin(angle) / UNIT_DM, z / UNIT_DM,
                        int(max(0.0, min(1.0, snr)) * 65535)))
    frames = []
    for subtype, records in groups.items():
        payload = build_1e61(records, subtype=subtype, seq=seq)
        frames.append(build_v2(MSG_PERCEPTION, payload, seq))
        seq += 1
    return frames


# 简易场景：返回该方位的回波列表 (range_m, z_m, snr, subtype)
# --------------------------------------------------------------------------
def _ray_rect(az_deg: float, half_w: float, half_d: float) -> float:
    a = math.radians(az_deg)
    dx, dy = math.cos(a), math.sin(a)
    tx = half_w / abs(dx) if abs(dx) > 1e-6 else 1e9
    ty = half_d / abs(dy) if abs(dy) > 1e-6 else 1e9
    return min(tx, ty)


class Scene:
    def __init__(self, name: str, rng: random.Random):
        self.name = name
        self.rng = rng

    def ray(self, az: float) -> list[tuple[float, float, float, int]]:
        rng, n = self.rng, self.name
        out: list[tuple[float, float, float, int]] = []
        if n == "room":
            r = _ray_rect(az, 2.6, 3.4) + rng.gauss(0, 0.04)
            h = rng.uniform(0.2, 2.4)
            out.append((r, h, max(0.15, 0.85 - r / 8.0) * rng.uniform(.8, 1.0), 2))
            if rng.random() < 0.25:
                out.append((r * rng.uniform(.55, .95), rng.uniform(0, 1.6),
                            rng.uniform(.1, .35), 2))
        elif n == "field":
            if rng.random() < 0.55:
                r = rng.uniform(3.0, 28.0)
                out.append((r, rng.uniform(-0.15, 1.1) + r * 0.02,
                            max(0.05, 0.45 * math.exp(-r / 12)), 2))
        elif n == "wire":
            # 一条横跨上空的电线：在固定方位角出现
            d = abs(((az - 35 + 180) % 360) - 180)
            if d < 8:
                r = rng.uniform(4.0, 26.0)
                out.append((r, rng.gauss(0.55, 0.03), 0.6, 1))
            if rng.random() < 0.3:
                out.append((rng.uniform(6, 30), rng.uniform(-0.1, 0.3), 0.2, 2))
        else:  # obstacle
            d = abs(((az - 0 + 180) % 360) - 180)
            if d < 12:
                r = 3.4 + rng.gauss(0, 0.05)
                out.append((r, rng.uniform(0.1, 2.0), 0.9, 2))
            else:
                r = _ray_rect(az, 4.0, 5.0) + rng.gauss(0, 0.05)
                out.append((r, rng.uniform(0.1, 2.2), rng.uniform(.2, .7), 2))
        return out


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="RD2484R UDP 上行模拟器")
    ap.add_argument("--host", required=True, help="目标 IP（跑监控端的那台电脑）")
    ap.add_argument("--port", type=int, default=40204,
                    help="目标 UDP 端口，真机/监控端默认 40204")
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--rpm", type=float, default=900.0)
    ap.add_argument("--range", type=float, default=30.0, help="最大量程 m")
    ap.add_argument("--height", type=float, default=1.8, help="雷达离地高度 m")
    ap.add_argument("--scene", default="room",
                    choices=["room", "field", "wire", "obstacle"])
    ap.add_argument("--duration", type=float, default=0.0, help="秒，0=一直发")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    rng = random.Random(args.seed or None)
    scene = Scene(args.scene, rng)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dt = 1.0 / args.fps
    angle = 0.0
    seq = 0
    t0 = time.time()
    total_pts = 0
    last_report = t0

    print(f"[sender] -> {args.host}:{args.port}  fps={args.fps} rpm={args.rpm} "
          f"scene={args.scene} range={args.range}")

    try:
        while True:
            if args.duration and time.time() - t0 >= args.duration:
                break
            ts = time.time()
            # 扫描角推进：每帧只推进一个扫描扇区。
            # 注意不能用 rpm*6*dt（900rpm@10fps = 540°/帧，mod 360 后只剩 0°/180°
            # 两个方位，点云会缺掉大半圈）。
            sweep = max(1.5, min(60.0, args.rpm * 6.0 * dt * 0.35))
            angle = (angle + sweep) % 360.0

            rmin = max(1.5, args.height / math.tan(math.radians(10.0)))
            # (az, r, z, snr, subtype)
            pts: list[tuple[float, float, float, float, int]] = []
            # 地面回波（方位随机，整圈都有）
            for _ in range(45):
                r = rng.uniform(rmin, args.range)
                pts.append((rng.uniform(0.0, 360.0), r, -args.height + rng.gauss(0, 0.03),
                            max(0.05, 0.9 * math.exp(-r / 16.0)) * rng.uniform(.55, 1.0), 0))
            # 场景回波
            n_rays = max(3, min(48, int(sweep / 1.2)))
            for i in range(n_rays):
                az = (angle + (i - n_rays / 2) * sweep / n_rays) % 360.0
                for (r, z, snr, sub) in scene.ray(az):
                    if 1.0 <= r <= args.range:
                        pts.append(((az + rng.gauss(0.0, 0.15)) % 360.0,
                                    r + rng.gauss(0, .015), z, snr, sub))

            frames = encode_scan(pts, seq)
            for frame in frames:
                sock.sendto(frame, (args.host, args.port))
            seq += len(frames)
            total_pts += len(pts)

            now = time.time()
            if not args.quiet and now - last_report >= 2.0:
                print(f"[sender] seq={seq} pts={len(pts)} bytes={sum(map(len, frames))} "
                      f"total={total_pts} elapsed={now - t0:.0f}s")
                last_report = now
            time.sleep(max(0.0, dt - (time.time() - ts)))
    except KeyboardInterrupt:
        print("\n[sender] 已停止")
    finally:
        sock.close()
    print(f"[sender] 共发 {seq} 帧 / {total_pts} 点")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
