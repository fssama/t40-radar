"""DJI T40 RD2484R 雷达链路协议（V1/CAN DUML + V2/UDP 40204）。

协议细节来自 leommxj/dji-agras-radar 的静态逆向笔记，
本包只是把它工程化：可打包、可解析、可往返校验。
"""
from .crc import crc8, crc16, CRC8_INIT, CRC16_INIT
from .duml import V1Frame, build_v1, parse_v1, split_can_blocks
from .v2 import (
    V2Frame,
    parse_v2,
    parse_1e61,
    parse_1e62,
    Record1E61,
    UNIT_DM,
    MSG_PERCEPTION,
    MSG_BLACKBOX,
    SUBTYPE_GROUND,
    SUBTYPE_WIRE,
    SUBTYPE_PCLA,
    EP_PERCEPTION,
    EP_BLACKBOX,
)

__all__ = [
    "crc8", "crc16", "CRC8_INIT", "CRC16_INIT",
    "V1Frame", "build_v1", "parse_v1", "split_can_blocks",
    "V2Frame", "parse_v2",
    "parse_1e61", "Record1E61", "UNIT_DM",
    "parse_1e62",
    "MSG_PERCEPTION", "MSG_BLACKBOX",
    "SUBTYPE_GROUND", "SUBTYPE_WIRE", "SUBTYPE_PCLA",
    "EP_PERCEPTION", "EP_BLACKBOX",
]
