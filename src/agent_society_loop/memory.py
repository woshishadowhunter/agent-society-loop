"""Three-scope memory services built on the durable repository."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Sequence

from .domain import Goal, KnowledgeItem, PerformanceRecord, Task
from .storage import SQLiteRepository


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[\w-]+", text.casefold()))


class MemoryManager:
    def __init__(self, repository: SQLiteRepository):
        self.repository = repository

    def build_context(self, goal: Goal, task: Task) -> dict[str, object]:
        dependency_artifacts: dict[str, str] = {}
        for dependency in task.dependencies:
            artifacts = self.repository.list_artifacts(goal.goal_id, dependency)
            if artifacts:
                dependency_artifacts[dependency] = artifacts[-1].content

        reviews = self.repository.list_reviews(goal.goal_id, task.task_id)
        feedback = [
            {
                "attempt_no": review.attempt_no,
                "score": review.score,
                "summary": review.summary,
                "defects": [
                    {
                        "location": defect.location,
                        "issue": defect.issue,
                        "suggestion": defect.suggestion,
                    }
                    for defect in review.defects
                ],
            }
            for review in reviews
            if review.verdict.value == "FAIL"
        ]
        knowledge = self.search_knowledge(task.description, (task.task_type,), limit=5)
        return {
            "goal": {"title": goal.title, "description": goal.description},
            "task_context": dict(task.context),
            "dependency_artifacts": dependency_artifacts,
            "review_feedback": feedback,
            "knowledge": [
                {"title": item.title, "content": item.content, "tags": item.tags}
                for item in knowledge
            ],
        }

    def add_knowledge(
        self, title: str, content: str, tags: Sequence[str] = ()
    ) -> str:
        item = KnowledgeItem.create(title, content, tags)
        self.repository.save_knowledge(item)
        return item.knowledge_id

    def search_knowledge(
        self, query: str, tags: Sequence[str] = (), *, limit: int = 5
    ) -> list[KnowledgeItem]:
        query_tokens = _tokens(query)
        requested_tags = {tag.casefold() for tag in tags}
        ranked: list[tuple[int, str, KnowledgeItem]] = []
        for item in self.repository.list_knowledge():
            text_score = len(query_tokens & _tokens(f"{item.title} {item.content}"))
            tag_score = len(requested_tags & {tag.casefold() for tag in item.tags}) * 2
            score = text_score + tag_score
            if score:
                ranked.append((score, item.created_at, item))
        ranked.sort(key=lambda entry: (-entry[0], entry[1], entry[2].knowledge_id))
        return [entry[2] for entry in ranked[:limit]]

    def record_outcome(
        self,
        agent_id: str,
        task_type: str,
        passed: bool,
        score: float,
        duration_ms: float,
    ) -> PerformanceRecord:
        record = self.calculate_outcome(
            agent_id, task_type, passed, score, duration_ms
        )
        self.repository.save_performance(record)
        return record

    def calculate_outcome(
        self,
        agent_id: str,
        task_type: str,
        passed: bool,
        score: float,
        duration_ms: float,
    ) -> PerformanceRecord:
        current = self.repository.get_performance(agent_id, task_type)
        if current is None:
            current = PerformanceRecord(agent_id, task_type)
        attempts = current.attempts + 1
        record = replace(
            current,
            attempts=attempts,
            passes=current.passes + int(passed),
            avg_score=((current.avg_score * current.attempts) + score) / attempts,
            avg_duration_ms=(
                (current.avg_duration_ms * current.attempts) + duration_ms
            )
            / attempts,
            recent_results=(
                *current.recent_results[-9:],
                {"passed": passed, "score": float(score), "duration_ms": float(duration_ms)},
            ),
        )
        return record
