import importlib.util
import os
import unittest
from uuid import uuid4

from agent_society_loop.postgres_storage import PostgreSQLRepository
from tests.scheduler_conformance import (
    ApprovalPauseContract,
    ClaimNextTaskContract,
    OwnershipConformanceContract,
    OutcomeReconciliationContract,
)


POSTGRES_URL = os.environ.get("SCHEDULER_POSTGRES_URL", "")


class PostgreSQLDependencyTests(unittest.TestCase):
    @unittest.skipIf(importlib.util.find_spec("psycopg") is not None, "psycopg installed")
    def test_default_install_reports_the_postgres_extra(self):
        with self.assertRaisesRegex(RuntimeError, r"postgres.*extra"):
            PostgreSQLRepository("postgresql://unused")


@unittest.skipUnless(POSTGRES_URL, "SCHEDULER_POSTGRES_URL is not configured")
class PostgreSQLContractBase:
    def setUp(self):
        self.schema = f"test_{uuid4().hex}"
        self.first = PostgreSQLRepository(POSTGRES_URL, schema=self.schema)
        self.second = PostgreSQLRepository(POSTGRES_URL, schema=self.schema)

    def tearDown(self):
        self.second.close()
        self.first.drop_schema()
        self.first.close()


class PostgreSQLClaimNextTaskTests(
    PostgreSQLContractBase, ClaimNextTaskContract, unittest.TestCase
):
    pass


class PostgreSQLApprovalPauseTests(
    PostgreSQLContractBase, ApprovalPauseContract, unittest.TestCase
):
    pass


class PostgreSQLOutcomeReconciliationTests(
    PostgreSQLContractBase, OutcomeReconciliationContract, unittest.TestCase
):
    pass


class PostgreSQLOwnershipConformanceTests(
    PostgreSQLContractBase, OwnershipConformanceContract, unittest.TestCase
):
    pass


if __name__ == "__main__":
    unittest.main()
