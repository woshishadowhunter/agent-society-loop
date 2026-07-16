import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeA2AServer:
    def __init__(self):
        self.httpd = None
        self.thread = None
        self.requests = []
        self.card_overrides = {}
        self.card_content_type = "application/json"
        self.card_body_override = None
        self.send_status = 200
        self.send_response = {
            "message": {
                "messageId": "reply-1",
                "role": "ROLE_AGENT",
                "parts": [{"text": "direct answer"}],
            }
        }
        self.send_body_override = None

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                owner._record(self, None)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", owner.card_url)
                    self.end_headers()
                    return
                if self.path == "/large":
                    owner._respond(self, 200, b"x" * 4096, "application/json")
                    return
                if self.path == "/bad-type":
                    owner._respond(self, 200, b"{}", "text/html")
                    return
                if self.path == "/.well-known/agent-card.json":
                    owner._respond(
                        self,
                        200,
                        owner.card_bytes,
                        owner.card_content_type,
                    )
                    return
                owner._respond(self, 404, b'{"error":"missing"}')

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                owner._record(self, body)
                if self.path == "/a2a/message:send":
                    response = (
                        owner.send_body_override
                        if owner.send_body_override is not None
                        else json.dumps(owner.send_response).encode("utf-8")
                    )
                    owner._respond(self, owner.send_status, response)
                    return
                owner._respond(self, 404, b'{"error":"missing"}')

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    @property
    def base_url(self):
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    @property
    def card_url(self):
        return f"{self.base_url}/.well-known/agent-card.json"

    @property
    def interface_url(self):
        return f"{self.base_url}/a2a"

    @property
    def card(self):
        value = {
            "name": "Fake analysis agent",
            "description": "Analyzes supplied material",
            "version": "1.0.0",
            "supportedInterfaces": [
                {
                    "url": self.interface_url,
                    "protocolBinding": "HTTP+JSON",
                    "protocolVersion": "1.0",
                    "tenant": "tenant-a",
                }
            ],
            "capabilities": {},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [
                {
                    "id": "analyze",
                    "name": "Analyze",
                    "description": "Analyze evidence",
                    "tags": ["analysis"],
                    "examples": [],
                    "inputModes": ["text/plain"],
                    "outputModes": ["text/plain"],
                }
            ],
        }
        value.update(self.card_overrides)
        return value

    @property
    def card_bytes(self):
        if self.card_body_override is not None:
            return self.card_body_override
        return json.dumps(
            self.card, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def _record(self, handler, body):
        self.requests.append(
            {
                "method": handler.command,
                "path": handler.path,
                "headers": dict(handler.headers.items()),
                "body": body,
            }
        )

    def _respond(self, handler, status, body, content_type="application/a2a+json"):
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

