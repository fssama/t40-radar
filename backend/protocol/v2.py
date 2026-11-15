"""DUML V2 帧（UDP 40204，雷达 -> FC 的上行链路）。

线上格式（27 B 头 + payload + 2 B CRC16），来自
`dji_v2_pack_frame` / `dji_v2_unpack_frame` 的逆向笔记：

    +0      0x55 SOF
    +1      低 6 位 = 0x18
    +2      (byte2 & 0xFC) == 0x08
    +3..4   u16 total_len
    +7..10  u32 seq
    +12..13 endpoint 1
    +18..19 endpoint 2
    +23     header CRC8（覆盖 +0..+22）
    +24     msg_hi
    +25     msg_lo
    +27..   payload
    -2..-1  CRC16

两条业务消息：
  0x1E61 perception_over_net_v2  —— 点云（本工程的主要数据源）
  0x00F0 blackbox_over_net_v2    —— 分片黑盒转储
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .crc import crc8, crc16

SOF = 0x55
HEADER_LEN = 27
TAIL_LEN = 2

MSG_PERCEPTION = 0x1E61
MSG_BLACKBOX = 0x00F0

# route key 0x06120400 / 0x061D0000 拆出来的 endpoint 字段
EP_PERCEPTION = (0x12, 0x04)
EP_BLACKBOX = (0x1D, 0x00)

SUBTYPE_GROUND = 0
SUBTYPE_WIRE = 1
SUBTYPE_PCLA = 2


@dataclass
class V2Frame:
    msg: int
    seq: int
    ep1: int
    ep2: int
    payload: bytes
    crc_ok: bool = True

    @property
    def msg_str(self) -> str:
        return f"0x{self.msg:04X}"

    def to_dict(self, hex_head: int = 32) -> dict:
        return {
            "msg": self.msg_str,
            "seq": self.seq,
            "len": HEADER_LEN + len(self.payload) + TAIL_LEN,
            "payload_len": len(self.payload),
            "crc_ok": self.crc_ok,
            "hex": self.payload[:hex_head].hex(" "),
        }


def build_v2(msg: int, payload: bytes, seq: int = 0,
             ep1: int | None = None, ep2: int | None = None) -> bytes:
    if ep1 is None or ep2 is None:
        d1, d2 = EP_PERCEPTION if msg == MSG_PERCEPTION else EP_BLACKBOX
        ep1 = d1 if ep1 is None else ep1
        ep2 = d2 if ep2 is None else ep2

    total = HEADER_LEN + len(payload) + TAIL_LEN
    if total > 0xFFFF:
        raise ValueError(f"V2 frame too large: {total}")

    buf = bytearray(total)
    buf[0] = SOF
    buf[1] = 0x18
    buf[2] = 0x08
    struct.pack_into("<H", buf, 3, total)
    struct.pack_into("<I", buf, 7, seq & 0xFFFFFFFF)
    struct.pack_into("<H", buf, 12, ep1)
    struct.pack_into("<H", buf, 18, ep2)
    buf[23] = crc8(bytes(buf[0:23]))
    buf[24] = (msg >> 8) & 0xFF
    buf[25] = msg & 0xFF
    buf[27:27 + len(payload)] = payload
    struct.pack_into("<H", buf, total - 2, crc16(bytes(buf[0:total - 2])))
    return bytes(buf)


def parse_v2(buf: bytes) -> V2Frame:
    if len(buf) < HEADER_LEN + TAIL_LEN:
        raise ValueError(f"V2 frame too short: {len(buf)}")
    if buf[0] != SOF:
        raise ValueError(f"V2 bad SOF 0x{buf[0]:02X}")
    if (buf[1] & 0x3F) != 0x18:
        raise ValueError(f"V2 bad byte1 0x{buf[1]:02X}")
    if (buf[2] & 0xFC) != 0x08:
        raise ValueError(f"V2 bad byte2 0x{buf[2]:02X}")
    total = struct.unpack_from("<H", buf, 3)[0]
    if total != len(buf):
        raise ValueError(f"V2 length mismatch: hdr={total} actual={len(buf)}")
    if crc8(bytes(buf[0:23])) != buf[23]:
        raise ValueError("V2 header CRC8 mismatch")
    msg = (buf[24] << 8) | buf[25]
    want = struct.unpack_from("<H", buf, total - 2)[0]
    crc_ok = crc16(bytes(buf[0:total - 2])) == want
    return V2Frame(
        msg=msg,
        seq=struct.unpack_from("<I", buf, 7)[0],
        ep1=struct.unpack_from("<H", buf, 12)[0],
        ep2=struct.unpack_from("<H", buf, 18)[0],
        payload=bytes(buf[27:total - 2]),
        crc_ok=crc_ok,
    )


# --------------------------------------------------------------------------
# 0x1E61 perception：69 B 头 + N × 16 B 记录
# --------------------------------------------------------------------------

# 1E61 的 x/y/z 原始数值实测为分米（0.1 m）单位：
#   实测帧点 x/y 落在 12–50，除 10 后 1.2–5.0 m，与 5 m 见方的室内场地吻合；
#   ground 点 z 落在 -4.1–0，除 10 后 -0.41–0 m，正好贴地平面 z≈0；
#   wire 点 z=1.545，除 10 后 0.15 m，线缆垂在雷达下方 15 cm。
# 作者 radar_min_client.py 的字段名虽为 _m，但其注释里 xy 范围 ±2..6 m 同样指向这个比例。
UNIT_DM = 0.1


@dataclass
class Record1E61:
    """一条点云记录：body 坐标系（前 x / 右 y / 上 z，原始单位为分米）。"""
    id: int
    x: float
    y: float
    z: float
    metric: int

    def pack(self) -> bytes:
        return struct.pack("<HfffH", self.id & 0xFFFF, self.x, self.y, self.z,
                           self.metric & 0xFFFF)

    @property
    def az(self) -> float:
        import math
        return math.degrees(math.atan2(self.y, self.x)) % 360.0

    @property
    def rng(self) -> float:
        import math
        return math.sqrt(self.x ** 2 + self.y ** 2) * UNIT_DM


def build_1e61(records: list[Record1E61], subtype: int = SUBTYPE_PCLA,
               seq: int = 0, flags: int = 0x00) -> bytes:
    """打包 1E61 payload。头里 +3=记录数、+5=flags、+65=subtype。"""
    hdr = bytearray(69)
    hdr[0] = seq & 0xFF
    hdr[1] = (seq >> 8) & 0xFF
    hdr[2] = (seq >> 16) & 0xFF
    hdr[3] = len(records) & 0xFF
    hdr[4] = (len(records) >> 8) & 0xFF
    hdr[5] = flags
    struct.pack_into("<H", hdr, 65, subtype & 0xFFFF)
    body = b"".join(r.pack() for r in records)
    return bytes(hdr) + body


def parse_1e61(payload: bytes) -> dict:
    if len(payload) < 69:
        raise ValueError(f"1E61 payload too short: {len(payload)}")
    n = payload[3] | (payload[4] << 8)
    subtype = struct.unpack_from("<H", payload, 65)[0]
    body = payload[69:]
    n = min(n, len(body) // 16)
    recs = []
    for i in range(n):
        rid, x, y, z, metric = struct.unpack_from("<HfffH", body, i * 16)
        recs.append(Record1E61(rid, x, y, z, metric))
    return {"subtype": subtype, "count": n, "records": recs}


# --------------------------------------------------------------------------
# 0x1E62 / 0x1E32 fusion avoidance：116 B
# --------------------------------------------------------------------------

def build_1e62(sectors: list[tuple[int, int, int]] | None = None,
               height_kalman_cm: int = 10000,
               height_filtered_cm: int = 10000,
               height_confidence: int = 0,
               btm_avoid_cm: int = 10000,
               up_avoid_cm: int = 10000,
               counter: int = 0) -> bytes:
    """构建 116 B 融合避障结果。

    sectors: [(az_centideg, el_centideg, dist_cm)] × 16，缺省补"无障碍"项。
    扇区项 6 B 布局按逆向笔记：+0 header / +1..2 az / +3..4 el / +5 dist 低字节，
    dist 高字节落在下一项 +0（最后一项的高字节置 0）。
    """
    buf = bytearray(116)
    buf[0] = 6
    struct.pack_into("<H", buf, 4, btm_avoid_cm)
    struct.pack_into("<H", buf, 6, up_avoid_cm)

    if not sectors:
        sectors = [(9000, 0, 10000)] * 16
    sectors = list(sectors)[:16]
    while len(sectors) < 16:
        sectors.append((9000, 0, 10000))

    dists = [s[2] for s in sectors]
    for i, (az, el, dist) in enumerate(sectors):
        off = 8 + 6 * i
        # +0 存上一项距离的高字节；第一项是 header 0x10
        buf[off] = 0x10 if i == 0 else ((dists[i - 1] >> 8) & 0xFF)
        struct.pack_into("<H", buf, off + 1, az & 0xFFFF)
        struct.pack_into("<H", buf, off + 3, el & 0xFFFF)
        buf[off + 5] = dist & 0xFF

    buf[104] = counter & 0xFF
    struct.pack_into("<H", buf, 105, height_kalman_cm & 0xFFFF)
    struct.pack_into("<H", buf, 107, height_filtered_cm & 0xFFFF)
    struct.pack_into("<H", buf, 109, height_confidence & 0xFFFF)
    return bytes(buf)


def parse_1e62(payload: bytes) -> dict:
    if len(payload) < 116:
        raise ValueError(f"1E62 payload too short: {len(payload)}")
    sectors = []
    for i in range(16):
        off = 8 + 6 * i
        az = struct.unpack_from("<H", payload, off + 1)[0]
        el = struct.unpack_from("<H", payload, off + 3)[0]
        dist_lo = payload[off + 5]
        dist_hi = payload[off + 6] if i < 15 else 0
        sectors.append({"az": az / 100.0, "el": el / 100.0,
                        "dist_cm": (dist_hi << 8) | dist_lo})
    return {
        "marker": payload[0],
        "btm_avoid_cm": struct.unpack_from("<H", payload, 4)[0],
        "up_avoid_cm": struct.unpack_from("<H", payload, 6)[0],
        "counter": payload[104],
        "height_kalman_cm": struct.unpack_from("<H", payload, 105)[0],
        "height_filtered_cm": struct.unpack_from("<H", payload, 107)[0],
        "height_confidence": struct.unpack_from("<H", payload, 109)[0],
        "sectors": sectors,
    }
