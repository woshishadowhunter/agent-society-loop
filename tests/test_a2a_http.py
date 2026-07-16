import hashlib
import json
import unittest

from agent_society_loop.a2a import (
    A2AAmbiguousSubmission,
    A2AHTTPClient,
    A2AHTTPError,
    A2ALimits,
    A2AProtocolError,
    register_remote_agent,
)
from agent_society_loop.storage import SQLiteRepository
from tests.a2a_fake_server import FakeA2AServer


class A2AHTTPClientTests(unittest.TestCase):
    def client(self, **kwargs):
        return A2AHTTPClient(allow_insecure_localhost=True, **kwargs)

    def test_inspects_card_and_computes_raw_digest(self):
        with FakeA2AServer() as server:
            inspection = self.client().inspect_card(server.card_url)

        self.assertEqual(
            inspection.sha256, hashlib.sha256(server.card_bytes).hexdigest()
        )
        self.assertEqual(inspection.card["name"], "Fake analysis agent")
        self.assertEqual(inspection.byte_count, len(server.card_bytes))
        self.assertEqual(server.requests[0]["headers"]["A2A-Version"], "1.0")

    def test_rejects_redirect_oversize_content_type_and_malformed_json(self):
        with FakeA2AServer() as server:
            client = self.client(limits=A2ALimits(max_response_bytes=512))
            with self.assertRaisesRegex(A2AHTTPError, "redirect"):
                client.inspect_card(f"{server.base_url}/redirect")
            with self.assertRaisesRegex(A2AProtocolError, "size"):
                client.inspect_card(f"{server.base_url}/large")
            with self.assertRaisesRegex(A2AProtocolError, "content type"):
                client.inspect_card(f"{server.base_url}/bad-type")
            server.card_body_override = b"not-json"
            with self.assertRaisesRegex(A2AProtocolError, "JSON"):
                client.inspect_card(server.card_url)

    def test_rejects_insecure_non_loopback_and_url_credentials(self):
        client = A2AHTTPClient()

        with self.assertRaisesRegex(ValueError, "HTTPS"):
            client.inspect_card("http://example.com/.well-known/agent-card.json")
        with self.assertRaisesRegex(ValueError, "credentials"):
            client.inspect_card(
                "https://user:secret@example.com/.well-known/agent-card.json"
            )

    def test_send_uses_a2a_headers_tenant_and_bearer(self):
        with FakeA2AServer() as server:
            response = self.client(auth_token="private-token").send_message(
                server.interface_url,
                {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "hello"}]}},
                tenant="tenant-a",
            )

        request = server.requests[-1]
        body = json.loads(request["body"])
        self.assertIn("message", response)
        self.assertEqual(body["tenant"], "tenant-a")
        self.assertEqual(request["headers"]["A2A-Version"], "1.0")
        self.assertEqual(request["headers"]["Authorization"], "Bearer private-token")
        self.assertEqual(request["headers"]["Content-Type"], "application/a2a+json")

    def test_send_server_failure_is_ambiguous_and_sanitized(self):
        with FakeA2AServer() as server:
            server.send_status = 503
            server.send_body_override = b'{"error":"private-token remote detail"}'
            with self.assertRaisesRegex(
                A2AAmbiguousSubmission, "ambiguous"
            ) as caught:
                self.client(auth_token="private-token").send_message(
                    server.interface_url,
                    {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "hello"}]}},
                )

        self.assertNotIn("private-token", str(caught.exception))
        self.assertNotIn("remote detail", str(caught.exception))


class A2ARegistrationTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")

    def tearDown(self):
        self.repository.close()

    def register(self, server, **kwargs):
        return register_remote_agent(
            self.repository,
            A2AHTTPClient(allow_insecure_localhost=True),
            "remote-a",
            server.card_url,
            hashlib.sha256(server.card_bytes).hexdigest(),
            server.interface_url,
            {"analysis": "analyze"},
            auth_env="A2A_TOKEN",
            allow_insecure_localhost=True,
            **kwargs,
        )

    def test_registers_exact_card_interface_skills_and_profile_atomically(self):
        with FakeA2AServer() as server:
            registration = self.register(server)

        profile = self.repository.get_agent("remote-a")
        self.assertEqual(registration.tenant, "tenant-a")
        self.assertEqual(registration.model_id, profile.model_id)
        self.assertEqual(profile.execution_kind, "a2a")
        self.assertEqual(profile.task_types, ("analysis",))

    def test_rejects_wrong_digest_interface_binding_version_and_skill(self):
        with FakeA2AServer() as server:
            cases = [
                ("0" * 64, server.interface_url, {}, "digest"),
                (None, f"{server.base_url}/other", {}, "interface"),
                (None, server.interface_url, {"supportedInterfaces": [{"url": server.interface_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}]}, "interface"),
                (None, server.interface_url, {"supportedInterfaces": [{"url": server.interface_url, "protocolBinding": "HTTP+JSON", "protocolVersion": "0.3"}]}, "interface"),
                (None, server.interface_url, {"skills": []}, "skill"),
            ]
            for digest, interface, overrides, pattern in cases:
                with self.subTest(pattern=pattern):
                    server.card_overrides = overrides
                    expected = digest or hashlib.sha256(server.card_bytes).hexdigest()
                    with self.assertRaisesRegex(ValueError, pattern):
                        register_remote_agent(
                            self.repository,
                            A2AHTTPClient(allow_insecure_localhost=True),
                            f"remote-{len(self.repository.list_remote_agents())}",
                            server.card_url,
                            expected,
                            interface,
                            {"analysis": "analyze"},
                            allow_insecure_localhost=True,
                        )


if __name__ == "__main__":
    unittest.main()
