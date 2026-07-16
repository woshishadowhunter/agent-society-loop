import unittest

from agent_society_loop.domain import SpanStatus
from agent_society_loop.storage import SQLiteRepository
from agent_society_loop.tracing import TraceRecorder


class TraceRecorderTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.recorder = TraceRecorder(self.repository)

    def tearDown(self):
        self.repository.close()

    def test_span_records_success_and_redacts_sensitive_attributes(self):
        with self.recorder.span(
            "g",
            "model.complete",
            kind="model",
            task_id="t",
            agent_id="a",
            attributes={"api_key": "secret", "model": "model-a"},
        ) as span:
            self.assertEqual(span.status, SpanStatus.RUNNING)

        saved = self.repository.list_spans("g")[0]
        self.assertEqual(saved.status, SpanStatus.OK)
        self.assertEqual(saved.attributes["api_key"], "[REDACTED]")
        self.assertEqual(saved.attributes["model"], "model-a")
        self.assertGreaterEqual(saved.duration_ms, 0)

    def test_span_records_error_category_and_reraises(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with self.recorder.span("g", "tool.call", kind="tool"):
                raise RuntimeError("boom")

        saved = self.repository.list_spans("g")[0]
        self.assertEqual(saved.status, SpanStatus.ERROR)
        self.assertEqual(saved.error_category, "RuntimeError")
        self.assertNotIn("boom", str(saved.attributes))

    def test_child_span_preserves_parent_identity(self):
        with self.recorder.span("g", "goal.run", kind="engine") as parent:
            with self.recorder.span(
                "g",
                "worker.execute",
                kind="agent",
                parent_span_id=parent.span_id,
            ):
                pass

        spans = self.repository.list_spans("g")
        self.assertEqual(spans[0].parent_span_id, parent.span_id)
        self.assertIsNone(spans[1].parent_span_id)


if __name__ == "__main__":
    unittest.main()
