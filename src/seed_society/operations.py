"""Bounded operational snapshots derived from durable runtime state."""

from __future__ import annotations


def collect_metrics(repository) -> dict[str, int]:
    now = repository.scheduler_now()
    return repository.operational_counts(now)


def collect_health(repository, *, worker_id: str = "") -> dict:
    database_time = repository.scheduler_now()
    metrics = repository.operational_counts(database_time)
    degraded = bool(
        metrics["claims_expired_active"] or metrics["outbox_failed"]
    )
    worker = repository.get_worker(worker_id) if worker_id else None
    worker_state = ""
    if worker_id:
        worker_state = (
            "missing"
            if worker is None
            else "expired"
            if worker.is_expired(database_time)
            else "live"
        )
    ready = not worker_id or worker_state == "live"
    result = {
        "status": (
            "unavailable"
            if not ready
            else "degraded"
            if degraded
            else "ok"
        ),
        "ready": ready,
        "database_time": database_time,
        "workers": {
            "total": metrics["workers_total"],
            "expired": metrics["workers_expired"],
        },
        "claims": {
            "active": metrics["claims_active"],
            "expired_active": metrics["claims_expired_active"],
        },
        "approvals": {"pending": metrics["approvals_pending"]},
        "outbox": {
            "pending": metrics["outbox_pending"],
            "delivering": metrics["outbox_delivering"],
            "delivered": metrics["outbox_delivered"],
            "failed": metrics["outbox_failed"],
        },
    }
    if worker_id:
        result["worker"] = {"worker_id": worker_id, "state": worker_state}
    return result
