#!/usr/bin/env python3
"""T40 RD2484R 雷达监控系统 —— 启动入口。

    python run.py                     # 模拟模式，本机 http://127.0.0.1:8000
    python run.py --host 0.0.0.0      # 局域网/手机可访问
    python run.py --source udp        # 接真机：监听 UDP 40204 的 1E61
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

from backend.app import RadarApp  # noqa: E402
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
    ap.add_argument("--scene", default="room",
                    choices=["room", "field", "wire", "obstacle"])
    ap.add_argument("--source", default="sim", choices=["sim", "udp"],
                    help="sim=内置模拟器，udp=监听真机 40204 上行")
    ap.add_argument("--fps", type=float, default=10.0, help="推送帧率")
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = ap.parse_args()

    root = os.path.join(HERE, "frontend")
    app = RadarApp(scene=args.scene, source=args.source, fps=args.fps)
    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}/"

    with create_server(app, root, args.host, args.port) as httpd:
        try:
            app.start()
            print(BANNER)
            print(f"  数据源   : {args.source}")
            print(f"  监听     : http://{args.host}:{args.port}/")
            print(f"  打开     : {url}")
            print(f"  提示     : Ctrl+C 退出\n")
            if args.open:
                threading.Timer(0.6, lambda: webbrowser.open(url)).start()
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n正在退出…")
        finally:
            app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
