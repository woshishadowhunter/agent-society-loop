import json
import unittest

from agent_society_loop.a2a import A2ALimits
from agent_society_loop.a2a_reliability import run_reliability_campaign


class BrokenClient:
    limits = A2ALimits(poll_interval=0)

    def send_message(self, interface_url, payload, *, tenant=""):
        raise RuntimeError("secret-token should never be reported")


class A2AReliabilityTests(unittest.TestCase):
    def test_default_campaign_proves_five_runtime_invariants(self):
        report = run_reliability_campaign()

        self.assertTrue(report.passed)
        self.assertEqual(
            tuple(check.check_id for check in report.checks),
            (
                "ambiguous_submission",
                "task_identity_drift",
                "remote_interruption",
                "unsupported_output",
                "poll_exhaustion",
            ),
        )
        self.assertTrue(all(check.passed for check in report.checks))
        evidence = {check.check_id: check.evidence for check in report.checks}
        self.assertEqual(evidence["ambiguous_submission"]["send_count"], 1)
        self.assertEqual(evidence["ambiguous_submission"]["status"], "unknown")
        self.assertEqual(evidence["task_identity_drift"]["status"], "failed")
        self.assertEqual(evidence["remote_interruption"]["status"], "interrupted")
        self.assertEqual(evidence["remote_interruption"]["interruption_count"], 2)
        self.assertEqual(evidence["unsupported_output"]["rejected_count"], 2)
        self.assertEqual(evidence["poll_exhaustion"]["cancel_count"], 1)
        self.assertEqual(evidence["poll_exhaustion"]["status"], "canceled")

    def test_broken_injected_client_becomes_sanitized_failed_check(self):
        report = run_reliability_campaign(
            clients={"ambiguous_submission": BrokenClient()}
        )

        check = report.checks[0]
        rendered = json.dumps(report.to_dict())
        self.assertFalse(report.passed)
        self.assertFalse(check.passed)
        self.assertIn("RuntimeError", check.summary)
        self.assertNotIn("secret-token", rendered)


if __name__ == "__main__":
    unittest.main()
