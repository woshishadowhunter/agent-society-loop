import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent_society_loop.providers import OpenAICompatibleProvider


class ProviderHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_body = {"choices": [{"message": {"content": "provider answer"}}]}
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


if __name__ == "__main__":
    unittest.main()
