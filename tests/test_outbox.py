import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agent_society_loop.cli import main
from agent_society_loop.domain import OutboxMessage, OutboxStatus
from agent_society_loop.outbox import (
    OutboxDispatcher,
    OutboxDispatchStatus,
    WebhookOutboxHandler,
)
from agent_society_loop.storage import SQLiteRepository


AT = "2026-07-17T00:00:00+00:00"
LATER = "2026-07-17T00:00:05+00:00"
EXPIRED = "2026-07-17T00:00:11+00:00"


class WebhookHandler(BaseHTTPRequestHandler):
    received = None

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        type(self).received = {
            "body": json.loads(self.rfile.read(length)),
            "idempotency_key": self.headers.get("Idempotency-Key"),
            "delivery_token": self.headers.get("X-Agent-Society-Delivery-Token"),
            "authorization": self.headers.get("Authorization"),
        }
        body = b'{"accepted":true}'
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class SlowWebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = b'{"accepted":true}'
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
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
            time.sleep(0.05)

    def log_message(self, format, *args):
        return


class OutboxDomainTests(unittest.TestCase):
    def test_message_validates_bounded_payload_and_idempotency(self):
        message = OutboxMessage.create(
            "webhook",
            "goal-a:task-a:notify",
            {"event": "task.completed"},
            now=AT,
            max_attempts=2,
        )

        self.assertEqual(message.status, OutboxStatus.PENDING)
        self.assertEqual(message.available_at, AT)
        with self.assertRaisesRegex(ValueError, "payload"):
            OutboxMessage.create("webhook", "large", {"value": "x" * 70_000})
        with self.assertRaisesRegex(ValueError, "idempotency"):
            OutboxMessage.create("webhook", "", {"event": "x"})

    def test_claim_uses_monotonic_delivery_token_and_rejects_stale_completion(self):
        message = OutboxMessage.create(
            "webhook", "delivery-a", {"event": "x"}, now=AT
        )
        first = message.claim("dispatcher-a", now=AT, lease_seconds=10)
        second = first.claim("dispatcher-b", now=EXPIRED, lease_seconds=10)

        self.assertEqual((first.attempt_count, first.delivery_token), (1, 1))
        self.assertEqual((second.attempt_count, second.delivery_token), (2, 2))
        with self.assertRaisesRegex(ValueError, "delivery ownership"):
            second.deliver("dispatcher-a", 1, now=EXPIRED)
        delivered = second.deliver("dispatcher-b", 2, now=EXPIRED)
        self.assertEqual(delivered.status, OutboxStatus.DELIVERED)

    def test_completion_at_exact_lease_expiry_is_stale(self):
        message = OutboxMessage.create(
            "webhook", "expiry-boundary", {"event": "x"}, now=AT
        ).claim("dispatcher-a", now=AT, lease_seconds=10)

        with self.assertRaisesRegex(ValueError, "ownership is stale"):
            message.deliver(
                "dispatcher-a",
                message.delivery_token,
                now="2026-07-17T00:00:10+00:00",
            )

    def test_exact_owner_can_renew_before_expiry(self):
        message = OutboxMessage.create(
            "webhook", "renew-boundary", {"event": "x"}, now=AT
        ).claim("dispatcher-a", now=AT, lease_seconds=10)

        renewed = message.renew(
            "dispatcher-a",
            message.delivery_token,
            now=LATER,
            lease_seconds=10,
        )

        self.assertEqual(
            renewed.claim_expires_at,
            "2026-07-17T00:00:15+00:00",
        )
        with self.assertRaisesRegex(ValueError, "ownership is stale"):
            message.renew(
                "dispatcher-b",
                message.delivery_token,
                now=LATER,
                lease_seconds=10,
            )

    def test_failure_retries_until_attempt_budget_is_exhausted(self):
        message = OutboxMessage.create(
            "webhook", "retry-a", {"event": "x"}, now=AT, max_attempts=2
        )
        first = message.claim("dispatcher-a", now=AT, lease_seconds=10)
        pending = first.fail(
            "dispatcher-a", 1, now=LATER, error="temporary", retry_seconds=5
        )
        second = pending.claim(
            "dispatcher-b", now="2026-07-17T00:00:10+00:00", lease_seconds=10
        )
        failed = second.fail(
            "dispatcher-b", 2, now=EXPIRED, error="still unavailable"
        )

        self.assertEqual(pending.status, OutboxStatus.PENDING)
        self.assertEqual(failed.status, OutboxStatus.FAILED)
        self.assertEqual(failed.last_error, "still unavailable")


class SQLiteOutboxTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "outbox.db"
        self.first = SQLiteRepository(self.path)
        self.second = SQLiteRepository(self.path)

    def tearDown(self):
        self.second.close()
        self.first.close()
        self.directory.cleanup()

    def test_enqueue_is_idempotent_and_rejects_changed_payload(self):
        message = OutboxMessage.create(
            "webhook", "same-key", {"event": "x"}, now=AT
        )

        self.assertEqual(self.first.enqueue_outbox(message), message)
        duplicate = OutboxMessage.create(
            "webhook", "same-key", {"event": "x"}, now=LATER
        )
        self.assertEqual(self.second.enqueue_outbox(duplicate), message)
        changed = OutboxMessage.create(
            "webhook", "same-key", {"event": "changed"}, now=LATER
        )
        with self.assertRaisesRegex(ValueError, "idempotency payload"):
            self.second.enqueue_outbox(changed)

    def test_claim_is_exclusive_and_expired_delivery_is_reclaimed(self):
        self.first.enqueue_outbox(
            OutboxMessage.create("webhook", "claim-key", {"event": "x"}, now=AT)
        )

        first = self.first.claim_outbox(
            "dispatcher-a", now=AT, lease_seconds=10
        )
        self.assertIsNotNone(first)
        self.assertIsNone(
            self.second.claim_outbox(
                "dispatcher-b", now=LATER, lease_seconds=10
            )
        )
        reclaimed = self.second.claim_outbox(
            "dispatcher-b", now=EXPIRED, lease_seconds=10
        )

        self.assertEqual(reclaimed.delivery_token, 2)
        with self.assertRaisesRegex(ValueError, "delivery ownership"):
            self.first.complete_outbox(
                first.message_id,
                "dispatcher-a",
                first.delivery_token,
                now=EXPIRED,
            )
        delivered = self.second.complete_outbox(
            reclaimed.message_id,
            "dispatcher-b",
            reclaimed.delivery_token,
            now=EXPIRED,
        )
        self.assertEqual(delivered.status, OutboxStatus.DELIVERED)

    def test_claim_filters_by_topic(self):
        self.first.enqueue_outbox(
            OutboxMessage.create("audit", "audit-key", {"event": "audit"}, now=AT)
        )
        expected = self.first.enqueue_outbox(
            OutboxMessage.create(
                "webhook", "webhook-key", {"event": "webhook"}, now=LATER
            )
        )

        claimed = self.second.claim_outbox(
            "dispatcher-a",
            topic="webhook",
            now=LATER,
            lease_seconds=10,
        )

        self.assertEqual(claimed.message_id, expected.message_id)
        self.assertEqual(claimed.topic, "webhook")

    def test_expired_delivery_at_attempt_limit_becomes_terminal(self):
        self.first.enqueue_outbox(
            OutboxMessage.create(
                "webhook",
                "crash-key",
                {"event": "x"},
                now=AT,
                max_attempts=1,
            )
        )
        self.first.claim_outbox("dispatcher-a", now=AT, lease_seconds=10)

        reclaimed = self.second.claim_outbox(
            "dispatcher-b", now=EXPIRED, lease_seconds=10
        )

        self.assertIsNone(reclaimed)
        saved = self.second.list_outbox()[0]
        self.assertEqual(saved.status, OutboxStatus.FAILED)
        self.assertIn("lease expired", saved.last_error)

    def test_outbox_list_is_filterable_and_terminal_history_is_purgeable(self):
        delivered = self.first.enqueue_outbox(
            OutboxMessage.create(
                "webhook", "purge-delivered", {"event": "old"}, now=AT
            )
        )
        claim = self.first.claim_outbox(
            "dispatcher-a",
            topic="webhook",
            now=AT,
            lease_seconds=10,
        )
        self.first.complete_outbox(
            delivered.message_id,
            "dispatcher-a",
            claim.delivery_token,
            now=LATER,
        )
        pending = self.first.enqueue_outbox(
            OutboxMessage.create(
                "webhook", "keep-pending", {"event": "new"}, now=LATER
            )
        )

        listed = self.first.list_outbox(
            status=OutboxStatus.PENDING,
            limit=1,
        )
        purged = self.first.purge_outbox(
            before=EXPIRED,
            limit=10,
        )

        self.assertEqual([message.message_id for message in listed], [pending.message_id])
        self.assertEqual(purged, 1)
        self.assertEqual(
            [message.message_id for message in self.first.list_outbox()],
            [pending.message_id],
        )

    def test_dispatcher_records_retry_and_success(self):
        message = self.first.enqueue_outbox(
            OutboxMessage.create(
                "webhook", "dispatch-key", {"event": "x"}, now=AT, max_attempts=2
            )
        )
        times = iter((AT, AT, LATER, LATER))
        self.first.scheduler_now = lambda: next(times)
        dispatcher = OutboxDispatcher(
            self.first,
            "dispatcher-a",
            lease_seconds=10,
            retry_seconds=0,
        )

        failed = dispatcher.run_once(
            lambda topic, payload, key, token: (_ for _ in ()).throw(
                RuntimeError("temporary")
            )
        )
        delivered = dispatcher.run_once(
            lambda topic, payload, key, token: None
        )

        self.assertEqual(failed.status, OutboxDispatchStatus.RETRY)
        self.assertEqual(delivered.status, OutboxDispatchStatus.DELIVERED)
        saved = self.first.list_outbox()
        self.assertEqual(saved[0].message_id, message.message_id)
        self.assertEqual(saved[0].status, OutboxStatus.DELIVERED)

    def test_dispatcher_renews_lease_during_slow_delivery(self):
        now = self.first.scheduler_now()
        self.first.enqueue_outbox(
            OutboxMessage.create(
                "webhook", "slow-dispatch", {"event": "x"}, now=now
            )
        )
        observed = {}
        dispatcher = OutboxDispatcher(
            self.first,
            "dispatcher-a",
            topic="webhook",
            lease_seconds=1,
            retry_seconds=0,
            repository_factory=lambda: SQLiteRepository(self.path),
        )

        def slow_handler(topic, payload, key, token):
            time.sleep(1.1)
            observed["reclaimed"] = self.second.claim_outbox(
                "dispatcher-b",
                topic="webhook",
                now=self.second.scheduler_now(),
                lease_seconds=1,
            )

        result = dispatcher.run_once(slow_handler)

        self.assertEqual(result.status, OutboxDispatchStatus.DELIVERED)
        self.assertIsNone(observed["reclaimed"])

    def test_dispatcher_watch_stops_cleanly(self):
        stop_event = threading.Event()
        stop_event.set()
        dispatcher = OutboxDispatcher(
            self.first,
            "dispatcher-a",
            topic="webhook",
        )

        result = dispatcher.run(
            lambda topic, payload, key, token: None,
            stop_event=stop_event,
            poll_interval_seconds=0.01,
        )

        self.assertEqual(result.status, OutboxDispatchStatus.STOPPED)

    def test_cli_dispatches_and_lists_webhook_with_delivery_identity(self):
        server = HTTPServer(("127.0.0.1", 0), WebhookHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        WebhookHandler.received = None
        thread.start()
        try:
            self.first.enqueue_outbox(
                OutboxMessage.create(
                    "webhook", "cli-key", {"event": "x"}, now=AT
                )
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(
                    [
                        "outbox",
                        "dispatch",
                        "--worker-id",
                        "dispatcher-a",
                        "--webhook-url",
                        f"http://127.0.0.1:{server.server_port}/events",
                        "--allow-insecure-localhost",
                        "--db",
                        str(self.path),
                        "--json",
                    ]
                )

            result = json.loads(stdout.getvalue())
            self.assertEqual((code, stderr.getvalue()), (0, ""))
            self.assertEqual(result["status"], "delivered")
            self.assertEqual(WebhookHandler.received["idempotency_key"], "cli-key")
            self.assertEqual(WebhookHandler.received["delivery_token"], "1")
            self.assertEqual(
                WebhookHandler.received["body"],
                {"payload": {"event": "x"}, "topic": "webhook"},
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    ["outbox", "list", "--db", str(self.path), "--json"]
                )
            messages = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(messages[0]["status"], "delivered")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "outbox",
                        "purge",
                        "--before",
                        "2027-01-01T00:00:00+00:00",
                        "--db",
                        str(self.path),
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue()), {"purged": 1})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_webhook_requires_https_or_explicit_loopback_opt_in(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            WebhookOutboxHandler("http://models.example/events")
        with self.assertRaisesRegex(ValueError, "explicit loopback"):
            WebhookOutboxHandler("http://127.0.0.1:8080/events")
        with self.assertRaisesRegex(ValueError, "credentials"):
            WebhookOutboxHandler("https://user:secret@models.example/events")

    def test_webhook_slow_drip_obeys_wall_clock_deadline(self):
        server = HTTPServer(("127.0.0.1", 0), SlowWebhookHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            handler = WebhookOutboxHandler(
                f"http://127.0.0.1:{server.server_port}/events",
                allow_insecure_localhost=True,
                timeout_seconds=0.15,
            )
            started = time.monotonic()

            with self.assertRaisesRegex(RuntimeError, "deadline"):
                handler("webhook", {"event": "x"}, "deadline-key", 1)

            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_cli_rejects_webhook_lease_without_timeout_safety_margin(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                [
                    "outbox",
                    "dispatch",
                    "--worker-id",
                    "dispatcher-a",
                    "--webhook-url",
                    "http://127.0.0.1:8080/events",
                    "--allow-insecure-localhost",
                    "--lease",
                    "30",
                    "--timeout",
                    "30",
                    "--db",
                    str(self.path),
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("safety margin", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
