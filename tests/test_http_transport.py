import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from agent_society_loop.http_transport import (
    HTTPDeadlineExceeded,
    post_bytes,
)


class DelayedHeaderHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        time.sleep(0.07)
        body = b"ok"
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (
            BrokenPipeError,
            ConnectionAbortedError,
            ConnectionResetError,
        ):
            return

    def log_message(self, format, *args):
        return


class HTTPTransportDeadlineTests(unittest.TestCase):
    def test_dns_resolution_returns_at_the_wall_clock_deadline(self):
        original = socket.getaddrinfo

        def blocked_resolution(host, port, *args, **kwargs):
            time.sleep(0.3)
            return original("127.0.0.1", port, *args, **kwargs)

        started = time.monotonic()
        with patch("socket.getaddrinfo", side_effect=blocked_resolution):
            with self.assertRaises(HTTPDeadlineExceeded):
                post_bytes(
                    "http://models.example:9/events",
                    b"{}",
                    {"Content-Type": "application/json"},
                    timeout_seconds=0.05,
                    max_response_bytes=1024,
                )

        self.assertLess(time.monotonic() - started, 0.2)

    def test_dns_and_response_headers_share_one_wall_clock_budget(self):
        server = HTTPServer(("127.0.0.1", 0), DelayedHeaderHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original = socket.getaddrinfo

        def delayed_resolution(host, port, *args, **kwargs):
            time.sleep(0.07)
            return original("127.0.0.1", port, *args, **kwargs)

        started = time.monotonic()
        try:
            with patch("socket.getaddrinfo", side_effect=delayed_resolution):
                with self.assertRaises(HTTPDeadlineExceeded):
                    post_bytes(
                        f"http://models.example:{server.server_port}/events",
                        b"{}",
                        {"Content-Type": "application/json"},
                        timeout_seconds=0.1,
                        max_response_bytes=1024,
                    )
            self.assertLess(time.monotonic() - started, 0.2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
