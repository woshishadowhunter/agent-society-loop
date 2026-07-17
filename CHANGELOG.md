# Changelog

All notable changes are documented here.

## 0.10.0 - 2026-07-17

- Added strict, secret-free local model runtime configuration with endpoint-bound model identities, explicit task ownership, independent model review, and fail-closed identity drift checks.
- Added `model doctor` strict JSON compatibility probes and `worker run --model-config` for durable OpenAI-compatible model workers.
- Added bounded no-redirect HTTP transport with response-size ceilings and wall-clock deadlines, including slow-drip regression coverage.
- Moved production worker lease timestamps to database-authoritative SQLite or PostgreSQL clocks while preserving explicit deterministic clocks for conformance tests.
- Added a transactional outbox whose intents commit atomically only with passing worker outcomes, with idempotent enqueue, leased delivery, monotonic delivery tokens, expiry takeover, bounded retries, and terminal failure evidence.
- Added guarded HTTPS webhook dispatch carrying `Idempotency-Key` and `X-Agent-Society-Delivery-Token`, plus outbox inspection commands.
- Bound each dispatcher to one topic, added independent delivery-lease renewal, continuous signal-aware dispatch, bounded status filtering, and terminal-history cleanup.
- Persisted endpoint-bound reviewer identities and attached them to model review traces so reviewer drift fails closed with worker drift.
- Added bounded database-aggregate `health` and `metrics` snapshots covering workers, claims, approvals, and outbox state without loading durable payload histories.
- Added optional worker-liveness health checks, a non-root container image, PostgreSQL Docker Compose worker/dispatcher deployment, local model configuration example, and production-oriented deployment guide.
- Expanded SQLite/PostgreSQL outbox and rollback coverage and increased the suite to 314 tests; PostgreSQL 17 and container smoke tests remain required CI gates.

## 0.9.0 - 2026-07-17

- Added coordinator-only `enqueue` planning and a durable worker service with deterministic ready-task discovery, independent lease maintenance, execute-review-retry processing, bounded task counts, and graceful signal draining.
- Added atomic terminal goal reconciliation to fenced outcome commits so task, evidence, performance, events, claim, and goal state cannot diverge.
- Added fenced approval pauses that persist the request, release ownership, restore the task to pending, pause the goal without consuming an attempt, and resume the goal transactionally after approval.
- Added an optional PostgreSQL execution backend with JSONB state, normalized scheduler indexes, `FOR UPDATE SKIP LOCKED` discovery, monotonic fencing, process-generation protection, expiry recovery, approvals, and traces.
- Added `--database-url`, `AGENT_SOCIETY_DATABASE_URL`, and `--postgres-schema` routing for supported execution-plane and approval commands; unsupported governance commands fail closed instead of falling back to SQLite.
- Added shared SQLite/PostgreSQL conformance contracts for dependency ordering, competing consumers, lease renewal, release, takeover, stale-owner rejection, session supersession, terminal reconciliation, and approval pause/resume.
- Added a PostgreSQL 17 CI service gate alongside the Python 3.10-3.14 SQLite matrix.
- Documented the single-authority rule, trusted UTC clock assumption, SQLite same-host boundary, PostgreSQL execution-plane scope, graceful-drain behavior, and external exactly-once limitation.

## 0.8.0 - 2026-07-16

- Added backend-neutral `SchedulerRepository` contracts for durable worker sessions, task claims, renewal, release, expiry recovery, and fenced outcome commits.
- Added a single-host, multi-process SQLite implementation using short immediate transactions, WAL, a one-active-claim index, and monotonic task-local fencing tokens.
- Added process-generation protection: a new worker session supersedes the old session, whose heartbeat and claim mutations fail closed.
- Added atomic outcome persistence covering optional artifact, review, attempt, performance, events, final task state, and committed claim state.
- Added explicit expiry recovery that blocks ambiguous/interrupted A2A work while preserving accepted/completed no-resend recovery.
- Added scheduler worker/claim inspection, explicit reap, and a deterministic two-connection five-invariant safety campaign.
- Added a synchronous-engine guard that refuses to recover a task while an active scheduler claim owns it.
- Closed legacy per-task write paths while a claim is active, requiring scheduler-managed outcomes to use the fenced atomic commit.
- Added outcome consistency validation so a succeeded task requires both an artifact and a passing review.
- Preserved v0.7 database compatibility and documented the exact same-host, non-exactly-once external-side-effect boundary.

## 0.7.0 - 2026-07-16

- Added strict, content-addressed delegation policy with exact agent/card/task domains, context allowlists, resource ceilings, and required evidence freshness.
- Added immutable policy activation, official A2A TCK attestation, and per-attempt ALLOW/DENY decision records in SQLite.
- Added fail-closed production enforcement before payload construction or network I/O; policy limits constrain requests, results, polling, and deadlines.
- Added bounded import of official `a2aproject/a2a-tck` compatibility JSON with source revision, tool version, transport, MUST-level, interface, tenant, and skill validation.
- Added a read-only eight-check A2A readiness doctor and a socket-free five-scenario reliability campaign.
- Added policy, attestation, decision, doctor, and self-test CLI workflows; production remote runs now require deployment, policy, fresh evidence, and explicit opt-in.
- Preserved no-resend recovery: existing accepted or completed v0.6 delegations resume without being reauthorized or resent.

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

