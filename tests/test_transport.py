import http.client
import json
import queue
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.app import RadarApp, UdpSource
from backend.protocol import Record1E61
from backend.server import Handler, SSE_WRITE_TIMEOUT, create_server

from test_protocol_and_tracking import record_frame


class UdpAndHttpTests(unittest.TestCase):
    def test_loopback_waiting_live_expired_recovery_and_socket_release(self):
        app = RadarApp(source="udp")
        receiver = UdpSource(app, 0, bind_host="127.0.0.1")
        app.udp_source = receiver
        self.addCleanup(app.stop)
        udp_address = receiver.sock.getsockname()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)

        httpd = create_server(app, str(Path(__file__).resolve().parents[1] / "frontend"), port=0)
        self.addCleanup(httpd.server_close)
        serving = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
        serving.start()
        self.addCleanup(serving.join, 2.0)
        self.addCleanup(httpd.shutdown)

        def get_state(path="/api/state"):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                return json.load(response)
            finally:
                conn.close()

        stream = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
        self.addCleanup(stream.close)
        stream.request("GET", "/events")
        response = stream.getresponse()
        self.assertEqual(response.status, 200)

        def read_frame():
            for line in iter(response.readline, b""):
                if line.startswith(b"data: "):
                    return json.loads(line[6:])
            self.fail("SSE disconnected before the next frame")

        self.assertEqual(read_frame()["data_status"], "waiting")
        state = get_state()
        self.assertEqual(state["data_status"], "waiting")
        for field in ("running", "rpm", "angle", "height_cm", "crc_ok"):
            self.assertIsNone(state[field])
        self.assertEqual(get_state("/api/snapshot?n=3")["source"], "sim")
        self.assertEqual(get_state()["frames"], 0)

        with patch("backend.app.UDP_DATA_TIMEOUT", 0.5):
            with patch("backend.app.UdpSource", return_value=receiver):
                app.start()
            invalid = bytearray(record_frame(seq=5))
            invalid[-1] ^= 1
            sender.sendto(invalid, udp_address)
            sender.sendto(record_frame([Record1E61(1, 30, 40, -8, 32768)], seq=6), udp_address)
            live = read_frame()
            self.assertEqual(live["data_status"], "live")
            self.assertEqual(live["points"][0][:3], [53.13, 5.0, -0.8])
            for field in ("running", "rpm", "angle", "height_cm", "gated"):
                self.assertIsNone(live[field])
            self.assertEqual(live["stats"]["frames"], 1)
            self.assertEqual(get_state()["data_status"], "live")

            sender.sendto(invalid, udp_address)
            stale = read_frame()
            self.assertEqual(stale["data_status"], "stale")
            self.assertEqual(stale["data_epoch"], live["data_epoch"] + 1)
            self.assertEqual(stale["points"], [])
            self.assertEqual(stale["tracks"], [])
            self.assertIsNone(stale["proto"])
            self.assertEqual(len(app._pt_win), 0)
            self.assertEqual(get_state()["data_status"], "stale")

            sender.sendto(record_frame(seq=7), udp_address)
            recovered = read_frame()
            self.assertEqual(recovered["data_status"], "live")
            self.assertEqual(recovered["data_epoch"], stale["data_epoch"])
            self.assertEqual(recovered["points"], [])
            self.assertEqual(recovered["stats"]["frames"], 2)
            self.assertEqual(recovered["proto"]["count"], 0)

        app.stop()
        self.assertFalse(receiver.is_alive())
        self.assertEqual(receiver.sock.fileno(), -1)
        self.assertEqual(response.read(), b"\n")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rebound:
            rebound.bind(udp_address)

    def test_udp_bind_failure_closes_socket(self):
        with patch("backend.app.socket.socket") as sock_factory:
            sock = sock_factory.return_value
            sock.bind.side_effect = OSError("bind failed")
            with self.assertRaisesRegex(OSError, "bind failed"):
                UdpSource(RadarApp(source="udp"), 0, bind_host="127.0.0.1")
            sock.close.assert_called_once()

    def test_udp_receive_failure_logs_and_closes_socket(self):
        app = RadarApp(source="udp")
        app.ingest_udp(record_frame())
        client = app.add_client()
        with patch("backend.app.socket.socket") as sock_factory:
            sock = sock_factory.return_value
            sock.recvfrom.side_effect = OSError("receive failed")
            receiver = UdpSource(app, 0, bind_host="127.0.0.1")
            with self.assertLogs("backend.app", "ERROR") as logs:
                receiver.run()
            self.assertIn("UDP radar receive failed", logs.output[0])
            sock.close.assert_called_once()
        self.assertEqual(json.loads(client.q.get_nowait())["data_status"], "stale")


class SseBackpressureTests(unittest.TestCase):
    def test_slow_subscriber_keeps_only_latest_and_does_not_delay_fast_one(self):
        app = RadarApp()
        slow = app.add_client()
        fast = app.add_client()
        for i in range(100):
            app._broadcast({"seq": i})
            self.assertEqual(json.loads(fast.q.get_nowait()), {"seq": i})
        self.assertEqual(json.loads(slow.q.get_nowait()), {"seq": 99})
        with self.assertRaises(queue.Empty):
            slow.q.get_nowait()
        self.assertEqual(len(app.clients), 2)
        app.stop()
        self.assertIsNone(slow.q.get_nowait())
        self.assertIsNone(fast.q.get_nowait())

    def test_loopback_stalled_socket_is_removed_after_write_deadline(self):
        app = RadarApp()
        with create_server(app, "", port=0) as httpd:
            accept = httpd.get_request

            def accept_with_small_buffer():
                connection, address = accept()
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                return connection, address

            httpd.get_request = accept_with_small_buffer
            serving = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
            serving.start()
            try:
                with patch("backend.server.SSE_WRITE_TIMEOUT", 0.15):
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                        client.settimeout(2)
                        client.connect(("127.0.0.1", httpd.server_port))
                        client.sendall(b"GET /events HTTP/1.0\r\n\r\n")
                        headers = b""
                        while b"\r\n\r\n" not in headers:
                            response = client.recv(4096)
                            self.assertTrue(response)
                            headers += response
                        self.assertEqual(len(app.clients), 1)
                        large_frame = {"data": "x" * (4 * 1024 * 1024)}
                        deadline = time.monotonic() + 3.0
                        while app.clients and time.monotonic() < deadline:
                            app._broadcast(large_frame)
                            time.sleep(0.03)
                        self.assertEqual(app.clients, [])
            finally:
                app.stop()
                httpd.shutdown()
                serving.join(2.0)

    def test_sse_write_timeout_unregisters_client(self):
        handler = object.__new__(Handler)
        handler.app = RadarApp()
        handler.connection = Mock()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.wfile.write.side_effect = socket.timeout("blocked subscriber")
        handler._sse()
        handler.connection.settimeout.assert_called_once_with(SSE_WRITE_TIMEOUT)
        self.assertEqual(handler.app.clients, [])
        self.assertTrue(handler.close_connection)
