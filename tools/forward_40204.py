#!/usr/bin/env python3
"""把雷达发到 VM 192.168.8.1:40204 的 V2 数据转发到 Windows 主机 192.168.59.1:40204。"""
import socket
import sys

LISTEN = ("0.0.0.0", 40204)
DEST = ("192.168.59.1", 40204)

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(LISTEN)
s.settimeout(5.0)
out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

print(f"forward {LISTEN} -> {DEST}", flush=True)
n = 0
while True:
    try:
        data, addr = s.recvfrom(65535)
    except socket.timeout:
        continue
    except KeyboardInterrupt:
        break
    try:
        out.sendto(data, DEST)
    except OSError as e:
        print(f"send err: {e}", flush=True)
        continue
    n += 1
    if n <= 3 or n % 200 == 0:
        print(f"fwd #{n} {len(data)}B from {addr[0]}:{addr[1]}", flush=True)
