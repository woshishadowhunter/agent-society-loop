import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent_society_loop.providers import OpenAICompatibleProvider


class ProviderHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_body = {"choices": [{"message": {"content": "provider answer"}}]}
    response_headers = {}
    drip_delay = 0.0
    received = None

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        type(self).received = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(self.rfile.read(length)),
        }
        body = json.dumps(type(self).response_body).encode("utf-8")
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in type(self).response_headers.items():
            self.send_header(name, value)
        self.end_headers()
        if type(self).drip_delay:
            for byte in body:
                try:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                except (
                    BrokenPipeError,
                    ConnectionAbortedError,
                    ConnectionResetError,
                ):
                    break
                time.sleep(type(self).drip_delay)
        else:
            self.wfile.write(body)

    def log_message(self, format, *args):
        return


class RedirectTargetHandler(BaseHTTPRequestHandler):
    received = False

    def do_GET(self):
        type(self).received = True
        body = json.dumps(
            {"choices": [{"message": {"content": "redirected answer"}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class OpenAICompatibleProviderTests(unittest.TestCase):
    def setUp(self):
        ProviderHandler.response_status = 200
        ProviderHandler.response_body = {
            "choices": [{"message": {"content": "provider answer"}}]
        }
        ProviderHandler.response_headers = {}
        ProviderHandler.drip_delay = 0.0
        ProviderHandler.received = None
        self.server = HTTPServer(("127.0.0.1", 0), ProviderHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_sends_chat_completion_request_and_returns_content(self):
        provider = OpenAICompatibleProvider("secret-token", self.url, "model-a")

        result = provider.complete([{"role": "user", "content": "hello"}], temperature=0.2)

        self.assertEqual(result, "provider answer")
        self.assertEqual(ProviderHandler.received["path"], "/v1/chat/completions")
        self.assertEqual(ProviderHandler.received["authorization"], "Bearer secret-token")
        self.assertEqual(ProviderHandler.received["body"]["model"], "model-a")
        self.assertEqual(ProviderHandler.received["body"]["temperature"], 0.2)
        self.assertNotIn("response_format", ProviderHandler.received["body"])

    def test_sends_optional_structured_output_format(self):
        provider = OpenAICompatibleProvider(
            "secret-token",
            self.url,
            "model-a",
            response_format={"type": "json_object"},
        )

        provider.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(
            ProviderHandler.received["body"]["response_format"],
            {"type": "json_object"},
        )

    def test_loopback_provider_may_omit_authorization(self):
        provider = OpenAICompatibleProvider("", self.url, "model-a")

        result = provider.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(result, "provider answer")
        self.assertIsNone(ProviderHandler.received["authorization"])

    def test_remote_provider_requires_authorization(self):
        with self.assertRaisesRegex(
            ValueError, "api_key is required for non-loopback model endpoints"
        ):
            OpenAICompatibleProvider("", "https://models.example/v1", "model-a")

    def test_remote_http_and_url_credentials_require_safe_configuration(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            OpenAICompatibleProvider(
                "secret-token", "http://models.example/v1", "model-a"
            )
        provider = OpenAICompatibleProvider(
            "secret-token",
            "http://models.example/v1",
            "model-a",
            allow_insecure_http=True,
        )
        self.assertEqual(provider.base_url, "http://models.example/v1")
        for url in (
            "https://user:secret@models.example/v1",
            "https://models.example/v1?token=secret",
            "https://models.example/v1#fragment",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(
                ValueError, "credentials|query|fragment"
            ):
                OpenAICompatibleProvider("secret-token", url, "model-a")

    def test_provider_rejects_redirect_without_contacting_target(self):
        target = HTTPServer(("127.0.0.1", 0), RedirectTargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        RedirectTargetHandler.received = False
        target_thread.start()
        try:
            ProviderHandler.response_status = 302
            ProviderHandler.response_headers = {
                "Location": f"http://127.0.0.1:{target.server_port}/redirected"
            }
            provider = OpenAICompatibleProvider("secret-token", self.url, "model-a")

            with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                provider.complete([{"role": "user", "content": "hello"}])

            self.assertFalse(RedirectTargetHandler.received)
        finally:
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=2)

    def test_malformed_response_raises_clear_error_without_secret(self):
        ProviderHandler.response_body = {"choices": []}
        provider = OpenAICompatibleProvider("do-not-leak", self.url, "model-a")

        with self.assertRaises(ValueError) as raised:
            provider.complete([{"role": "user", "content": "hello"}])

        self.assertIn("malformed", str(raised.exception))
        self.assertNotIn("do-not-leak", str(raised.exception))

    def test_http_failure_raises_clear_error_without_secret(self):
        ProviderHandler.response_status = 503
        ProviderHandler.response_body = {"error": "unavailable"}
        provider = OpenAICompatibleProvider("do-not-leak", self.url, "model-a")

        with self.assertRaises(RuntimeError) as raised:
            provider.complete([{"role": "user", "content": "hello"}])

        self.assertIn("503", str(raised.exception))
        self.assertNotIn("do-not-leak", str(raised.exception))

    def test_oversized_response_is_rejected_before_json_parsing(self):
        ProviderHandler.response_body = {
            "choices": [{"message": {"content": "x" * 1_048_576}}]
        }
        provider = OpenAICompatibleProvider("secret-token", self.url, "model-a")

        with self.assertRaisesRegex(ValueError, "response exceeds"):
            provider.complete([{"role": "user", "content": "hello"}])

    def test_slow_drip_response_obeys_wall_clock_deadline(self):
        ProviderHandler.drip_delay = 0.05
        provider = OpenAICompatibleProvider(
            "secret-token",
            self.url,
            "model-a",
            timeout=0.15,
        )
        started = time.monotonic()

        with self.assertRaisesRegex(RuntimeError, "deadline"):
            provider.complete([{"role": "user", "content": "hello"}])

        self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
