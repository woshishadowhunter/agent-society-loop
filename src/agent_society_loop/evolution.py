"""Deterministic genome recombination for auditable child candidates."""

from __future__ import annotations

from collections.abc import Sequence

from .domain import (
    AgentGenome,
    AgentSelfModel,
    ExperienceRecord,
    GenomeRecombinationReport,
)


_RISK_RANK = {
    "read_only": 0,
    "approval_required": 1,
    "sandboxed": 2,
    "operator_managed": 3,
}


class GenomeRecombiner:
    """Create child genome candidates without activating deployments."""

    def __init__(self, repository):
        self.repository = repository

    def recombine(
        self,
        child_id: str,
        parent_ids: Sequence[str],
        *,
        task_type: str,
    ) -> GenomeRecombinationReport:
        normalized_child_id = str(child_id).strip()
        if not normalized_child_id:
            raise ValueError("child ID must not be empty")
        normalized_task_type = str(task_type).strip()
        if not normalized_task_type:
            raise ValueError("task type must not be empty")
        normalized_parent_ids = tuple(
            sorted({str(parent_id).strip() for parent_id in parent_ids if str(parent_id).strip()})
        )
        if len(normalized_parent_ids) < 2:
            raise ValueError("at least two parent genomes are required")
        if self.repository.get_agent_genome(normalized_child_id) is not None:
            raise ValueError(f"child genome already exists: {normalized_child_id}")

        parents = self._load_parents(normalized_parent_ids)
        experience = self._supporting_experience(normalized_parent_ids, normalized_task_type)
        pass_lessons = self._experience_lessons(experience, verdict="pass", minimum_score=80)
        fail_lessons = self._experience_lessons(experience, verdict="fail", minimum_score=None)
        child = AgentGenome.create(
            normalized_child_id,
            base_model=self._base_model(parents),
            role_seed=(
                f"Recombined candidate for {normalized_task_type} from "
                + ", ".join(normalized_parent_ids)
            ),
            self_model=AgentSelfModel.create(
                mission=(
                    f"Combine reviewed strengths from {', '.join(normalized_parent_ids)} "
                    f"for {normalized_task_type}"
                ),
                success_signals=(
                    *self._parent_success_signals(parents),
                    *pass_lessons,
                ),
                failure_modes=(
                    *self._parent_failure_modes(parents),
                    *fail_lessons,
                ),
            ),
            traits=self._inherited_traits(parents),
            tool_profile=self._tool_intersection(parents),
            memory_profile={
                "strategy": "reviewed_experience_recombination",
                "task_type": normalized_task_type,
                "supporting_experience": [item.experience_id for item in experience],
            },
            risk_policy=self._strictest_risk_policy(parents),
            parents=normalized_parent_ids,
            generation=max(parent.generation for parent in parents) + 1,
        )
        self.repository.save_agent_genome(child)
        return GenomeRecombinationReport(
            child,
            normalized_task_type,
            normalized_parent_ids,
            tuple(item.experience_id for item in experience),
            child.traits,
            (
                f"risk_policy inherited as {child.risk_policy}",
                "tool_profile is the parent intersection only",
                "candidate is not an active deployment",
            ),
        )

    def _load_parents(self, parent_ids: tuple[str, ...]) -> list[AgentGenome]:
        parents: list[AgentGenome] = []
        missing: list[str] = []
        for parent_id in parent_ids:
            parent = self.repository.get_agent_genome(parent_id)
            if parent is None:
                missing.append(parent_id)
            else:
                parents.append(parent)
        if missing:
            raise KeyError(f"parent genome not found: {', '.join(missing)}")
        return parents

    def _supporting_experience(
        self,
        parent_ids: tuple[str, ...],
        task_type: str,
    ) -> tuple[ExperienceRecord, ...]:
        records: list[ExperienceRecord] = []
        for parent_id in parent_ids:
            records.extend(
                self.repository.list_experience(
                    agent_id=parent_id,
                    task_type=task_type,
                    limit=100,
                )
            )
        return tuple(sorted(records, key=lambda item: item.experience_id))

    @staticmethod
    def _base_model(parents: Sequence[AgentGenome]) -> str:
        models = sorted({parent.base_model for parent in parents})
        return models[0]

    @staticmethod
    def _parent_success_signals(parents: Sequence[AgentGenome]) -> tuple[str, ...]:
        return tuple(
            signal
            for parent in parents
            for signal in parent.self_model.success_signals
        )

    @staticmethod
    def _parent_failure_modes(parents: Sequence[AgentGenome]) -> tuple[str, ...]:
        return tuple(
            failure
            for parent in parents
            for failure in parent.self_model.failure_modes
        )

    @staticmethod
    def _experience_lessons(
        records: Sequence[ExperienceRecord],
        *,
        verdict: str,
        minimum_score: float | None,
    ) -> tuple[str, ...]:
        lessons: list[str] = []
        for record in records:
            if record.verdict.casefold() != verdict:
                continue
            if minimum_score is not None and record.score < minimum_score:
                continue
            lessons.extend(record.lessons)
        return tuple(lessons)

    @staticmethod
    def _inherited_traits(parents: Sequence[AgentGenome]) -> tuple[str, ...]:
        traits = {trait for parent in parents for trait in parent.traits}
        return tuple(sorted(traits))

    @staticmethod
    def _tool_intersection(parents: Sequence[AgentGenome]) -> tuple[str, ...]:
        shared = set(parents[0].tool_profile)
        for parent in parents[1:]:
            shared &= set(parent.tool_profile)
        return tuple(sorted(shared))

    @staticmethod
    def _strictest_risk_policy(parents: Sequence[AgentGenome]) -> str:
        return max(
            (parent.risk_policy for parent in parents),
            key=lambda policy: _RISK_RANK[policy],
        )
