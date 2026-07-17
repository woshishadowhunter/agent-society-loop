import json
import os
import subprocess
import sys
import tempfile
import unittest

from agent_society_loop.readiness import run_product_readiness_self_test


class ProductReadinessTests(unittest.TestCase):
    def test_product_readiness_report_passes_with_required_checks(self):
        report = run_product_readiness_self_test("1.1.0")

        self.assertTrue(report["passed"])
        self.assertEqual(report["version"], "1.1.0")
        self.assertEqual(
            [check["name"] for check in report["checks"]],
            [
                "version",
                "docs",
                "deterministic_demo",
                "scheduler_self_test",
                "a2a_reliability_self_test",
            ],
        )
        self.assertTrue(all(check["passed"] for check in report["checks"]))

    def test_product_readiness_fails_when_required_doc_is_missing(self):
        report = run_product_readiness_self_test(
            "1.0.0", required_docs=("docs/does-not-exist.md",)
        )

        self.assertFalse(report["passed"])
        docs = next(check for check in report["checks"] if check["name"] == "docs")
        self.assertFalse(docs["passed"])
        self.assertIn("docs/does-not-exist.md", docs["details"]["missing"])

    def test_product_readiness_finds_docs_from_current_working_directory(self):
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(os.path.join(directory, "docs"))
            with open(
                os.path.join(directory, "docs", "operator.md"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("# Operator docs\n")
            os.chdir(directory)
            try:
                report = run_product_readiness_self_test(
                    "1.0.0", required_docs=("docs/operator.md",)
                )
            finally:
                os.chdir(previous)

        self.assertTrue(report["passed"])

    def test_product_self_test_cli_emits_json(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_society_loop.cli",
                "product",
                "self-test",
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        report = json.loads(completed.stdout)
        self.assertTrue(report["passed"])
        self.assertEqual(report["version"], "1.1.0")
