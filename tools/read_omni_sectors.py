#!/usr/bin/env python3
"""Read the RD2484R side stream and export its pre-1E61 polar detections."""

import argparse
from collections import Counter
from contextlib import ExitStack
import csv
import math
from pathlib import Path
import socket
import struct
import time


# OMNI v2 layout from dji-agras-radar/radar_client/radar_streamer.c.
FRAME_SIZE = 60632
HEADER_SIZE = 32
SECTOR_SIZE = 160
RECORD_START = 15
RECORD_SIZE = 20
RECORDS_PER_SECTOR = 7
SECTOR_COUNT = 360
BIN_WIDTH = 22.5


def frame_points(frame, offset, min_range, max_range):
    if len(frame) != FRAME_SIZE or frame[:5] != b"OMNI\x02":
        raise ValueError("expected a complete 60632-byte OMNI v2 frame")
    if frame[6] or frame[28]:  # Producer busy or last CFAR decode failed.
        return None

    points = []
    for slot in range(SECTOR_COUNT):
        start = HEADER_SIZE + slot * SECTOR_SIZE
        if not frame[start]:
            continue
        angle = (offset - slot) % 360
        for index in range(min(frame[start + 1], RECORDS_PER_SECTOR)):
            record = start + RECORD_START + index * RECORD_SIZE
            distance, = struct.unpack_from("<f", frame, record)
            if not math.isfinite(distance) or not min_range <= distance <= max_range:
                continue
            snr, = struct.unpack_from("<H", frame, record + 16)
            points.append((slot, angle, distance, snr))
    return points


def receive_frames(host, port, seconds):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, port))
        sock.send(b"\0")  # radar_streamer registration, on its side data port.
        deadline = time.monotonic() + seconds if seconds is not None else None
        while True:
            remaining = deadline - time.monotonic() if deadline is not None else 2.0
            if remaining <= 0:
                return
            sock.settimeout(remaining)
            try:
                yield sock.recv(65535)
            except TimeoutError:
                if deadline is not None:
                    return
                sock.send(b"\0")
            except ConnectionRefusedError:
                if deadline is not None:
                    raise
                time.sleep(1)
                sock.send(b"\0")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--host", help="radar already running radar_streamer side")
    source.add_argument("--input", type=Path, help="concatenated binary OMNI v2 frames")
    parser.add_argument("--port", type=int, default=9877)
    parser.add_argument("--seconds", type=float, help="sampling duration (default 5 s, or continuous when forwarding)")
    parser.add_argument("--offset", type=float, default=180, help="azimuth of encoder slot 0")
    parser.add_argument("--min-range", type=float, default=1.5)
    parser.add_argument("--max-range", type=float, default=50)
    parser.add_argument("--csv", type=Path, help="write per-frame polar detections to a new CSV")
    parser.add_argument("--capture", type=Path, help="save complete live OMNI v2 frames to a new file")
    parser.add_argument("--forward-host", help="forward unchanged live OMNI datagrams to the monitor")
    parser.add_argument("--forward-port", type=int, default=40204)
    args = parser.parse_args()
    if args.seconds is not None and not 0 < args.seconds < math.inf:
        parser.error("seconds must be positive and finite")
    if not all(0 < port <= 65535 for port in (args.port, args.forward_port)):
        parser.error("ports must be in [1, 65535]")
    if not math.isfinite(args.offset) or not 0 < args.min_range < args.max_range < math.inf:
        parser.error("invalid offset or range limits")
    if args.input and (args.capture or args.forward_host):
        parser.error("--capture and --forward-host require --host")
    seconds = args.seconds if args.seconds is not None else (None if args.forward_host else 5)

    returns = Counter()
    samples = Counter()
    rejected = received = rear_returns = 0
    try:
        with ExitStack() as stack:
            if args.input:
                file = stack.enter_context(args.input.open("rb"))
                frames = iter(lambda: file.read(FRAME_SIZE), b"")
            else:
                frames = receive_frames(args.host, args.port, seconds)
                stack.callback(frames.close)
            capture = stack.enter_context(args.capture.open("xb")) if args.capture else None
            out = None
            if args.forward_host:
                out = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
                destination = (socket.gethostbyname(args.forward_host), args.forward_port)
                print(f"Forwarding {args.host}:{args.port} to {args.forward_host}:{args.forward_port}", flush=True)
            writer = None
            if args.csv:
                file = stack.enter_context(args.csv.open("x", newline="", encoding="utf-8"))
                writer = csv.writer(file)
                writer.writerow(("sample", "encoder_slot", "azimuth_deg", "slant_range_m", "snr_raw"))

            for received, frame in enumerate(frames, 1):
                points = frame_points(frame, args.offset, args.min_range, args.max_range)
                if out is not None:
                    out.sendto(frame, destination)
                if capture:
                    capture.write(frame)
                if points is None:
                    rejected += 1
                    continue
                hits = Counter(int(point[1] // BIN_WIDTH) for point in points)
                returns.update(hits)
                samples.update(hits.keys())
                rear_returns += sum(120 < point[1] < 240 for point in points)
                if writer:
                    writer.writerows((received, *point) for point in points)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")

    if received == rejected:
        parser.exit(1, f"No usable OMNI frames (received {received}, busy/CFAR error {rejected})\n")
    print(f"OMNI samples: {received} received, {rejected} busy/CFAR error, "
          f"{rear_returns} returns with 120 < azimuth < 240 deg")
    print("azimuth_deg       samples_with_returns       returns")
    for index in range(16):
        start = index * BIN_WIDTH
        print(f"{start:5.1f}-{start + BIN_WIDTH:5.1f} {samples[index]:22d} {returns[index]:13d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
