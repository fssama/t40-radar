#!/usr/bin/env python3
"""T40 RD2484R 雷达监控系统 —— 启动入口。

    python run.py                     # 监听 UDP 40204，页面在 http://127.0.0.1:8000
    python run.py --host 0.0.0.0      # 局域网/手机可访问
    python run.py --open              # 自动打开浏览器
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from backend.app import RadarApp, UdpSource  # noqa: E402
from backend.server import create_server  # noqa: E402

BANNER = r"""
  ____  _____ ___  ____      ____   _    ____    _    ___  _  __
 |  _ \|___ /|  _ \___ \    |  _ \ / \  |  _ \  / \  |__ \| |/ /
 | | | | |_ \| |_) |__) |   | |_) / _ \ | | | |/ _ \   / /| ' /
 | |_| |___) |  _ < __/ /   |  _ </ ___ \| |_| / ___ \ / /_| . \
 |____/|____/|_| \_\___/    |_| \_/_/   \_\____/_/   \_\____|_|\_\
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="T40 RD2484R radar monitor")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，局域网用 0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--source", choices=["udp"], help=argparse.SUPPRESS)
    ap.add_argument("--udp-host", default="0.0.0.0", help="UDP 监听地址")
    ap.add_argument("--udp-port", type=int, default=40204, help="1E61 / 1E62 接收端口")
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = ap.parse_args()

    root = os.path.join(HERE, "frontend")
    app = RadarApp()
    httpd = create_server(app, root, args.host, args.port)
    udp = None
    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{httpd.server_port}/"

    try:
        udp = UdpSource(app, bind_host=args.udp_host, bind_port=args.udp_port)
        udp.start()
        print(BANNER)
        print(f"  实机上行 : UDP {args.udp_host}:{udp.bind_port}")
        print(f"  浏览页面 : {url}")
        print("  提示     : Ctrl+C 退出\n")
        if args.open:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        if udp is not None:
            udp.stop()
            if udp.ident is not None:
                udp.join(timeout=1.0)
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
