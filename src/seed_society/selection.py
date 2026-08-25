"""Explainable performance-weighted agent routing."""

from __future__ import annotations

from typing import Sequence

from .domain import AgentProfile, PerformanceRecord, SelectionDecision, Task


class PerformanceWeightedSelector:
    """Rank eligible agents using bounded, task-specific performance signals."""

    def select(
        self,
        task: Task,
        candidates: Sequence[AgentProfile],
        records: Sequence[PerformanceRecord],
    ) -> SelectionDecision:
        eligible = [
            agent
            for agent in candidates
            if agent.enabled
            and agent.role == task.assigned_role
            and ("*" in agent.task_types or task.task_type in agent.task_types)
        ]
        if not eligible:
            raise LookupError(f"no eligible agent for task type {task.task_type}")

        history = {
            record.agent_id: record
            for record in records
            if record.task_type == task.task_type
        }
        ranked: list[tuple[float, str, dict[str, float]]] = []
        for agent in sorted(eligible, key=lambda item: item.agent_id):
            record = history.get(agent.agent_id)
            if record is None:
                components = {
                    "success_rate": 0.5,
                    "review_score": 0.5,
                    "latency": 0.5,
                    "confidence": 0.0,
                }
            else:
                components = {
                    "success_rate": record.success_rate,
                    "review_score": record.avg_score / 100.0,
                    "latency": 1.0 / (1.0 + record.avg_duration_ms / 1000.0),
                    "confidence": min(record.attempts / 10.0, 1.0),
                }
            total = (
                0.45 * components["success_rate"]
                + 0.35 * components["review_score"]
                + 0.10 * components["latency"]
                + 0.10 * components["confidence"]
            )
            ranked.append((total, agent.agent_id, components))

        ranked.sort(key=lambda entry: (-entry[0], entry[1]))
        winner = ranked[0]
        considered = tuple(
            {
                "agent_id": agent_id,
                "total_score": round(total, 6),
                "components": components,
            }
            for total, agent_id, components in ranked
        )
        return SelectionDecision(
            agent_id=winner[1],
            total_score=winner[0],
            components=winner[2],
            considered=considered,
        )
