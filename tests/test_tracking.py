import math
import struct
import unittest
from unittest.mock import patch

from backend.app import RadarApp
from backend.protocol import crc8, crc16
from backend.track import (COAST_S, MAX_PTS, MAX_WINDOW_POINTS, TRACK_WINDOW, Detection,
                           Tracker, associate, cluster_detections)


def cloud(x, z=0.0):
    return [(x, (i - 2) * 0.02, z, 1.0) for i in range(5)]


def frame_fixture(seq, subtype, points=()):
    """Build an isolated test fixture, with the coordinates converted to wire units."""
    header = bytearray(27)
    header[:3] = bytes.fromhex('55 18 08')
    struct.pack_into('<H', header, 3, 98 + 16 * len(points))
    struct.pack_into('<I', header, 7, seq)
    header[23] = crc8(header[:23])
    header[24:26] = bytes.fromhex('1e 61')
    payload = bytearray(69)
    struct.pack_into('<H', payload, 3, len(points))
    struct.pack_into('<H', payload, 65, subtype)
    for x, y, z, strength in points:
        payload.extend(struct.pack('<HfffH', 1, x * 10.0, y * 10.0, z * 10.0,
                                   round(strength * 65535)))
    frame = header + payload
    return bytes(frame) + struct.pack('<H', crc16(frame))


class TrackingTest(unittest.TestCase):
    def test_only_independent_pcla_measurements_confirm_a_track(self):
        app = RadarApp()
        packet = frame_fixture(10, 2, cloud(3))
        with patch('backend.app.time.monotonic', return_value=100.0) as clock:
            for frame in (packet, frame_fixture(11, 0, cloud(3)), frame_fixture(12, 1, cloud(2)), packet):
                self.assertEqual(app.ingest_udp(frame)['tracks'], [])
                clock.return_value += 0.04
            self.assertEqual([(t.id, t.hits) for t in app.tracker.tracks], [(1, 1)])

            # This PCLA report has no observations. Old points remain in the window.
            app.ingest_udp(frame_fixture(13, 2))
            self.assertEqual(app.tracker.tracks[0].hits, 0)
            for seq in (14, 15, 16):
                clock.return_value += 0.05
                tracks = app.ingest_udp(frame_fixture(seq, 2, cloud(3)))['tracks']
                self.assertEqual(len(tracks), int(seq == 16))
            self.assertEqual(tracks[0]['hits'], 3)
            clock.return_value += 0.05
            wire = app.ingest_udp(frame_fixture(17, 1, cloud(6)))
            self.assertEqual(len(wire['points']), 5)  # the wire layer still gets data
            self.assertEqual([(t['id'], t['hits']) for t in wire['tracks']], [(1, 3)])

    def test_sequence_wrap_and_out_of_order_packet(self):
        tracker = Tracker()
        for idx, seq in enumerate((0xFFFFFFFF, 0, 0xFFFFFFFE, 0)):
            self.assertEqual(tracker.observe(cloud(3), 100 + idx * 0.05, seq), [])
        track, = tracker.observe(cloud(3), 100.25, 1)
        self.assertEqual(track['hits'], 3)
        self.assertEqual(tracker.observe(cloud(3), 106.0, 0), [])
        self.assertEqual([(t.id, t.hits) for t in tracker.tracks], [(2, 1)])

    def test_missing_observation_coasts_then_reacquires_the_same_id(self):
        app = RadarApp()
        with patch('backend.app.time.monotonic', return_value=100.0) as clock:
            for seq in (1, 2, 3):
                clock.return_value += 0.10
                tracks = app.ingest_udp(frame_fixture(seq, 2, cloud(3 + seq * 0.1)))['tracks']
            observed, = tracks
            self.assertEqual(observed['status'], 'observed')
            clock.return_value += 0.10
            predicted, = app.ingest_udp(frame_fixture(4, 2))['tracks']
            self.assertEqual(predicted['status'], 'coasting')
            self.assertGreater(predicted['x'], observed['x'])
            clock.return_value += 0.30
            newer, = app.refresh_tracks(clock.return_value)['tracks']
            self.assertGreater(newer['x'], predicted['x'])
            self.assertEqual(newer['id'], observed['id'])
            self.assertEqual(newer['hits'], 3)

            again, = app.ingest_udp(frame_fixture(5, 2, cloud(3.7)))['tracks']
            self.assertEqual((again['id'], again['status']), (observed['id'], 'observed'))
            clock.return_value += COAST_S + 0.02
            self.assertEqual(app.refresh_tracks(clock.return_value), {'type': 'tracks', 'tracks': []})
            self.assertIsNone(app.refresh_tracks(clock.return_value + 1.0))

    def test_dbscan_does_not_join_clusters_over_a_sparse_bridge(self):
        xs = [1.95 + i * 0.02 for i in range(5)] + [2.43, 2.88, 3.33, 3.78]
        xs += [3.95 + i * 0.02 for i in range(5)]
        self.assertEqual(len(cluster_detections([], [(x, 0, 0, 1.0) for x in xs])), 2)
        detections = cluster_detections([], cloud(3, -2) + cloud(3, 2))
        self.assertEqual(sorted(d.position[2] for d in detections), [-2, 2])

    def test_cluster_needs_fresh_evidence_and_uses_the_fresh_centroid(self):
        old = cloud(3)
        self.assertEqual(cluster_detections(old, [(5, 0, 0, 1.0)]), [])
        detection, = cluster_detections(old, [(3.2, 0, 0, 0.5)])
        self.assertEqual(detection, Detection((3.2, 0, 0), 0.5))

    def test_dense_target_remains_observed_across_the_window(self):
        tracker = Tracker()
        points = [(3.0, (i % 10) * 0.01, 0.0, 1.0) for i in range(85)]
        for seq in range(1, 9):
            tracks = tracker.observe(points, 100 + seq * 0.04, seq)
            if seq >= 3:
                track, = tracks
                self.assertEqual((track['id'], track['status'], track['hits']),
                                 (1, 'observed', seq))
        self.assertEqual(cluster_detections([], points[0:1] * (MAX_PTS + 1)), [])

    def test_history_is_bounded_by_age_and_point_count(self):
        tracker = Tracker()
        old = (3.0, 0.0, 0.0, 1.0)
        new = (4.0, 0.0, 0.0, 1.0)
        size = MAX_WINDOW_POINTS // 2
        with patch('backend.track.cluster_detections', return_value=[]) as cluster:
            tracker.observe([old] * size, 100.00, 1)
            tracker.observe([new] * size, 100.05, 2)
            tracker.observe([new] * size, 100.10, 3)
            history, fresh = cluster.call_args.args
            self.assertEqual(history, [new] * size)
            self.assertEqual(len(history) + len(fresh), MAX_WINDOW_POINTS)
            tracker.observe([old], 100.10 + TRACK_WINDOW + 0.01, 4)
            cluster.assert_called_with([], [old])

    def test_global_matching_preserves_two_existing_ids(self):
        detections = [Detection((x, 0, 0), 1.0) for x in (1.2, 2.2)]
        predicted = [(2, 0, 0), (4.2, 0, 0)]
        self.assertEqual(associate(predicted, detections), {0: 0, 1: 1})
        self.assertEqual(associate([(1, 0, 0)], [Detection((1, 0, 4), 1.0)]), {})

        tracker = Tracker()
        for seq in range(1, 4):
            tracks = tracker.observe(cloud(2) + cloud(4.2), 100 + seq * 0.10, seq)
        ids = [t['id'] for t in tracks]
        tracks = tracker.observe(cloud(1.2) + cloud(2.2), 100.80, 4)
        self.assertEqual([t['id'] for t in tracks], ids)
        self.assertTrue(all(t['status'] == 'observed' for t in tracks))
        self.assertTrue(math.isclose(tracks[0]['range'], 1.56))


if __name__ == '__main__':
    unittest.main()
