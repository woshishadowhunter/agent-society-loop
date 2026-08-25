"""Product-level readiness checks for installed Seed Society runtimes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .a2a_reliability import run_reliability_campaign
from .deterministic import build_demo_engine
from .scheduler import run_scheduler_self_test
from .storage import SQLiteRepository


DEFAULT_REQUIRED_DOCS = (
    "README.md",
    "README.zh-CN.md",
    "docs/architecture.md",
    "docs/deployment.md",
    "docs/production-runbook.md",
    "docs/release-checklist.md",
    "SECURITY.md",
)


def run_product_readiness_self_test(
    version: str,
    *,
    required_docs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run deterministic install-time checks that prove the product can operate."""

    checks = [
        _version_check(version),
        _docs_check(required_docs or DEFAULT_REQUIRED_DOCS),
        _demo_check(),
        _scheduler_check(),
        _a2a_reliability_check(),
    ]
    return {
        "product": "seed-society",
        "version": version,
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


def _version_check(version: str) -> dict[str, Any]:
    return {
        "name": "version",
        "passed": bool(version and version[0].isdigit()),
        "details": {"version": version},
    }


def _docs_check(required_docs: Sequence[str]) -> dict[str, Any]:
    roots = (Path(__file__).resolve().parents[2], Path.cwd())
    missing = [
        document
        for document in required_docs
        if not any((root / document).is_file() for root in roots)
    ]
    return {
        "name": "docs",
        "passed": not missing,
        "details": {"required": list(required_docs), "missing": missing},
    }


def _demo_check() -> dict[str, Any]:
    repository = SQLiteRepository(":memory:")
    try:
        engine = build_demo_engine(repository)
        goal = engine.create_goal(
            "Quantum Coffee Mug Launch",
            "Create market, visual, copy, and integrated launch materials",
            goal_id="product-readiness-demo",
        )
        report = engine.resume(goal.goal_id)
        passed = (
            report.status.value == "succeeded"
            and report.tasks_total == 4
            and report.tasks_succeeded == 4
            and report.retries >= 1
        )
        return {
            "name": "deterministic_demo",
            "passed": passed,
            "details": {
                "goal_id": report.goal_id,
                "status": report.status.value,
                "tasks_total": report.tasks_total,
                "tasks_succeeded": report.tasks_succeeded,
                "retries": report.retries,
            },
        }
    finally:
        repository.close()


def _scheduler_check() -> dict[str, Any]:
    report = run_scheduler_self_test()
    return {
        "name": "scheduler_self_test",
        "passed": bool(report["passed"]),
        "details": {"checks": report["checks"]},
    }


def _a2a_reliability_check() -> dict[str, Any]:
    report = run_reliability_campaign().to_dict()
    return {
        "name": "a2a_reliability_self_test",
        "passed": bool(report["passed"]),
        "details": {"checks": report["checks"]},
    }
