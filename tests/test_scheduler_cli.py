import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from seed_society.cli import build_parser, main
from seed_society.domain import Goal, GoalStatus, Task
from seed_society.scheduler import WorkerSession
from seed_society.storage import SQLiteRepository


AT = "2026-07-16T00:00:00+00:00"


class SchedulerCLITests(unittest.TestCase):
    def run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_parser_exposes_scheduler_operations(self):
        parser = build_parser()

        self.assertEqual(
            parser.parse_args(["scheduler", "workers"]).scheduler_command,
            "workers",
        )
        self.assertEqual(
            parser.parse_args(["scheduler", "claims", "--goal-id", "g"]).goal_id,
            "g",
        )
        self.assertEqual(
            parser.parse_args(["scheduler", "reap", "--at", AT]).at,
            AT,
        )
        self.assertEqual(
            parser.parse_args(["scheduler", "self-test"]).scheduler_command,
            "self-test",
        )

    def test_scheduler_self_test_reports_five_passing_checks(self):
        code, output, error = self.run_cli(["scheduler", "self-test", "--json"])

        payload = json.loads(output)
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(payload["passed"])
        self.assertEqual(
            [item["name"] for item in payload["checks"]],
            [
                "exclusive_claim",
                "lease_renewal",
                "monotonic_reclaim",
                "stale_commit_rejected",
                "current_commit",
            ],
        )
        self.assertTrue(all(item["passed"] for item in payload["checks"]))

    def test_workers_claims_and_reap_are_json_inspectable(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "scheduler.db")
            repository = SQLiteRepository(database)
            goal = replace(
                Goal.create("Schedule", "Inspect", goal_id="g"),
                status=GoalStatus.RUNNING,
            )
            repository.save_goal(goal)
            repository.save_task(Task.create("g", "t", "analysis", "Analyze"))
            repository.register_worker(
                WorkerSession.create(
                    "worker-a", "session-a", ("analysis",), now=AT, ttl_seconds=30
                )
            )
            repository.claim_task(
                "g", "t", "worker-a", "session-a", "agent-a",
                now=AT, lease_seconds=10,
            )
            repository.close()

            code, output, error = self.run_cli(
                ["scheduler", "workers", "--at", AT, "--db", database, "--json"]
            )
            workers = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertFalse(workers[0]["expired"])

            code, output, _ = self.run_cli(
                ["scheduler", "claims", "--goal-id", "g", "--db", database, "--json"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)[0]["fencing_token"], 1)

            code, output, _ = self.run_cli(
                [
                    "scheduler", "reap", "--at", "2026-07-16T00:00:10+00:00",
                    "--db", database, "--json",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)[0]["status"], "expired")

    def test_scheduler_rejects_non_utc_inspection_time(self):
        code, _, error = self.run_cli(
            ["scheduler", "workers", "--at", "2026-07-16T00:00:00", "--json"]
        )

        self.assertEqual(code, 2)
        self.assertIn("UTC", error)


if __name__ == "__main__":
    unittest.main()
