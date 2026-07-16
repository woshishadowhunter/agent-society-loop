# Changelog

All notable changes are documented here.

## 0.6.0 - 2026-07-16

- Added strict outbound A2A `1.0` `HTTP+JSON` Agent Card inspection, digest pinning, and immutable remote registrations.
- Added bounded `SendMessage`, `GetTask`, and `CancelTask` transport with HTTPS-by-default, no redirects, allowlisted context, and secret-safe errors.
- Added durable delegation lifecycle, resumable polling, operator cancellation, normalized text/data results, and fail-closed remote identity checks.
- Added the no-resend ambiguity rule: uncertain submissions become terminal `unknown` and block for operator review.
- Added deployment-gated remote routing, explicit `run --allow-remote`, and mandatory local review of remote output.
- Added delegation/card evidence to benchmark outcomes plus A2A CLI inspection, registration, listing, and cancellation workflows.

## 0.5.0 - 2026-07-16

- Added a bounded MCP `2025-11-25` stdio client with initialization, paginated tool discovery, and tool calls.
- Added mandatory operator-owned risk mappings and namespace isolation for discovered MCP tools.
- Added immutable benchmark cases, stable benchmark digests, and durable per-candidate case outcomes.
- Added champion/challenger gates for critical failures, pass rate, mean score, per-case regression, and p95 latency.
- Added explicit, identity-checked promotion and active task-type deployment records.
- Added production routing that blocks when an approved champion is unavailable instead of silently falling back.
- Added `evaluate`, `evaluations`, `promote`, and `deployments` CLI workflows and a reproducible benchmark example.

## 0.4.0 - 2026-07-16

- Added verification-gated Git commit, push, and GitHub pull-request publication.
- Added durable `prepared -> committed -> pushed -> pull_request_created` records.
- Added recovery for interruptions after commit, push, or remote PR creation.
- Added exact publication approval payloads including branch policy, base HEAD, diff digest, paths, and checks.
- Added goal-owned path staging and rejection of extra, staged, renamed, protected-branch, or stale changes.
- Added a deterministic publication reviewer gate; model PASS cannot replace a real PR record.
- Added opt-in `maintain --publish` with configurable base, remote, and branch prefix.

## 0.3.0 - 2026-07-16

- Added content-addressed, atomic UTF-8 workspace writes with stale-read protection.
- Added durable per-goal recovery snapshots and conflict-aware restoration.
- Added operator-configured named verification checks with no model-supplied shell text.
- Added durable verification results tied to deterministic workspace digests.
- Added a deterministic reviewer gate that rejects missing or stale check evidence.
- Added goal-scoped diff inspection and protected path, symlink, size, timeout, and output boundaries.
- Added opt-in guarded `maintain --apply` mode while preserving read-only defaults.
- Added cross-approval integration coverage for write, check, resume, diff, and review.

## 0.2.0 - 2026-07-16

- Added strict JSON model-backed planner, worker, and reviewer adapters.
- Added bounded tool loops with deterministic top-level schema validation.
- Added read, write, and execute risk classes with durable human approval.
- Added non-terminal paused goals that resume without consuming failed attempts.
- Added linked, timed, secret-redacted trace spans for model and tool activity.
- Added bounded read-only local workspace tools and a public GitHub issue client.
- Added a read-only `maintain` workflow plus trace and approval CLI commands.

## 0.1.0 - 2026-07-16

- Added explicit goal and task lifecycle contracts.
- Added dependency-aware outer orchestration and review-driven inner retries.
- Added SQLite short-term, long-term, social, and audit memory.
- Added explainable task-specific agent selection.
- Added resumable deterministic scenarios and JSON goal specifications.
- Added OpenAI-compatible HTTP provider boundary with secret-safe errors.
- Added bilingual documentation, tests, CLI inspection, and open-source governance.

