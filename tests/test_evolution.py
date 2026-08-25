import unittest

from seed_society.domain import (
    AgentGenome,
    AgentSelfModel,
    Artifact,
    ExperienceRecord,
    Goal,
    Review,
    Task,
    Verdict,
)
from seed_society.evolution import GenomeRecombiner
from seed_society.storage import SQLiteRepository


class GenomeRecombinationTests(unittest.TestCase):
    def test_recombine_creates_candidate_from_parent_genomes_and_experience(self):
        repository = SQLiteRepository(":memory:")
        try:
            first = AgentGenome.create(
                "analyst-a",
                base_model="local-qwen",
                role_seed="Evidence analyst",
                self_model=AgentSelfModel.create(
                    mission="Analyze with evidence",
                    success_signals=("cites sources",),
                    failure_modes=("unsupported claim",),
                ),
                traits=("careful", "structured"),
                tool_profile=("search", "sqlite", "filesystem"),
                risk_policy="read_only",
                generation=2,
            )
            second = AgentGenome.create(
                "analyst-b",
                base_model="local-qwen",
                role_seed="Practical analyst",
                self_model=AgentSelfModel.create(
                    mission="Make practical recommendations",
                    success_signals=("clear recommendation",),
                    failure_modes=("vague action",),
                ),
                traits=("practical", "structured"),
                tool_profile=("search", "sqlite"),
                risk_policy="approval_required",
                generation=3,
            )
            repository.save_agent_genome(first)
            repository.save_agent_genome(second)
            goal = Goal.create("Market", "Analyze market", goal_id="goal")
            task = Task.create("goal", "analysis", "market_analysis", "Analyze")
            repository.save_goal(goal)
            repository.save_task(task)
            pass_review = Review.create(
                "goal",
                "analysis",
                1,
                Verdict.PASS,
                91,
                (),
                "Evidence-backed recommendation",
            )
            fail_review = Review.create(
                "goal",
                "analysis",
                2,
                Verdict.FAIL,
                55,
                (),
                "Missed competitor risk",
            )
            repository.save_experience(
                ExperienceRecord.create(
                    task,
                    pass_review,
                    Artifact.create("goal", "analysis", "analyst-a", "Strong report"),
                    "analyst-a",
                    lessons=("Lead with cited evidence before recommendation",),
                    tags=("pattern:success",),
                )
            )
            repository.save_experience(
                ExperienceRecord.create(
                    task,
                    fail_review,
                    Artifact.create("goal", "analysis", "analyst-b", "Weak report"),
                    "analyst-b",
                    lessons=("Check competitor risk before final answer",),
                    tags=("verdict:fail",),
                )
            )

            report = GenomeRecombiner(repository).recombine(
                "analyst-child",
                ("analyst-b", "analyst-a"),
                task_type="market_analysis",
            )

            child = repository.get_agent_genome("analyst-child")
            self.assertIsNotNone(child)
            self.assertEqual(report.child, child)
            self.assertEqual(child.parents, ("analyst-a", "analyst-b"))
            self.assertEqual(child.generation, 4)
            self.assertEqual(child.risk_policy, "approval_required")
            self.assertEqual(child.tool_profile, ("search", "sqlite"))
            self.assertIn("structured", child.traits)
            self.assertIn(
                "Lead with cited evidence before recommendation",
                child.self_model.success_signals,
            )
            self.assertIn(
                "Check competitor risk before final answer",
                child.self_model.failure_modes,
            )
            self.assertEqual(report.task_type, "market_analysis")
            self.assertEqual(report.parents, ("analyst-a", "analyst-b"))
            self.assertIn("approval_required", report.safety_notes[0])
        finally:
            repository.close()

    def test_recombine_rejects_missing_parent_and_existing_child(self):
        repository = SQLiteRepository(":memory:")
        try:
            repository.save_agent_genome(
                AgentGenome.create(
                    "parent-a",
                    base_model="local-qwen",
                    role_seed="Analyst A",
                    traits=("careful",),
                )
            )
            repository.save_agent_genome(
                AgentGenome.create(
                    "child",
                    base_model="local-qwen",
                    role_seed="Existing child",
                )
            )

            with self.assertRaisesRegex(KeyError, "parent genome not found"):
                GenomeRecombiner(repository).recombine(
                    "new-child",
                    ("parent-a", "missing-parent"),
                    task_type="analysis",
                )
            with self.assertRaisesRegex(ValueError, "already exists"):
                GenomeRecombiner(repository).recombine(
                    "child",
                    ("parent-a", "missing-parent"),
                    task_type="analysis",
                )
        finally:
            repository.close()


if __name__ == "__main__":
    unittest.main()
