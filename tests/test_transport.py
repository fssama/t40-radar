import http.client
import json
import queue
import socket
import struct
import threading
import time
import unittest
from unittest.mock import Mock, patch

from backend.app import RadarApp, UdpSource
from backend.protocol import build_v1, parse_1e61, parse_v2
from backend.server import Handler, SSE_WRITE_TIMEOUT_S, create_server
from tests.test_avoidance import CAPTURED_TAIL, CAPTURED_WIRE, EMPTY, HEADER
from tests.test_tracking import frame_fixture


class PointProtocolTest(unittest.TestCase):
    def test_v2_checksum_and_length_are_independent(self):
        self.assertTrue(parse_v2(CAPTURED_WIRE).crc_ok)
        bad_tail = CAPTURED_WIRE[:-1] + bytes([CAPTURED_WIRE[-1] ^ 1])
        self.assertFalse(parse_v2(bad_tail).crc_ok)
        self.assertIsNone(RadarApp().ingest_udp(bad_tail))
        bad_header = bytearray(CAPTURED_WIRE)
        bad_header[23] ^= 1
        with self.assertRaisesRegex(ValueError, 'header CRC8 mismatch'):
            parse_v2(bad_header)
        for datagram in (CAPTURED_WIRE[:-1], CAPTURED_WIRE + b'\0'):
            with self.assertRaisesRegex(ValueError, 'V2 length mismatch'):
                parse_v2(datagram)
        with self.assertRaisesRegex(ValueError, 'V2 frame too short'):
            parse_v2(CAPTURED_WIRE[:28])

    def test_1e61_count_checks_short_and_trailing_records(self):
        payload = parse_v2(CAPTURED_WIRE).payload
        for count in (0, 2, 65535):
            bad = bytearray(payload)
            struct.pack_into('<H', bad, 3, count)
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, 'record count mismatch'):
                parse_1e61(bad)
        for partial in (payload[:-1], payload + b'\0'):
            with self.assertRaisesRegex(ValueError, 'record count mismatch'):
                parse_1e61(partial)
        with self.assertRaisesRegex(ValueError, 'payload too short'):
            parse_1e61(payload[:68])

    def test_scaled_coordinates_invalid_points_and_valid_empty_frame(self):
        app = RadarApp()
        bad = (float('nan'), 4, -0.5, 0.5)
        self.assertIsNone(app.ingest_udp(frame_fixture(1, 9, [(3, 4, -0.5, 0.5)])))
        self.assertIsNone(app.ingest_udp(frame_fixture(2, 2, [bad])))
        self.assertEqual(app.radar_status()['point_cloud'], 'waiting')
        self.assertEqual(app.frame_count, 0)
        event = app.ingest_udp(frame_fixture(3, 2, [(3, 4, -0.5, 0.5), bad]))
        self.assertEqual(event['points'], [[53.13, 5.0, -0.5, 0.5, 2]])
        self.assertEqual(event['proto']['count'], 2)  # On-wire record count, even if one point was dropped.
        event = app.ingest_udp(frame_fixture(4, 2))
        self.assertEqual((event['points'], event['proto']['count']), ([], 0))
        self.assertEqual(app.radar_status()['point_cloud'], 'live')


class ReceiverAndSseTest(unittest.TestCase):
    def test_setup_and_unexpected_receive_failures_release_udp_socket(self):
        with patch('backend.app.socket.socket') as socket_factory:
            sock = socket_factory.return_value
            sock.settimeout.side_effect = OSError('socket setup failed')
            with self.assertRaisesRegex(OSError, 'socket setup failed'):
                UdpSource(RadarApp(), 0, '127.0.0.1')
            sock.close.assert_called_once_with()
            sock.reset_mock(side_effect=True)
            sock.getsockname.return_value = ('127.0.0.1', 12345)
            sock.recvfrom.side_effect = OSError('receive failed')
            receiver = UdpSource(RadarApp(), 0, '127.0.0.1')
            with self.assertLogs('backend.app', 'ERROR') as logs:
                receiver.run()
            self.assertIn('Radar UDP receive failed', logs.output[0])
            sock.close.assert_called_once_with()

    def test_refresh_error_does_not_stop_udp_processing(self):
        app = RadarApp()
        app.refresh_tracks = Mock(side_effect=[RuntimeError('track refresh failed'), None, None])
        with patch('backend.app.socket.socket') as factory:
            sock = factory.return_value
            sock.getsockname.return_value = ('127.0.0.1', 12345)
            sock.recvfrom.side_effect = [socket.timeout(), (CAPTURED_WIRE, None), OSError('end')]
            receiver = UdpSource(app, 0, '127.0.0.1')
            clock = iter(range(100, 200))
            with patch('backend.app.time.monotonic', side_effect=lambda: next(clock)):
                with self.assertLogs('backend.app', 'ERROR') as logs:
                    receiver.run()
        self.assertIn('Failed to refresh radar tracks', logs.output[0])
        self.assertEqual(app.frame_count, 1)

    def test_each_slow_client_keeps_the_latest_event_of_each_kind(self):
        app = RadarApp()
        slow, fast = app.add_client(), app.add_client()
        self.assertEqual(fast.get(0)[0], 'radar_status')
        avoidance = {'type': 'avoidance', 'avoidance': {'seq': 10}}
        app._broadcast(avoidance)
        self.assertEqual(json.loads(fast.get(0)[2]), avoidance)
        for seq in range(60):
            frame = {'type': 'frame', 'seq': seq}
            app._broadcast(frame)
            self.assertEqual(json.loads(fast.get(0)[2]), frame)
        self.assertEqual(slow.get(0)[0], 'radar_status')
        self.assertEqual(json.loads(slow.get(0)[2]), avoidance)
        self.assertEqual(json.loads(slow.get(0)[2]), {'type': 'frame', 'seq': 59})
        with self.assertRaises(queue.Empty):
            slow.get(0)

    def test_expired_pending_data_is_skipped_and_write_timeout_removes_client(self):
        app = RadarApp()
        client = app.add_client()
        timestamp = time.monotonic()
        client.put('frame', timestamp - 10, '{"type":"frame","seq":1}')
        client.put('omni', timestamp, '{"type":"omni","points":[]}')
        handler = object.__new__(Handler)
        handler.app = app
        handler.connection = Mock()
        handler.wfile = Mock()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile.write.side_effect = [None, None, TimeoutError('slow client')]
        with patch.object(app, 'add_client', return_value=client):
            handler._sse()
        handler.connection.settimeout.assert_called_once_with(SSE_WRITE_TIMEOUT_S)
        writes = [call.args[0] for call in handler.wfile.write.call_args_list]
        self.assertIn(b'"type": "radar_status"', writes[1])
        self.assertIn(b'"type":"omni"', writes[2])
        self.assertTrue(handler.close_connection)
        self.assertEqual(app.clients, [])

    def test_loopback_udp_sse_and_http_state_expire_and_recover(self):
        app = RadarApp()
        server = create_server(app, '.', port=0)
        self.addCleanup(server.server_close)
        serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        serving.start()
        self.addCleanup(serving.join, 2)
        self.addCleanup(server.shutdown)
        receiver = UdpSource(app, 0, '127.0.0.1')
        self.addCleanup(receiver.stop)
        address = receiver.sock.getsockname()

        def get_json(path):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                conn.request('GET', path)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                return json.load(response)
            finally:
                conn.close()

        stream = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        self.addCleanup(stream.close)
        stream.request('GET', '/events')
        avoid = build_v1(0, 24, 6, 3, 42, 0x1E, 0x62, HEADER + EMPTY * 16 + CAPTURED_TAIL)
        with stream.getresponse() as response, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            self.assertEqual(response.status, 200)

            def read_event(predicate):
                for line in iter(response.readline, b''):
                    if line.startswith(b'data: '):
                        event = json.loads(line[6:])
                        if predicate(event):
                            return event
                self.fail('SSE disconnected before expected event')

            self.assertEqual(read_event(lambda event: True)['radar'],
                             {'point_cloud': 'waiting', 'avoidance': 'waiting'})
            with patch('backend.app.RADAR_TIMEOUT_S', 0.8):
                receiver.start()
                try:
                    sender.sendto(CAPTURED_WIRE[:-1], address)
                    sender.sendto(CAPTURED_WIRE, address)
                    read_event(lambda event: event['type'] == 'frame')
                    sender.sendto(avoid, address)
                    read_event(lambda event: event['type'] == 'avoidance')
                    live = get_json('/api/state')
                    self.assertEqual(live['frames'], 1)
                    self.assertEqual(live['avoidance']['seq'], 42)
                    stale = read_event(lambda event: event.get('radar') ==
                                       {'point_cloud': 'stale', 'avoidance': 'stale'})
                    self.assertIsNone(stale['avoidance'])
                    self.assertIsNone(get_json('/api/state')['avoidance'])
                    self.assertEqual(get_json('/api/protocol')['1E62']['seq'], 42)

                    sender.sendto(frame_fixture(43, 0), address)
                    frame = read_event(lambda event: event['type'] == 'frame')
                    self.assertEqual(frame['points'], [])
                    self.assertEqual(get_json('/api/state')['radar'],
                                     {'point_cloud': 'live', 'avoidance': 'stale'})
                finally:
                    receiver.stop()
                    receiver.join(timeout=2)
        self.assertFalse(receiver.is_alive())
        self.assertEqual(receiver.sock.fileno(), -1)
