"""Deterministic experience distillation from reviewed task attempts."""

from __future__ import annotations

from .domain import Artifact, ExperienceRecord, Review, Task, Verdict
from .storage import SQLiteRepository


class ExperienceDistiller:
    """Turn durable review evidence into bounded lessons for future context."""

    def __init__(self, repository: SQLiteRepository):
        self.repository = repository

    def distill_goal(self, goal_id: str) -> list[ExperienceRecord]:
        tasks = {task.task_id: task for task in self.repository.list_tasks(goal_id)}
        for task in tasks.values():
            artifacts = self.repository.list_artifacts(goal_id, task.task_id)
            attempts = {
                attempt.attempt_no: attempt
                for attempt in self.repository.list_attempts(goal_id, task.task_id)
            }
            for review in self.repository.list_reviews(goal_id, task.task_id):
                artifact = self._artifact_for_attempt(artifacts, attempts, review)
                agent_id = self._agent_for_attempt(artifact, attempts, review)
                lessons = self._lessons(review)
                tags = self._tags(review)
                if not lessons or not agent_id:
                    continue
                self.repository.save_experience(
                    ExperienceRecord.create(
                        task,
                        review,
                        artifact,
                        agent_id,
                        lessons=lessons,
                        tags=tags,
                    )
                )
        return self.repository.list_experience(goal_id=goal_id)

    @staticmethod
    def _artifact_for_attempt(
        artifacts: list[Artifact],
        attempts: dict[int, object],
        review: Review,
    ) -> Artifact | None:
        attempt = attempts.get(review.attempt_no)
        artifact_id = getattr(attempt, "artifact_id", None)
        if artifact_id:
            for artifact in artifacts:
                if artifact.artifact_id == artifact_id:
                    return artifact
        return artifacts[-1] if artifacts else None

    @staticmethod
    def _agent_for_attempt(
        artifact: Artifact | None,
        attempts: dict[int, object],
        review: Review,
    ) -> str:
        attempt = attempts.get(review.attempt_no)
        agent_id = str(getattr(attempt, "agent_id", "") or "").strip()
        if agent_id:
            return agent_id
        return artifact.agent_id if artifact is not None else ""

    @staticmethod
    def _lessons(review: Review) -> tuple[str, ...]:
        if review.verdict == Verdict.PASS:
            summary = review.summary.strip()
            return ((f"Successful pattern: {summary}",) if summary else ())
        lessons = []
        for defect in review.defects:
            lesson = (
                f"Repair defect at {defect.location}: {defect.issue}; "
                f"{defect.suggestion}"
            )
            lessons.append(lesson)
        if not lessons and review.summary.strip():
            lessons.append(f"Failure pattern: {review.summary.strip()}")
        return tuple(lessons)

    @staticmethod
    def _tags(review: Review) -> tuple[str, ...]:
        tags = [f"verdict:{review.verdict.value.casefold()}"]
        tags.extend(f"defect:{defect.location.casefold()}" for defect in review.defects)
        if review.verdict == Verdict.PASS:
            tags.append("pattern:success")
        return tuple(tags)
