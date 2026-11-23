#!/usr/bin/env python3
"""Run the upstream flight-mode client and forward only raw 1E62 UDP frames.

Run on the Linux AP alongside forward_40204.py. This process has no CAN or
motor control options and holds flight_mode at 2 from the first heartbeat.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import socket
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_dir", type=Path, help="upstream radar_client directory")
    parser.add_argument("host", help="host running the Web backend")
    parser.add_argument("--port", type=int, default=40204)
    args = parser.parse_args()
    if not (args.client_dir / "radar_min_client.py").is_file():
        parser.error("client_dir must contain radar_min_client.py")
    sys.path.insert(0, str(args.client_dir.resolve()))
    from radar_min_client import ClientConfig, RadarClient, parse_v1_frame

    class AvoidanceRelay(RadarClient):
        def send_v1(self, msg_id, *args, **kwargs):
            # Keep the relay off the ESC path, even if upstream adds tasks.
            if msg_id in (0x1E21, 0x1E25, 0x1E27):
                super().send_v1(msg_id, *args, **kwargs)

        def handle_v1_datagram(self, data, peer):
            # Filter before parsing: byte 9/10 is the command ID on the wire.
            if peer[0] != self.config.radar_ip or data[9:11] != b"\x1e\x62":
                return
            frame = parse_v1_frame(data)
            if frame is not None and len(frame.payload) == 116:
                out.sendto(data, destination)

    destination = (socket.gethostbyname(args.host), args.port)
    config = ClientConfig(flight_mode=2, fm_ramp_sec=0, v2_local_port=40205)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as out:
        client = AvoidanceRelay(config)
        print(f"Forwarding radar 1E62 to {destination[0]}:{destination[1]}", flush=True)
        try:
            client.run()
        except KeyboardInterrupt:
            pass
        finally:
            # Upstream close() sends a motor-stop command over UDP.
            client.control_sock.close()
            client.v2_sock.close()


if __name__ == "__main__":
    main()
