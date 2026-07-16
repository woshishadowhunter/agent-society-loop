"""Fail-closed policy and conformance controls for remote A2A delegation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from .domain import (
    ConformanceAttestation,
    DelegationPolicy,
    DelegationRule,
    PolicyDecision,
    PolicyVerdict,
    RemoteAgentRegistration,
    Task,
)


_MAX_POLICY_BYTES = 1_048_576
_MAX_REPORT_BYTES = 4_194_304
_POLICY_KEYS = {"policy_id", "version", "default", "rules"}
_RULE_KEYS = {
    "rule_id",
    "task_types",
    "agent_ids",
    "card_sha256s",
    "allowed_context_sections",
    "max_request_bytes",
    "max_result_bytes",
    "max_polls",
    "total_timeout_seconds",
    "required_attestation_kinds",
    "max_attestation_age_hours",
}
_REPORT_KEYS = {"summary", "per_requirement", "per_transport", "agent_card"}
_SUMMARY_KEYS = {
    "timestamp",
    "sut_url",
    "spec_version",
    "overall_compatibility",
    "must_compatibility",
    "should_compatibility",
    "may_compatibility",
}
_REQUIREMENT_KEYS = {"level", "status", "transports", "errors", "test_ids"}
_TRANSPORT_KEYS = {"total", "passed", "failed", "skipped"}


class _DuplicateKey(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _load_document(raw: bytes, *, label: str, max_bytes: int) -> dict[str, Any]:
    if not isinstance(raw, bytes):
        raise TypeError(f"{label} must be provided as bytes")
    if not raw or len(raw) > max_bytes:
        raise ValueError(f"{label} size is invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} must be UTF-8") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_nonfinite,
        )
    except _DuplicateKey:
        raise
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} is not valid strict JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value


def _require_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"{label} is missing fields: {', '.join(missing)}")


def _string(value: Any, label: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ValueError(f"{label} must be a string")
    return value.strip()


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < (1 if positive else 0):
        qualifier = "positive " if positive else "non-negative "
        raise ValueError(f"{label} must be a {qualifier}integer")
    return value


def _string_list(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
    reject_duplicates: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    items = tuple(_string(item, label) for item in value)
    if not allow_empty and not items:
        raise ValueError(f"{label} must not be empty")
    if reject_duplicates and len(set(items)) != len(items):
        raise ValueError(f"{label} contains duplicate values")
    return items


def parse_policy_document(raw: bytes) -> DelegationPolicy:
    """Parse a bounded declarative policy with strict field validation."""

    document = _load_document(raw, label="delegation policy", max_bytes=_MAX_POLICY_BYTES)
    _require_keys(document, _POLICY_KEYS, "delegation policy")
    if not isinstance(document["rules"], list) or not document["rules"]:
        raise ValueError("delegation policy rules must be a non-empty array")
    rules: list[DelegationRule] = []
    for index, value in enumerate(document["rules"]):
        label = f"delegation policy rule {index}"
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object")
        _require_keys(value, _RULE_KEYS, label)
        rules.append(
            DelegationRule.create(
                _string(value["rule_id"], f"{label} rule_id"),
                _string_list(value["task_types"], f"{label} task_types"),
                _string_list(value["agent_ids"], f"{label} agent_ids"),
                _string_list(value["card_sha256s"], f"{label} card_sha256s"),
                allowed_context_sections=_string_list(
                    value["allowed_context_sections"],
                    f"{label} allowed_context_sections",
                    allow_empty=True,
                ),
                max_request_bytes=_integer(
                    value["max_request_bytes"],
                    f"{label} max_request_bytes",
                    positive=True,
                ),
                max_result_bytes=_integer(
                    value["max_result_bytes"],
                    f"{label} max_result_bytes",
                    positive=True,
                ),
                max_polls=_integer(
                    value["max_polls"], f"{label} max_polls", positive=True
                ),
                total_timeout_seconds=_integer(
                    value["total_timeout_seconds"],
                    f"{label} total_timeout_seconds",
                    positive=True,
                ),
                required_attestation_kinds=_string_list(
                    value["required_attestation_kinds"],
                    f"{label} required_attestation_kinds",
                ),
                max_attestation_age_hours=_integer(
                    value["max_attestation_age_hours"],
                    f"{label} max_attestation_age_hours",
                    positive=True,
                ),
            )
        )
    return DelegationPolicy.create(
        _string(document["policy_id"], "delegation policy policy_id"),
        _integer(document["version"], "delegation policy version", positive=True),
        rules,
        default=_string(document["default"], "delegation policy default"),
    )


def _percentage(value: Any, label: str) -> float:
    if not isinstance(value, str) or not re.fullmatch(r"(?:100(?:\.0+)?|\d{1,2}(?:\.\d+)?)%", value):
        raise ValueError(f"{label} percentage is invalid")
    parsed = float(value[:-1])
    if not math.isfinite(parsed) or not 0 <= parsed <= 100:
        raise ValueError(f"{label} percentage is invalid")
    return parsed


def _timestamp(value: Any, label: str) -> str:
    text = _string(value, label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{label} timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} timestamp must include a timezone")
    return parsed.isoformat()


def _validate_requirement(value: Any, requirement_id: str) -> tuple[str, str]:
    label = f"TCK requirement {requirement_id}"
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    _require_keys(value, _REQUIREMENT_KEYS, label)
    level = _string(value["level"], f"{label} level")
    status = _string(value["status"], f"{label} status")
    if level not in {"MUST", "SHOULD", "MAY"}:
        raise ValueError(f"{label} level is invalid")
    if status not in {"PASS", "FAIL", "SKIPPED", "NOT TESTED"}:
        raise ValueError(f"{label} status is invalid")
    transports = value["transports"]
    if not isinstance(transports, dict):
        raise ValueError(f"{label} transports must be an object")
    for name, transport_status in transports.items():
        _string(name, f"{label} transport")
        if transport_status not in {"PASS", "FAIL", "SKIPPED"}:
            raise ValueError(f"{label} transport status is invalid")
    _string_list(
        value["errors"],
        f"{label} errors",
        allow_empty=True,
        reject_duplicates=False,
    )
    _string_list(
        value["test_ids"],
        f"{label} test_ids",
        allow_empty=True,
        reject_duplicates=False,
    )
    return level, status


def _validate_transport(value: Any, name: str) -> dict[str, int]:
    label = f"TCK transport {name}"
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    _require_keys(value, _TRANSPORT_KEYS, label)
    counts = {key: _integer(value[key], f"{label} {key}") for key in _TRANSPORT_KEYS}
    if counts["total"] != counts["passed"] + counts["failed"] + counts["skipped"]:
        raise ValueError(f"{label} counts are inconsistent")
    return counts


def _validate_report_card(
    card: Any, registration: RemoteAgentRegistration
) -> None:
    if not isinstance(card, dict):
        raise ValueError("TCK agent_card must be an object")
    interfaces = card.get("supportedInterfaces")
    if not isinstance(interfaces, list):
        raise ValueError("TCK agent card interface declaration is missing")
    selected = next(
        (
            item
            for item in interfaces
            if isinstance(item, dict)
            and item.get("url") == registration.interface_url
            and item.get("protocolBinding") == "HTTP+JSON"
            and item.get("protocolVersion") == "1.0"
            and str(item.get("tenant", "")) == registration.tenant
        ),
        None,
    )
    if selected is None:
        raise ValueError("TCK agent card has no exact registered interface identity")
    skills = card.get("skills")
    declared = {
        item.get("id")
        for item in skills or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if not set(registration.skill_by_task_type.values()).issubset(declared):
        raise ValueError("TCK agent card does not contain every registered skill")


def parse_a2a_tck_report(
    raw: bytes,
    registration: RemoteAgentRegistration,
    *,
    source_revision: str,
    tool_version: str,
) -> ConformanceAttestation:
    """Import official TCK JSON as bounded evidence for one pinned card."""

    document = _load_document(raw, label="A2A TCK report", max_bytes=_MAX_REPORT_BYTES)
    _require_keys(document, _REPORT_KEYS, "A2A TCK report")
    summary = document["summary"]
    if not isinstance(summary, dict):
        raise ValueError("A2A TCK report summary must be an object")
    _require_keys(summary, _SUMMARY_KEYS, "A2A TCK report summary")
    observed_at = _timestamp(summary["timestamp"], "A2A TCK report")
    _string(summary["sut_url"], "A2A TCK report sut_url")
    if summary["spec_version"] != "1.0":
        raise ValueError("A2A TCK report spec version must be 1.0")
    percentages = {
        name: _percentage(summary[name], f"A2A TCK report {name}")
        for name in (
            "overall_compatibility",
            "must_compatibility",
            "should_compatibility",
            "may_compatibility",
        )
    }

    requirements = document["per_requirement"]
    if not isinstance(requirements, dict) or not requirements:
        raise ValueError("A2A TCK per_requirement must be a non-empty object")
    must_failure = False
    must_count = 0
    for requirement_id, value in requirements.items():
        identifier = _string(requirement_id, "TCK requirement ID")
        level, status = _validate_requirement(value, identifier)
        if level == "MUST":
            must_count += 1
            must_failure = must_failure or status == "FAIL"
    if must_count == 0:
        raise ValueError("A2A TCK report contains no MUST requirements")

    transports = document["per_transport"]
    if not isinstance(transports, dict):
        raise ValueError("A2A TCK per_transport must be an object")
    counts = {
        _string(name, "TCK transport name"): _validate_transport(value, name)
        for name, value in transports.items()
    }
    if "http_json" not in counts or counts["http_json"]["total"] < 1:
        raise ValueError("A2A TCK report must contain tested http_json transport")
    _validate_report_card(document["agent_card"], registration)

    http_counts = counts["http_json"]
    passed = (
        percentages["must_compatibility"] == 100.0
        and not must_failure
        and http_counts["failed"] == 0
    )
    metrics: dict[str, float | int] = {
        **percentages,
        "must_requirement_count": must_count,
        "http_json_total": http_counts["total"],
        "http_json_passed": http_counts["passed"],
        "http_json_failed": http_counts["failed"],
        "http_json_skipped": http_counts["skipped"],
    }
    return ConformanceAttestation.create(
        agent_id=registration.agent_id,
        card_sha256=registration.card_sha256,
        kind="a2a-tck",
        report_sha256=hashlib.sha256(raw).hexdigest(),
        source_revision=source_revision,
        tool_version=tool_version,
        spec_version="1.0",
        observed_at=observed_at,
        passed=passed,
        metrics=metrics,
    )


class DelegationPolicyEvaluator:
    """Resolve an active policy and persist one decision per task attempt."""

    def __init__(
        self,
        repository: Any,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self._now = now or (lambda: datetime.now(timezone.utc))

    def decide(
        self,
        task: Task,
        registration: RemoteAgentRegistration,
        attempt_no: int,
        runtime_limits: Any,
        *,
        persist: bool = True,
    ) -> PolicyDecision:
        existing = self.repository.get_attempt_policy_decision(
            task.goal_id, task.task_id, attempt_no, registration.agent_id
        )
        if existing is not None:
            return existing

        activation = self.repository.get_policy_activation(task.task_type)
        if activation is None:
            return self._decision(
                task,
                registration,
                attempt_no,
                PolicyVerdict.DENY,
                ("no_active_policy",),
                persist=persist,
            )
        policy = self.repository.get_policy(activation.policy_digest)
        if policy is None:
            return self._decision(
                task,
                registration,
                attempt_no,
                PolicyVerdict.DENY,
                ("policy_not_found",),
                policy_digest=activation.policy_digest,
                persist=persist,
            )
        matches = [
            rule
            for rule in policy.rules
            if task.task_type in rule.task_types
            and registration.agent_id in rule.agent_ids
            and registration.card_sha256 in rule.card_sha256s
        ]
        if len(matches) != 1:
            reason = "ambiguous_policy_rule" if matches else "no_matching_rule"
            return self._decision(
                task,
                registration,
                attempt_no,
                PolicyVerdict.DENY,
                (reason,),
                policy_digest=policy.policy_digest,
                persist=persist,
            )
        rule = matches[0]
        if not set(registration.allowed_context_sections).issubset(
            rule.allowed_context_sections
        ):
            return self._decision(
                task,
                registration,
                attempt_no,
                PolicyVerdict.DENY,
                ("context_not_allowed",),
                policy_digest=policy.policy_digest,
                rule=rule,
                persist=persist,
            )

        attestations = self.repository.list_attestations(
            registration.agent_id, registration.card_sha256
        )
        selected: list[ConformanceAttestation] = []
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("policy evaluator clock must return a timezone-aware value")
        for kind in rule.required_attestation_kinds:
            candidates = [item for item in attestations if item.kind == kind]
            if not candidates:
                return self._decision(
                    task,
                    registration,
                    attempt_no,
                    PolicyVerdict.DENY,
                    ("missing_attestation",),
                    policy_digest=policy.policy_digest,
                    rule=rule,
                    persist=persist,
                )
            latest = max(candidates, key=lambda item: datetime.fromisoformat(item.observed_at))
            observed = datetime.fromisoformat(latest.observed_at)
            if not latest.passed:
                reason = "failed_attestation"
            elif observed > now or now - observed > timedelta(
                hours=rule.max_attestation_age_hours
            ):
                reason = "stale_attestation"
            else:
                selected.append(latest)
                continue
            return self._decision(
                task,
                registration,
                attempt_no,
                PolicyVerdict.DENY,
                (reason,),
                policy_digest=policy.policy_digest,
                rule=rule,
                attestation_ids=(latest.attestation_id,),
                persist=persist,
            )

        max_request_bytes = min(
            _runtime_positive_int(runtime_limits, "max_request_bytes"),
            rule.max_request_bytes,
        )
        max_result_bytes = min(
            _runtime_positive_int(runtime_limits, "max_result_bytes"),
            rule.max_result_bytes,
        )
        max_polls = min(
            _runtime_positive_int(runtime_limits, "max_polls"), rule.max_polls
        )
        runtime_timeout = getattr(runtime_limits, "total_timeout", None)
        if (
            isinstance(runtime_timeout, bool)
            or not isinstance(runtime_timeout, (int, float))
            or not math.isfinite(float(runtime_timeout))
            or runtime_timeout < 1
        ):
            raise ValueError("runtime total_timeout must be at least one second")
        total_timeout_seconds = min(int(runtime_timeout), rule.total_timeout_seconds)
        return self._decision(
            task,
            registration,
            attempt_no,
            PolicyVerdict.ALLOW,
            ("policy_allowed",),
            policy_digest=policy.policy_digest,
            rule=rule,
            allowed_context_sections=registration.allowed_context_sections,
            max_request_bytes=max_request_bytes,
            max_result_bytes=max_result_bytes,
            max_polls=max_polls,
            total_timeout_seconds=total_timeout_seconds,
            attestation_ids=tuple(item.attestation_id for item in selected),
            persist=persist,
        )

    def _decision(
        self,
        task: Task,
        registration: RemoteAgentRegistration,
        attempt_no: int,
        verdict: PolicyVerdict,
        reasons: Sequence[str],
        *,
        policy_digest: str = "",
        rule: DelegationRule | None = None,
        allowed_context_sections: Sequence[str] = (),
        max_request_bytes: int = 0,
        max_result_bytes: int = 0,
        max_polls: int = 0,
        total_timeout_seconds: int = 0,
        attestation_ids: Sequence[str] = (),
        persist: bool,
    ) -> PolicyDecision:
        decision = PolicyDecision.create(
            task.goal_id,
            task.task_id,
            attempt_no,
            registration,
            verdict,
            policy_digest=policy_digest,
            policy_rule_id=rule.rule_id if rule else "",
            reason_codes=reasons,
            allowed_context_sections=allowed_context_sections,
            max_request_bytes=max_request_bytes,
            max_result_bytes=max_result_bytes,
            max_polls=max_polls,
            total_timeout_seconds=total_timeout_seconds,
            attestation_ids=attestation_ids,
        )
        if persist:
            self.repository.save_policy_decision(decision)
        return decision


def _runtime_positive_int(runtime_limits: Any, name: str) -> int:
    value = getattr(runtime_limits, name, None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"runtime {name} must be a positive integer")
    return value
