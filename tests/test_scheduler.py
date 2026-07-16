import unittest

from agent_society_loop.scheduler import (
    ClaimStatus,
    TaskClaim,
    WorkerSession,
    parse_utc,
    validate_duration,
)


AT = "2026-07-16T00:00:00+00:00"
PLUS_5 = "2026-07-16T00:00:05+00:00"
PLUS_15 = "2026-07-16T00:00:15+00:00"


class SchedulerDomainTests(unittest.TestCase):
    def test_worker_session_and_claim_validate_identity_time_and_transitions(self):
        session = WorkerSession.create(
            "worker-a", "session-a", ("writing", "analysis", "analysis"),
            now=AT, ttl_seconds=30,
        )
        claim = TaskClaim.create(
            "g", "t", session, "agent-a", 1, now=AT, lease_seconds=10
        )

        self.assertEqual(session.capabilities, ("analysis", "writing"))
        self.assertEqual(claim.status, ClaimStatus.ACTIVE)
        renewed = claim.renew(now=PLUS_5, lease_seconds=10)
        self.assertEqual(renewed.expires_at, PLUS_15)
        committed = renewed.finish(ClaimStatus.COMMITTED, now=PLUS_5)
        self.assertEqual(committed.status, ClaimStatus.COMMITTED)
        self.assertEqual(committed.finished_at, PLUS_5)

        with self.assertRaisesRegex(ValueError, "terminal"):
            claim.finish(ClaimStatus.ACTIVE, now=PLUS_5)
        with self.assertRaisesRegex(ValueError, "active"):
            committed.renew(now=PLUS_5, lease_seconds=10)

    def test_scheduler_values_reject_invalid_identifiers_durations_and_times(self):
        with self.assertRaisesRegex(ValueError, "worker_id"):
            WorkerSession.create("bad worker", "session-a", (), now=AT, ttl_seconds=30)
        with self.assertRaisesRegex(ValueError, "capabilities"):
            WorkerSession.create("worker-a", "session-a", ("bad value",), now=AT, ttl_seconds=30)
        with self.assertRaisesRegex(ValueError, "duration"):
            validate_duration(0)
        with self.assertRaisesRegex(ValueError, "duration"):
            validate_duration(86_401)
        with self.assertRaisesRegex(ValueError, "UTC"):
            parse_utc("2026-07-16T00:00:00")

        session = WorkerSession.create(
            "worker-a", "session-a", (), now=AT, ttl_seconds=30
        )
        with self.assertRaisesRegex(ValueError, "fencing_token"):
            TaskClaim.create(
                "g", "t", session, "agent-a", 0, now=AT, lease_seconds=10
            )


if __name__ == "__main__":
    unittest.main()
