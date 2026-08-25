"""Reproducible offline agents and the bundled product-launch scenario."""

from __future__ import annotations

import re
from typing import Any, Sequence

from .domain import AgentProfile, Defect, Goal, Review, RunBudget, Task, Verdict
from .engine import LoopEngine
from .memory import MemoryManager
from .selection import PerformanceWeightedSelector
from .storage import SQLiteRepository


class QuantumMugPlanner:
    def plan(self, goal: Goal, context: dict[str, Any]) -> Sequence[Task]:
        return [
            Task.create(
                goal.goal_id,
                "market",
                "market_analysis",
                "Analyze the smart drinkware market and direct competitors",
                acceptance_criteria={
                    "required_terms": ["competitor", "market growth"],
                    "required_sections": ["Market Signals", "SWOT"],
                    "min_sources": 3,
                    "min_length": 220,
                },
                position=1,
            ),
            Task.create(
                goal.goal_id,
                "visual",
                "visual_concept",
                "Define a visual concept for the quantum coffee mug",
                acceptance_criteria={
                    "required_terms": ["blue", "white", "quantum"],
                    "min_length": 120,
                },
                position=2,
            ),
            Task.create(
                goal.goal_id,
                "copy",
                "launch_copy",
                "Write accessible launch copy focused on the main benefit",
                acceptance_criteria={
                    "required_terms": ["quantum coffee mug", "instant heating"],
                    "min_length": 140,
                },
                dependencies=("market",),
                position=3,
            ),
            Task.create(
                goal.goal_id,
                "integration",
                "integration",
                "Combine the approved materials into one launch package",
                acceptance_criteria={
                    "required_sections": [
                        "Market Analysis",
                        "Visual Concept",
                        "Launch Copy",
                    ],
                    "min_length": 400,
                },
                dependencies=("market", "visual", "copy"),
                position=4,
            ),
        ]


class TemplateWorker:
    """A deterministic specialist that demonstrates feedback-based repair."""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id

    def execute(self, task: Task, context: dict[str, Any]) -> str:
        if task.task_type == "market_analysis":
            third_source = "\n[Source: Retail Lab 2026] Buyers value transparent safety data." if context.get("review_feedback") else ""
            return (
                "## Market Signals\n"
                "Smart drinkware shows steady market growth as hybrid workers seek "
                "reliable temperature control. The leading competitor set emphasizes "
                "battery life, while interviews reveal demand for speed and safe daily use.\n"
                "[Source: Connected Home Index 2026] Category demand rose across urban buyers.\n"
                "[Source: Beverage Hardware Survey 2026] Heating speed leads purchase intent."
                f"{third_source}\n"
                "## SWOT\nStrength: instant utility. Weakness: education is required. "
                "Opportunity: office gifting. Threat: established competitor trust."
            )
        if task.task_type == "visual_concept":
            return (
                "Use a precise blue and white system with generous contrast. A subtle quantum "
                "particle motif frames the real mug without obscuring its controls. Product "
                "photography remains clear, accessible, and suitable for retail and social media."
            )
        if task.task_type == "launch_copy":
            evidence = context.get("dependency_artifacts", {}).get("market", "")
            return (
                "Meet the Quantum Coffee Mug: instant heating for the drink you actually want, "
                "when you want it. Clear controls and a safety-first design turn a rushed morning "
                "into a reliable routine. Market evidence supports a simple promise: less waiting, "
                f"more focus. Evidence used: {evidence[:60]}"
            )
        if task.task_type == "integration":
            artifacts = context.get("dependency_artifacts", {})
            return (
                "## Market Analysis\n" + artifacts.get("market", "") + "\n\n"
                "## Visual Concept\n" + artifacts.get("visual", "") + "\n\n"
                "## Launch Copy\n" + artifacts.get("copy", "")
            )
        raise ValueError(f"unsupported deterministic task type: {task.task_type}")


class CriteriaReviewer:
    """Evaluate the structured criteria used by deterministic and test scenarios."""

    def review(self, task: Task, artifact: str, attempt_no: int) -> Review:
        criteria = task.acceptance_criteria
        defects: list[Defect] = []
        folded = artifact.casefold()

        missing_terms = [
            term for term in criteria.get("required_terms", []) if term.casefold() not in folded
        ]
        if missing_terms:
            defects.append(
                Defect(
                    "content",
                    f"missing required terms: {', '.join(missing_terms)}",
                    "include every required term in meaningful context",
                )
            )

        missing_sections = [
            section
            for section in criteria.get("required_sections", [])
            if f"## {section}".casefold() not in folded
        ]
        if missing_sections:
            defects.append(
                Defect(
                    "structure",
                    f"missing sections: {', '.join(missing_sections)}",
                    "add every required markdown section",
                )
            )

        source_count = len(re.findall(r"\[source:", folded))
        minimum_sources = int(criteria.get("min_sources", 0))
        if source_count < minimum_sources:
            defects.append(
                Defect(
                    "evidence",
                    f"found {source_count} sources; requires {minimum_sources}",
                    f"add {minimum_sources - source_count} relevant source citation(s)",
                )
            )

        minimum_length = int(criteria.get("min_length", 0))
        if len(artifact) < minimum_length:
            defects.append(
                Defect(
                    "content",
                    f"content length {len(artifact)} is below {minimum_length}",
                    "add concise supporting detail",
                )
            )

        score = max(0.0, 100.0 - 20.0 * len(defects))
        verdict = Verdict.PASS if not defects else Verdict.FAIL
        summary = "All acceptance criteria met" if not defects else "; ".join(
            defect.issue for defect in defects
        )
        return Review.create(
            task.goal_id, task.task_id, attempt_no, verdict, score, defects, summary
        )


def build_demo_engine(
    repository: SQLiteRepository, *, budget: RunBudget | None = None
) -> LoopEngine:
    profiles = [
        AgentProfile("market-analyst", "worker", "deterministic-v1", ("market_analysis",)),
        AgentProfile("visual-designer", "worker", "deterministic-v1", ("visual_concept",)),
        AgentProfile("launch-copywriter", "worker", "deterministic-v1", ("launch_copy",)),
        AgentProfile("material-integrator", "worker", "deterministic-v1", ("integration",)),
    ]
    for profile in profiles:
        repository.save_agent(profile)
    workers = {profile.agent_id: TemplateWorker(profile.agent_id) for profile in profiles}
    memory = MemoryManager(repository)
    if not repository.list_knowledge():
        memory.add_knowledge(
            "Brand voice",
            "Use plain language, concrete benefits, and verifiable claims.",
            ("launch_copy", "integration"),
        )
        memory.add_knowledge(
            "Product facts",
            "The concept centers on instant heating, clear controls, and safe daily use.",
            ("market_analysis", "launch_copy"),
        )
    return LoopEngine(
        planner=QuantumMugPlanner(),
        workers=workers,
        reviewer=CriteriaReviewer(),
        repository=repository,
        memory=memory,
        selector=PerformanceWeightedSelector(),
        budget=budget,
    )

