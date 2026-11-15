"""DUML V1 帧（CAN 侧，雷达 ESC <-> app.elf / PC 假 FC）。

线上格式（11 B 头 + payload + 2 B CRC16）：
    +0     0x55 SOF
    +1     低 8 位总长
    +2     ((total >> 8) & 0x03) | 0x04
    +3     header CRC8
    +4     (src_type << 5) | (src_id & 0x1F)
    +5     (dst_type << 5) | (dst_id & 0x1F)
    +6..7  seq  (u16 LE)
    +8     flags / ack 位
    +9     msg_hi
    +10    msg_lo
    +11..  payload
    -2..-1 CRC16

注意：真机上 V1 帧通过 USB-CAN（1 Mbit）传输，8 字节分块由
CAN 适配器与驱动负责，本模块只处理逻辑帧。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .crc import crc8, crc16

SOF = 0x55
HEADER_LEN = 11
TAIL_LEN = 2
MAX_FRAME = 1023

# 常用端点（type, id）—— 与真机逆向一致
EP_FC = (6, 3)          # 飞控
EP_SELF = (0, 24)       # 雷达自身 0x18
EP_ALT = (0, 2)         # alt receiver
EP_PC = (0, 10)         # PC debug


@dataclass
class V1Frame:
    src_type: int
    src_id: int
    dst_type: int
    dst_id: int
    seq: int
    flags: int
    msg_hi: int
    msg_lo: int
    payload: bytes = b""
    crc_ok: bool = True

    @property
    def msg(self) -> int:
        return (self.msg_hi << 8) | self.msg_lo

    @property
    def msg_str(self) -> str:
        return f"0x{self.msg:04X}"

    def to_dict(self) -> dict:
        return {
            "src": [self.src_type, self.src_id],
            "dst": [self.dst_type, self.dst_id],
            "seq": self.seq,
            "msg": self.msg_str,
            "payload_len": len(self.payload),
            "crc_ok": self.crc_ok,
        }


def build_v1(src_type: int, src_id: int, dst_type: int, dst_id: int,
             seq: int, cmd_set: int, cmd_id: int,
             payload: bytes = b"", need_ack: bool = True) -> bytes:
    """打包一个完整 V1 帧。cmd_set 在高字节，msg_hi 实际是 cmd_set。"""
    total = HEADER_LEN + len(payload) + TAIL_LEN
    if total > MAX_FRAME:
        raise ValueError(f"frame too large: {total} > {MAX_FRAME}")

    buf = bytearray(total)
    buf[0] = SOF
    buf[1] = total & 0xFF
    buf[2] = ((total >> 8) & 0x03) | 0x04
    buf[3] = crc8(bytes(buf[0:3]))
    buf[4] = (src_type << 5) | (src_id & 0x1F)
    buf[5] = (dst_type << 5) | (dst_id & 0x1F)
    struct.pack_into("<H", buf, 6, seq & 0xFFFF)
    buf[8] = 0x40 if need_ack else 0x00
    buf[9] = cmd_set & 0xFF
    buf[10] = cmd_id & 0xFF
    buf[11:11 + len(payload)] = payload
    struct.pack_into("<H", buf, total - 2, crc16(bytes(buf[0:total - 2])))
    return bytes(buf)


def parse_v1(buf: bytes) -> V1Frame:
    """解析并校验 V1 帧，校验失败时抛 ValueError（保留原始帧以便排查）。"""
    if len(buf) < HEADER_LEN + TAIL_LEN:
        raise ValueError(f"V1 frame too short: {len(buf)}")
    if buf[0] != SOF:
        raise ValueError(f"V1 bad SOF 0x{buf[0]:02X}")
    total = buf[1] | ((buf[2] & 0x03) << 8)
    if total != len(buf):
        raise ValueError(f"V1 length mismatch: hdr={total} actual={len(buf)}")
    if crc8(bytes(buf[0:3])) != buf[3]:
        raise ValueError("V1 header CRC8 mismatch")
    body = buf[0:total - 2]
    want = struct.unpack_from("<H", buf, total - 2)[0]
    crc_ok = crc16(body) == want
    return V1Frame(
        src_type=buf[4] >> 5, src_id=buf[4] & 0x1F,
        dst_type=buf[5] >> 5, dst_id=buf[5] & 0x1F,
        seq=struct.unpack_from("<H", buf, 6)[0],
        flags=buf[8], msg_hi=buf[9], msg_lo=buf[10],
        payload=bytes(buf[11:total - 2]),
        crc_ok=crc_ok,
    )


def split_can_blocks(frame: bytes, block: int = 8) -> list[bytes]:
    """把逻辑帧切成 CAN 数据块，仅用于可视化（真机分包规则未逆向）。"""
    return [frame[i:i + block] for i in range(0, len(frame), block)]
