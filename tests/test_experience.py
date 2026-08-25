import unittest

from seed_society.domain import (
    AgentGenome,
    AgentSelfModel,
    Artifact,
    Defect,
    ExperienceRecord,
    Goal,
    Review,
    Task,
    Verdict,
)
from seed_society.experience import ExperienceDistiller
from seed_society.memory import MemoryManager
from seed_society.storage import SQLiteRepository


class AgentGenomeExperienceTests(unittest.TestCase):
    def test_genome_create_normalizes_seed_and_self_model(self):
        genome = AgentGenome.create(
            " analyst ",
            base_model="local-qwen",
            role_seed="Evidence-first analyst",
            self_model=AgentSelfModel.create(
                mission="Produce verified analysis",
                success_signals=("passes review", "uses evidence"),
                failure_modes=("unsupported claim",),
            ),
            traits=("evidence", "careful"),
            tool_profile=("search", "docs"),
            memory_profile={"retrieval_tags": ["analysis"], "feedback_weight": 0.7},
            risk_policy="read_only",
            parents=("researcher-v1",),
            generation=2,
        )

        self.assertEqual(genome.agent_id, "analyst")
        self.assertEqual(genome.self_model.mission, "Produce verified analysis")
        self.assertEqual(genome.traits, ("careful", "evidence"))
        self.assertEqual(genome.tool_profile, ("docs", "search"))
        self.assertEqual(genome.parents, ("researcher-v1",))
        self.assertEqual(genome.generation, 2)

    def test_genome_rejects_unsafe_identity_and_policy(self):
        with self.assertRaisesRegex(ValueError, "agent ID"):
            AgentGenome.create("", base_model="m", role_seed="seed")
        with self.assertRaisesRegex(ValueError, "risk policy"):
            AgentGenome.create("agent", base_model="m", role_seed="seed", risk_policy="root")

    def test_genome_round_trips_through_sqlite(self):
        repository = SQLiteRepository(":memory:")
        try:
            genome = AgentGenome.create(
                "writer",
                base_model="local-model",
                role_seed="Clear writer",
                self_model=AgentSelfModel.create(
                    mission="Write clearly",
                    success_signals=("clear structure",),
                    failure_modes=("vague claim",),
                ),
                traits=("clear",),
            )

            repository.save_agent_genome(genome)

            self.assertEqual(repository.get_agent_genome("writer"), genome)
            self.assertEqual(repository.list_agent_genomes(), [genome])
        finally:
            repository.close()

    def test_experience_record_is_deterministic_and_idempotent(self):
        repository = SQLiteRepository(":memory:")
        try:
            task = Task.create("goal", "draft", "writing", "Draft the note")
            review = Review.create(
                "goal",
                "draft",
                1,
                Verdict.FAIL,
                60,
                [],
                "Missing evidence",
            )
            artifact = Artifact.create("goal", "draft", "writer", "A weak draft")
            first = ExperienceRecord.create(
                task,
                review,
                artifact,
                "writer",
                lessons=("Add source-backed evidence",),
                tags=("defect:evidence",),
            )
            second = ExperienceRecord.create(
                task,
                review,
                artifact,
                "writer",
                lessons=("Add source-backed evidence",),
                tags=("defect:evidence",),
            )

            repository.save_experience(first)
            repository.save_experience(second)
            records = repository.list_experience(agent_id="writer", task_type="writing")

            self.assertEqual(first.experience_id, second.experience_id)
            self.assertEqual(records, [first])
        finally:
            repository.close()

    def test_distiller_extracts_failure_lesson_from_review_defects(self):
        repository = SQLiteRepository(":memory:")
        try:
            goal = Goal.create("Research", "Prepare evidence", goal_id="goal")
            task = Task.create("goal", "research", "analysis", "Find evidence")
            repository.save_goal(goal)
            repository.save_task(task)
            repository.save_artifact(
                Artifact.create("goal", "research", "analyst", "Draft without source")
            )
            repository.save_review(
                Review.create(
                    "goal",
                    "research",
                    1,
                    Verdict.FAIL,
                    45,
                    (
                        Defect(
                            "evidence",
                            "missing source",
                            "cite a primary source",
                        ),
                    ),
                    "Needs evidence",
                )
            )

            records = ExperienceDistiller(repository).distill_goal("goal")

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].agent_id, "analyst")
            self.assertEqual(records[0].task_type, "analysis")
            self.assertIn("defect:evidence", records[0].tags)
            self.assertIn("cite a primary source", records[0].lessons[0])
            self.assertEqual(repository.list_experience(goal_id="goal"), records)

            repeated = ExperienceDistiller(repository).distill_goal("goal")
            self.assertEqual(repeated, records)
            self.assertEqual(len(repository.list_experience(goal_id="goal")), 1)
        finally:
            repository.close()

    def test_memory_context_includes_relevant_experience(self):
        repository = SQLiteRepository(":memory:")
        try:
            goal = Goal.create("Write", "Write a memo", goal_id="goal")
            task = Task.create("goal", "draft", "writing", "Draft with evidence")
            review = Review.create(
                "goal",
                "draft",
                1,
                Verdict.PASS,
                95,
                (),
                "Clear and supported",
            )
            experience = ExperienceRecord.create(
                task,
                review,
                Artifact.create("goal", "draft", "writer", "Evidence-backed memo"),
                "writer",
                lessons=("Open with the evidence before recommendation",),
                tags=("pattern:success",),
            )
            repository.save_goal(goal)
            repository.save_task(task)
            repository.save_experience(experience)

            context = MemoryManager(repository).build_context(goal, task)

            self.assertEqual(
                context["experience"][0]["lessons"],
                ("Open with the evidence before recommendation",),
            )
            self.assertEqual(context["experience"][0]["agent_id"], "writer")
        finally:
            repository.close()


if __name__ == "__main__":
    unittest.main()
