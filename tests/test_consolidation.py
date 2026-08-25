"""Tests for the sleep-replay consolidation engine (memory & learning)."""

import unittest

from seed_society.consolidation import (
    EXPERIENCE_DORMANT_THRESHOLD,
    ConsolidationEngine,
    ConsolidationPolicy,
    decay_factor,
)
from seed_society.deterministic import build_demo_engine
from seed_society.domain import (
    ExperienceRecord,
    Goal,
    Task,
    utc_now,
)
from seed_society.memory import MemoryManager
from seed_society.storage import SQLiteRepository


class ConsolidationPolicyTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        policy = ConsolidationPolicy()
        self.assertEqual(policy.promotion_min_goals, 2)
        self.assertAlmostEqual(
            sum(
                (
                    policy.salience_base_weight,
                    policy.salience_surprise_weight,
                    policy.salience_defect_weight,
                )
            ),
            1.0,
        )

    def test_negative_weight_rejected(self):
        with self.assertRaises(ValueError):
            ConsolidationPolicy(salience_surprise_weight=-0.1)

    def test_single_goal_corroboration_rejected(self):
        with self.assertRaises(ValueError):
            ConsolidationPolicy(promotion_min_goals=1)


class DecayMathTests(unittest.TestCase):
    def test_no_elapsed_time_keeps_strength(self):
        self.assertEqual(decay_factor(0.0, 86_400.0), 1.0)

    def test_half_life_halves_strength(self):
        self.assertAlmostEqual(decay_factor(7 * 86_400.0, 7 * 86_400.0), 0.5)

    def test_two_half_lives_quarter_strength(self):
        self.assertAlmostEqual(decay_factor(2 * 7 * 86_400.0, 7 * 86_400.0), 0.25)


class ExperienceDynamicsTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")
        self.goal = Goal.create("Learn", "Learn memory", goal_id="g1")
        self.task = Task.create("g1", "t1", "analysis", "Analyze")
        self.repository.save_goal(self.goal)
        self.repository.save_tasks([self.task])

    def tearDown(self):
        self.repository.close()

    def _record(self, **overrides):
        values = dict(
            experience_id="experience-test",
            goal_id="g1",
            task_id="t1",
            task_type="analysis",
            agent_id="agent-a",
            attempt_no=1,
            verdict="PASS",
            score=90.0,
            lessons=("lesson",),
            tags=("pattern:success",),
            created_at=utc_now(),
            strength=0.6,
            salience=0.6,
            valence=0.9,
        )
        values.update(overrides)
        return ExperienceRecord(**values)

    def test_reactivate_increases_strength_and_counts(self):
        record = self._record()
        updated = record.reactivate(now="2026-07-16T00:00:00+00:00")
        self.assertAlmostEqual(updated.strength, 0.8)
        self.assertEqual(updated.activations, 1)
        self.assertEqual(updated.last_activated_at, "2026-07-16T00:00:00+00:00")

    def test_reactivate_is_bounded_by_one(self):
        record = self._record(strength=0.99)
        updated = record.reactivate(now="2026-07-16T00:00:00+00:00", gain=0.5)
        self.assertAlmostEqual(updated.strength, 0.995)

    def test_decay_reduces_strength(self):
        record = self._record(strength=0.8)
        updated = record.decay(factor=0.5)
        self.assertAlmostEqual(updated.strength, 0.4)

    def test_out_of_range_dynamics_rejected(self):
        with self.assertRaises(ValueError):
            self._record(valence=1.5)
        with self.assertRaises(ValueError):
            self._record(strength=-0.1)

    def test_content_identity_is_independent_of_dynamics(self):
        from seed_society.domain import Artifact, Review, Verdict

        review = Review.create("g1", "t1", 1, Verdict.PASS, 95, [], "fine")
        artifact = Artifact.create("g1", "t1", "agent-a", "content")
        first = ExperienceRecord.create(
            self.task, review, artifact, "agent-a", lessons=("lesson",), salience=0.6
        )
        second = ExperienceRecord.create(
            self.task, review, artifact, "agent-a", lessons=("lesson",), salience=0.9
        )
        self.assertEqual(first.experience_id, second.experience_id)
        self.assertNotEqual(first.salience, second.salience)

    def test_dormant_records_are_filtered_out(self):
        self.repository.save_experience(self._record(strength=0.9))
        self.repository.save_experience(
            self._record(experience_id="experience-dormant", strength=0.05)
        )
        active = self.repository.list_experience(
            min_strength=EXPERIENCE_DORMANT_THRESHOLD
        )
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].experience_id, "experience-test")


class ConsolidationEngineTests(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteRepository(":memory:")

    def tearDown(self):
        self.repository.close()

    def _run_goal(self, goal_id):
        engine = build_demo_engine(self.repository)
        goal = engine.create_goal(
            "Quantum Coffee Mug Launch",
            "Create launch materials",
            goal_id=goal_id,
        )
        report = engine.run(goal.goal_id)
        self.assertEqual(report.status.value, "succeeded")
        return goal

    def test_dry_run_replays_demo_goal(self):
        goal = self._run_goal("goal-a")
        report = ConsolidationEngine(self.repository).consolidate(goal.goal_id)
        self.assertEqual(report.replayed_attempts, 5)
        self.assertEqual(report.experiences_new, 5)
        self.assertFalse(report.applied)
        self.assertEqual(report.decayed_records, 0)
        self.assertEqual(report.promotions_applied, 0)
        self.assertEqual(len(report.promotion_candidates), 0)  # 1 goal: no corroboration

    def test_replayed_records_carry_salience_and_valence(self):
        goal = self._run_goal("goal-a")
        ConsolidationEngine(self.repository).consolidate(goal.goal_id)
        records = self.repository.list_experience(goal_id=goal.goal_id)
        self.assertEqual(len(records), 5)
        for record in records:
            self.assertTrue(0 < record.salience <= 1)
            self.assertTrue(-1 <= record.valence <= 1)
            if record.verdict == "PASS":
                self.assertGreater(record.valence, 0)
            else:
                self.assertLess(record.valence, 0)

    def test_two_goals_corroborate_and_apply_promotes_semantic_knowledge(self):
        first = self._run_goal("goal-a")
        second = self._run_goal("goal-b")
        # Distill the first goal into the store so cross-goal corroboration
        # (systems consolidation) can be evaluated for the second.
        ConsolidationEngine(self.repository).consolidate(first.goal_id)
        dry = ConsolidationEngine(self.repository).consolidate(second.goal_id)
        self.assertGreaterEqual(len(dry.promotion_candidates), 1)
        self.assertEqual(dry.promotions_applied, 0)

        applied = ConsolidationEngine(self.repository).consolidate(
            second.goal_id, apply=True
        )
        self.assertTrue(applied.applied)
        self.assertGreaterEqual(applied.promotions_applied, 1)
        titles = {item.title for item in self.repository.list_knowledge()}
        self.assertTrue(any("成功模式" in title for title in titles))

    def test_apply_writes_audit_event(self):
        goal = self._run_goal("goal-a")
        ConsolidationEngine(self.repository).consolidate(goal.goal_id, apply=True)
        events = self.repository.list_events(goal.goal_id)
        self.assertTrue(
            any(event.event_type == "memory.consolidated" for event in events)
        )

    def test_apply_decays_long_dormant_seeds(self):
        goal = Goal.create("Decay", "Decay test", goal_id="g-decay")
        task = Task.create("g-decay", "t1", "analysis", "Analyze")
        self.repository.save_goal(goal)
        self.repository.save_tasks([task])
        self.repository.save_experience(
            ExperienceRecord(
                experience_id="experience-old",
                goal_id="g-decay",
                task_id="t1",
                task_type="analysis",
                agent_id="agent-a",
                attempt_no=1,
                verdict="PASS",
                score=90.0,
                lessons=("old lesson",),
                tags=("pattern:success",),
                created_at="2000-01-01T00:00:00+00:00",
                strength=1.0,
                salience=0.9,
                valence=0.9,
            )
        )
        report = ConsolidationEngine(self.repository).consolidate(
            "g-decay", apply=True
        )
        self.assertGreaterEqual(report.decayed_records, 1)
        self.assertGreaterEqual(report.dormant_records, 1)
        updated = self.repository.list_experience(goal_id="g-decay")[0]
        self.assertLess(updated.strength, EXPERIENCE_DORMANT_THRESHOLD)

    def test_self_model_suggestions_are_advisory_only(self):
        goal = self._run_goal("goal-a")
        report = ConsolidationEngine(self.repository).consolidate(goal.goal_id)
        suggestions = report.self_model_suggestions
        self.assertTrue(suggestions)
        self.assertTrue(
            all(item["kind"] in {"success_signal", "failure_mode"} for item in suggestions)
        )
        # Advisory: no genome was written.
        self.assertEqual(self.repository.list_agent_genomes(), [])


class RetrievalReactivationTests(unittest.TestCase):
    def test_context_build_reactivates_injected_experience(self):
        repository = SQLiteRepository(":memory:")
        try:
            goal = Goal.create("Learn", "Learn memory", goal_id="g1")
            task = Task.create("g1", "t1", "analysis", "Analyze")
            repository.save_goal(goal)
            repository.save_tasks([task])
            repository.save_experience(
                ExperienceRecord(
                    experience_id="experience-react",
                    goal_id="g1",
                    task_id="t1",
                    task_type="analysis",
                    agent_id="agent-a",
                    attempt_no=1,
                    verdict="PASS",
                    score=90.0,
                    lessons=("Successful pattern: evidence first",),
                    tags=("pattern:success",),
                    created_at=utc_now(),
                    strength=0.5,
                    salience=0.5,
                    valence=0.9,
                )
            )
            memory = MemoryManager(repository)
            memory.build_context(goal, task)
            updated = repository.list_experience(task_type="analysis")[0]
            self.assertAlmostEqual(updated.strength, 0.75)
            self.assertEqual(updated.activations, 1)
        finally:
            repository.close()


if __name__ == "__main__":
    unittest.main()
