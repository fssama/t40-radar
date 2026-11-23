import struct
import unittest

from backend.app import RadarApp
from tools.read_omni_sectors import FRAME_SIZE, HEADER_SIZE, RECORD_START, SECTOR_SIZE


class OmniTest(unittest.TestCase):
    def setUp(self):
        self.app = RadarApp()
        self.frame = bytearray(FRAME_SIZE)
        self.frame[:5] = b"OMNI\x02"
        slot = HEADER_SIZE + 20 * SECTOR_SIZE
        self.frame[slot:slot + 2] = b"\x01\x01"
        struct.pack_into("<f", self.frame, slot + RECORD_START, 3.25)

    def test_raw_point_is_forwarded_as_its_own_event(self):
        self.assertEqual(self.app.ingest_udp(bytes(self.frame)),
                         {"type": "omni", "points": [[160, 3.25]]})
        self.assertEqual(self.app.frame_count, 0)
        self.assertIsNone(self.app.current_avoidance())
        self.assertEqual(self.app.radar_status(), {"point_cloud": "waiting", "avoidance": "waiting"})

    def test_bad_or_truncated_frames_cannot_draw_raw_points(self):
        for flag in (6, 28):
            with self.subTest(flag=flag):
                bad = self.frame.copy()
                bad[flag] = 1
                self.assertIsNone(self.app.ingest_udp(bytes(bad)))
        with self.assertRaises(ValueError):
            self.app.ingest_udp(bytes(self.frame[:-1]))
