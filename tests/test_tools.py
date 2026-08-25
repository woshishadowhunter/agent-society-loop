import unittest

from seed_society.domain import ApprovalStatus, ToolRisk
from seed_society.storage import SQLiteRepository
from seed_society.tools import (
    ApprovalRequired,
    DefaultToolPolicy,
    ToolContext,
    ToolDenied,
    ToolExecutor,
    ToolRegistry,
)
from seed_society.tracing import TraceRecorder


class EchoTool:
    name = "echo"
    description = "Return the supplied text"
    input_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }

    def __init__(self, risk=ToolRisk.READ, *, fail=False):
        self.risk = risk
        self.fail = fail
        self.calls = 0

    def invoke(self, arguments):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private tool detail")
        return arguments["text"]


class ToolRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.context = ToolContext("goal", "task", "agent")

    def tearDown(self):
        self.repository.close()

    def executor(self, tool):
        return ToolExecutor(
            ToolRegistry([tool]),
            DefaultToolPolicy(),
            self.repository,
            TraceRecorder(self.repository),
        )

    def test_read_tool_executes_without_approval(self):
        tool = EchoTool()

        result = self.executor(tool).execute("echo", {"text": "hello"}, self.context)

        self.assertTrue(result.ok)
        self.assertEqual(result.output, "hello")
        self.assertEqual(tool.calls, 1)
        self.assertEqual(self.repository.list_approvals("goal"), [])
        self.assertEqual(self.repository.list_spans("goal")[0].kind, "tool")

    def test_write_tool_creates_durable_approval_before_execution(self):
        tool = EchoTool(ToolRisk.WRITE)
        executor = self.executor(tool)

        with self.assertRaises(ApprovalRequired) as caught:
            executor.execute("echo", {"text": "hello"}, self.context)

        saved = self.repository.get_approval(caught.exception.approval.approval_id)
        self.assertEqual(saved.status, ApprovalStatus.PENDING)
        self.assertEqual(tool.calls, 0)

    def test_approval_record_redacts_sensitive_arguments(self):
        class SecretTool(EchoTool):
            name = "secret_echo"
            input_schema = {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "api_token": {"type": "string"},
                },
                "required": ["text", "api_token"],
                "additionalProperties": False,
            }

        executor = self.executor(SecretTool(ToolRisk.WRITE))

        with self.assertRaises(ApprovalRequired) as caught:
            executor.execute(
                "secret_echo",
                {"text": "hello", "api_token": "private-token"},
                self.context,
            )

        self.assertEqual(caught.exception.approval.arguments["api_token"], "[REDACTED]")
        self.assertNotIn(
            "private-token",
            str(self.repository.get_approval(caught.exception.approval.approval_id)),
        )

    def test_approved_write_executes_and_rejected_write_is_denied(self):
        approved_tool = EchoTool(ToolRisk.WRITE)
        approved_executor = self.executor(approved_tool)
        with self.assertRaises(ApprovalRequired) as caught:
            approved_executor.execute("echo", {"text": "approved"}, self.context)
        self.repository.save_approval(
            caught.exception.approval.resolve(ApprovalStatus.APPROVED, "operator")
        )

        result = approved_executor.execute(
            "echo", {"text": "approved"}, self.context
        )

        self.assertEqual(result.output, "approved")
        self.assertEqual(approved_tool.calls, 1)

        rejected_tool = EchoTool(ToolRisk.WRITE)
        rejected_executor = self.executor(rejected_tool)
        rejected_context = ToolContext("goal", "other-task", "agent")
        with self.assertRaises(ApprovalRequired) as rejected:
            rejected_executor.execute(
                "echo", {"text": "rejected"}, rejected_context
            )
        self.repository.save_approval(
            rejected.exception.approval.resolve(ApprovalStatus.REJECTED, "operator")
        )

        with self.assertRaisesRegex(ToolDenied, "rejected"):
            rejected_executor.execute(
                "echo", {"text": "rejected"}, rejected_context
            )
        self.assertEqual(rejected_tool.calls, 0)

    def test_unknown_and_schema_invalid_calls_are_denied(self):
        executor = self.executor(EchoTool())

        with self.assertRaisesRegex(ToolDenied, "unknown tool"):
            executor.execute("missing", {}, self.context)
        with self.assertRaisesRegex(ToolDenied, "required.*text"):
            executor.execute("echo", {}, self.context)
        with self.assertRaisesRegex(ToolDenied, "unexpected.*extra"):
            executor.execute(
                "echo", {"text": "hello", "extra": True}, self.context
            )

    def test_schema_enum_rejects_values_outside_registered_options(self):
        tool = EchoTool()
        tool.input_schema = {
            "type": "object",
            "properties": {"text": {"type": "string", "enum": ["allowed"]}},
            "required": ["text"],
            "additionalProperties": False,
        }

        with self.assertRaisesRegex(ToolDenied, "allowed values"):
            self.executor(tool).execute("echo", {"text": "other"}, self.context)

    def test_duplicate_registration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate tool"):
            ToolRegistry([EchoTool(), EchoTool()])

    def test_unknown_risk_classification_is_rejected(self):
        tool = EchoTool()
        tool.risk = "custom"

        with self.assertRaisesRegex(ValueError, "risk"):
            ToolRegistry([tool])

    def test_tool_exception_is_returned_without_private_detail(self):
        result = self.executor(EchoTool(fail=True)).execute(
            "echo", {"text": "hello"}, self.context
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "RuntimeError: tool execution failed")
        self.assertNotIn("private", result.error)


if __name__ == "__main__":
    unittest.main()
