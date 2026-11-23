import struct
import unittest
from unittest.mock import patch

from backend.app import RADAR_TIMEOUT_S, RadarApp
from backend.protocol import build_v1, crc8, crc16, parse_1e62


# Default slots from the 200 rpm bench capture: 90° / 0° / 10000 cm.
EMPTY = bytes.fromhex('28 23 00 00 10 27')
HEADER = bytes.fromhex('06 00 05 00 8e 00 10 27 10')
CAPTURED_TAIL = bytes.fromhex('00 00 00 00 00 00 00 00 00 00 9d')
CAPTURED_WIRE = bytes.fromhex('''
    55 18 08 72 00 60 00 57 a7 00 00 00 03 18 00 c8 00 00 04 12 00 64 00 7d
    1e 61 00 9d 00 9d 01 00 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
    00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 80 3f 00 00
    00 00 b1 40 44 fa 00 00 00 00 5d 4d 00 37 00 00 00 00 09 00 01 00 00 00
    2b 04 1a e8 92 b2 19 14 3a a7 8d 92 b7 3f 00 00 d8 6f
''')


class AvoidanceTest(unittest.TestCase):
    def test_all_default_sectors_are_inactive(self):
        data = parse_1e62(HEADER + EMPTY * 16 + CAPTURED_TAIL)
        self.assertEqual(data['active_sectors'], 0)
        self.assertEqual([s['dist_cm'] for s in data['sectors']], [10000] * 16)
        self.assertEqual(data['height_confidence'], 0)
        self.assertEqual(data['counter'], 0x9d)

    def test_distance_high_bytes_in_first_and_last_slots(self):
        first = bytes.fromhex('c4 09 f4 01 c8 00')  # +25°, +5°, 2.00 m
        last = bytes.fromhex('50 46 00 00 2c 01')   # +180°, 0°, 3.00 m
        # +110 is an independent flag; it must not affect confidence.
        tail = bytes.fromhex('b4 00 ff ff 07 01 00 00 00 00 9d')
        data = parse_1e62(HEADER + first + EMPTY * 14 + last + tail)
        hits = [s for s in data['sectors'] if s['valid']]
        self.assertEqual([(s['index'], s['az'], s['el'], s['dist_cm']) for s in hits],
                         [(0, 25.0, 5.0, 200), (15, 180.0, 0.0, 300)])
        self.assertEqual(data['height_confidence'], 7)
        self.assertEqual(data['height_filtered_cm'], -1)

    def test_v1_crc_and_v2_point_cloud_remain_independent(self):
        app = RadarApp()
        payload = HEADER + EMPTY * 16 + CAPTURED_TAIL
        wire = build_v1(0, 24, 6, 3, 123, 0x1E, 0x62, payload)
        self.assertIsNone(app.ingest_udp(wire[:-1] + bytes([wire[-1] ^ 1])))
        self.assertIsNone(app.current_avoidance())
        self.assertIsNone(app.ingest_udp(build_v1(0, 24, 0, 2, 10, 0x1E, 0x32, payload)))

        event = app.ingest_udp(wire)
        self.assertEqual(event['type'], 'avoidance')
        self.assertEqual(event['avoidance']['seq'], 123)
        self.assertEqual(app.frame_count, 0)
        point, = app.ingest_udp(CAPTURED_WIRE)['points']
        self.assertEqual(point[2:], [0.143, 0.0, 1])
        self.assertEqual(app.current_avoidance()['seq'], 123)

    def test_invalid_frames_do_not_mark_feeds_live(self):
        app = RadarApp()
        self.assertIsNone(app.ingest_udp(CAPTURED_WIRE[:-1] + bytes([CAPTURED_WIRE[-1] ^ 1])))
        truncated = bytearray(CAPTURED_WIRE[:-18] + b'\0\0')
        struct.pack_into('<H', truncated, 3, len(truncated))
        truncated[23] = crc8(truncated[:23])
        struct.pack_into('<H', truncated, len(truncated) - 2, crc16(truncated[:-2]))
        with self.assertRaises(ValueError):
            app.ingest_udp(bytes(truncated))
        with self.assertRaises(ValueError):
            app.ingest_udp(build_v1(0, 24, 6, 3, 1, 0x1E, 0x62, bytes(116)))
        self.assertEqual(app.radar_status(), {'point_cloud': 'waiting', 'avoidance': 'waiting'})

    def test_each_stream_expires_independently(self):
        app = RadarApp()
        avoid = build_v1(0, 24, 6, 3, 123, 0x1E, 0x62, HEADER + EMPTY * 16 + CAPTURED_TAIL)
        with patch('backend.app.time.monotonic', return_value=10.0) as clock:
            app.ingest_udp(CAPTURED_WIRE)
            clock.return_value += 1.0
            app.ingest_udp(avoid)
            self.assertTrue(app.running)

            clock.return_value = 10.0 + RADAR_TIMEOUT_S
            self.assertEqual(app.radar_status(), {'point_cloud': 'stale', 'avoidance': 'live'})
            self.assertFalse(app.running)
            app.ingest_udp(CAPTURED_WIRE)
            clock.return_value += 1.0
            self.assertEqual(app.radar_status(), {'point_cloud': 'live', 'avoidance': 'stale'})


if __name__ == '__main__':
    unittest.main()
