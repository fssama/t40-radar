#!/usr/bin/env python3
"""雷达转速控制桥：UDP 50009 -> duml_can_tool -> CAN -> ESC。

在 VM 里以 root 运行：
    sudo /home/ubuntu/work/venv/bin/python /tmp/motor_bridge.py

Windows 后端发 JSON：{"speed": 900, "dry": false}
返回 {"ok": true, "speed": 900, "seq": 3, "out": "..."}
dry=true 时只回执不执行（调试链路用）。
"""
import json
import socket
import subprocess
import sys

LISTEN = ("0.0.0.0", 50009)
CLIENT_DIR = "/home/ubuntu/work/dji-agras-radar/radar_client"
PY = "/home/ubuntu/work/venv/bin/python"
IFACE = "can0"

seq = 0


def run_motor(speed: int, dry: bool) -> dict:
    global seq
    seq += 1
    if dry:
        return {"ok": True, "dry": True, "speed": speed, "seq": seq}
    cmd = [PY, "duml_can_tool.py", "send", "motor-speed",
           "--speed", str(speed), "--seq", str(seq), "--iface", IFACE]
    try:
        r = subprocess.run(cmd, cwd=CLIENT_DIR, capture_output=True, timeout=10)
        out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
        return {"ok": r.returncode == 0, "speed": speed, "seq": seq,
                "rc": r.returncode, "out": out[-400:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": "timeout", "speed": speed, "seq": seq}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": str(e), "speed": speed, "seq": seq}


def main() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(LISTEN)
    print(f"motor-bridge listening on {LISTEN[0]}:{LISTEN[1]}", flush=True)
    while True:
        try:
            data, addr = s.recvfrom(2048)
        except KeyboardInterrupt:
            break
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
            speed = max(0, min(1800, int(msg.get("speed", 0))))
            dry = bool(msg.get("dry", False))
            res = run_motor(speed, dry)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "err": f"bad request: {e}"}
        print(f"[{addr[0]}:{addr[1]}] -> {res}", flush=True)
        s.sendto(json.dumps(res, ensure_ascii=False).encode("utf-8"), addr)


if __name__ == "__main__":
    sys.exit(main())
