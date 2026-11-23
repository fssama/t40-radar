import math
import struct
import unittest
from unittest.mock import patch

from backend.app import RadarApp, TRACK_MAX_POINTS, TRACK_WINDOW
from backend.protocol import (
    MSG_PERCEPTION, Record1E61, build_1e61, build_v2, crc8, crc16, parse_1e61, parse_v2,
)
from backend.track import MIN_PTS, Tracker
from tools.radar_udp_sender import encode_scan


def record_frame(records=(), *, seq=1, subtype=2):
    return build_v2(MSG_PERCEPTION, build_1e61(list(records), subtype, seq), seq)


def point(x, y, z=0.0, subtype=2):
    return (math.degrees(math.atan2(y, x)) % 360, math.hypot(x, y), z, 0.9, subtype)


def compact_cluster(x=3.0, y=0.0):
    return [point(x + i * 0.015, y) for i in range(MIN_PTS)]


class ProtocolTests(unittest.TestCase):
    def test_crc_known_vectors(self):
        self.assertEqual(crc8(b"123456789"), 0xFB)
        self.assertEqual(crc16(b"123456789"), 0x7109)

    def test_v2_length_is_exact_and_header_is_validated(self):
        original = record_frame()
        for packet in (original[:28], original[:-1], original + b"\0"):
            with self.subTest(length=len(packet)), self.assertRaisesRegex(ValueError, "short|length"):
                parse_v2(packet)
        incorrect_total = bytearray(original)
        struct.pack_into("<H", incorrect_total, 3, len(original) - 1)
        incorrect_total[23] = crc8(incorrect_total[:23])
        with self.assertRaisesRegex(ValueError, "length"):
            parse_v2(incorrect_total)
        invalid_header = bytearray(original)
        invalid_header[23] ^= 1
        with self.assertRaisesRegex(ValueError, "CRC8"):
            parse_v2(invalid_header)

    def test_crc16_failure_is_diagnostic_only_and_never_updates_live_state(self):
        good = record_frame([Record1E61(1, 30, 40, -8, 32768)])
        bad = bytearray(good)
        bad[-3] ^= 1
        app = RadarApp(source="udp")
        self.assertFalse(parse_v2(bad).crc_ok)
        with self.assertRaisesRegex(ValueError, "CRC16"):
            app.ingest_udp(bad)
        self.assertEqual(app.udp_data_status(), "waiting")
        self.assertEqual(app.frame_count, 0)
        self.assertEqual(app.tracker.frames, 0)
        self.assertEqual(app.ingest_udp(good)["points"][0][:3], [53.13, 5.0, -0.8])

    def test_1e61_requires_exact_record_count_and_finite_coordinates(self):
        good = build_1e61([Record1E61(1, 30, 40, -8, 32768)])
        wrong_count = bytearray(good)
        struct.pack_into("<H", wrong_count, 3, 0)
        nan_record = build_1e61([Record1E61(1, math.nan, 40, -8, 32768)])
        app = RadarApp(source="udp")
        for payload in (good[:-1], good + bytes(16), bytes(wrong_count), nan_record):
            with self.subTest(length=len(payload)), self.assertRaises(ValueError):
                app.ingest_udp(build_v2(MSG_PERCEPTION, payload))
        self.assertEqual(app.frame_count, 0)
        self.assertEqual(parse_1e61(good)["count"], 1)
        self.assertEqual(parse_1e61(build_1e61([]))["count"], 0)

    def test_internal_sim_protocol_and_udp_render_the_same_distances(self):
        app = RadarApp()
        replay = RadarApp(source="udp")
        points = [(225.0, 5.0, -0.8, 0.5, 2), (90.0, 8.0, -1.8, 0.6, 0)]
        raw = {"state": {"rpm": 100, "running": True, "angle": 225},
               "height_cm": 180, "gated": False, "sectors": []}
        with patch("backend.app.build_v2", wraps=build_v2) as pack:
            local = app.build_frame_message(raw, points)
        wire = replay.ingest_udp(build_v2(*pack.call_args.args, **pack.call_args.kwargs))
        self.assertEqual(wire["points"], local["points"][:1])
        self.assertEqual(local["proto"]["count"], 1)

    def test_standalone_sender_round_trip_preserves_units_axes_and_subtype(self):
        points = [(225.0, 5.0, -0.8, 0.5, 2), (90.0, 8.0, -1.8, 0.6, 0),
                  (0.0, 1.25, 2.0, 1.0, 1)]
        app = RadarApp(source="udp")
        frames = encode_scan(points, 9)
        self.assertEqual([parse_v2(f).seq for f in frames], [9, 10, 11])
        self.assertEqual([tuple(app.ingest_udp(f)["points"][0]) for f in frames], points)


class TrackingTests(unittest.TestCase):
    def test_sparse_bridge_does_not_merge_two_dense_targets(self):
        points = compact_cluster(-1.5) + compact_cluster(1.5)
        points += [point(i * 0.4, 0.0) for i in range(-3, 4)]
        clusters = Tracker._cluster(points)
        self.assertEqual(len(clusters), 2)
        self.assertLess(clusters[0]["x"], -1.0)
        self.assertGreater(clusters[1]["x"], 1.0)

    def test_noise_marked_earlier_is_still_admitted_as_border(self):
        points = [point(2.54, 0.0)] + compact_cluster()
        clusters = Tracker._cluster(points)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["n"], MIN_PTS + 1)

    def test_old_cluster_cannot_create_or_confirm_a_track_on_unrelated_frames(self):
        app = RadarApp()
        app.track_step(compact_cluster(), 0.1, now=1.0)
        app.track_step([], 0.1, now=1.1)
        app.track_step([point(-4, 0)], 0.1, now=1.2)
        self.assertEqual(app.tracker.count, 0)
        self.assertEqual(len(app.tracker.tracks), 1)
        self.assertEqual(app.tracker.tracks[0].misses, 2)

    def test_fresh_points_can_use_unexpired_history_and_window_is_capped(self):
        app = RadarApp()
        points = compact_cluster()
        for i in range(3):
            app.track_step(points[:2], 0.1, now=i * 0.1)
        self.assertEqual(len(app.tracker.tracks), 1)
        app.track_step([], 0.1, now=0.2 + TRACK_WINDOW + 0.01)
        self.assertEqual(len(app._pt_win), 0)
        app.track_step([point(3, 0)] * (TRACK_MAX_POINTS + 20), 0.1, now=2.0)
        self.assertEqual(len(app._pt_win), TRACK_MAX_POINTS)

    def test_udp_ground_frames_do_not_break_object_track_confirmation(self):
        app = RadarApp(source="udp")
        returns = [Record1E61.from_metres(i, 3 + i * 0.015, 0, 0, 60000)
                   for i in range(MIN_PTS)]
        for seq in range(3):
            app.ingest_udp(record_frame(returns, seq=seq * 2))
            result = app.ingest_udp(record_frame(seq=seq * 2 + 1, subtype=0))
        self.assertEqual(len(result["tracks"]), 1)
        self.assertAlmostEqual(result["tracks"][0]["range"], 3.03, places=2)
