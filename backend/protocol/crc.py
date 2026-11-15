"""DJI DUML 链路 CRC（表来自真机 ROM，见 crc_tables.py）。

CRC8  init = 0x77，用于 V1/V2 帧头校验
CRC16 init = 0x3692，用于整帧尾校验
"""
from .crc_tables import T8, T16

CRC8_INIT = 0x77
CRC16_INIT = 0x3692


def crc8(data: bytes, init: int = CRC8_INIT) -> int:
    c = init
    for b in data:
        c = T8[c ^ b]
    return c


def crc16(data: bytes, init: int = CRC16_INIT) -> int:
    c = init
    for b in data:
        c = T16[(c & 0xFF) ^ b] ^ (c >> 8)
    return c
