# Seed Society Design

## 1. Purpose

Seed Society is an auditable Python runtime for coordinating specialized AI agents around a goal. It turns the "Agent Society" blueprint into a durable execution system with goal lifecycle management, an outer planning loop, an inner execution-review loop, three memory scopes, performance-based agent selection, and deterministic offline examples.

The project must be useful without an API key, while exposing stable protocols for real model and tool adapters. "Self-evolution" means improving future routing decisions from recorded outcomes. It never means silently rewriting source code, prompts, acceptance criteria, or safety policy.

## 2. Success Criteria

1. A user can install the package on Python 3.10+ and run a complete offline demo from one CLI command.
2. A goal moves through explicit states: `created`, `planning`, `running`, `succeeded`, `failed`, or `blocked`.
3. The outer loop plans dependencies, dispatches ready tasks, records progress, and reaches a terminal goal state.
4. The inner loop executes, reviews, feeds structured defects back, and retries up to a configured limit.
5. SQLite persists goal state, tasks, attempts, reviews, artifacts, long-term knowledge, events, and agent performance.
6. Agent selection uses task-type-specific historical performance while retaining a cold-start fallback.
7. A stopped run can be inspected and resumed without redoing completed tasks.
8. Every material state transition is represented in an append-only event log.
9. Unit and integration tests prove planning, dependency ordering, fail-fix-pass behavior, budget exhaustion, performance updates, persistence, resume, and CLI behavior.
10. English and Simplified Chinese documentation explain architecture, quick start, extension points, limitations, and responsible use.

## 3. Scope

### Included in v0.1

- Standard-library-first Python package with no required runtime dependencies.
- Typed dataclasses and enums for goals, tasks, attempts, reviews, defects, artifacts, budgets, agents, performance, and events.
- SQLite repository using schema migrations created on first open.
- Agent protocols for planner, worker, reviewer, memory manager, and model provider.
- Weighted agent selector using task-specific success rate, review score, latency, sample confidence, and deterministic tie-breaking.
- Sequential dependency-aware loop engine with bounded retries and actions.
- Checkpoint/resume semantics through persisted task and goal states.
- Deterministic "quantum coffee mug launch" scenario that demonstrates a failed review followed by correction and a changed performance record.
- Generic OpenAI-compatible chat-completions provider implemented with `urllib`, enabled only when configured.
- CLI commands: `demo`, `run`, `status`, `events`, `agents`, and `knowledge`.
- JSON goal specification format and JSON/text output.

### Explicitly Deferred

- Distributed queues, concurrent workers, web dashboard, container orchestration, autonomous source-code mutation, and unbounded recursive planning.
- Provider-specific SDK dependencies. The generic HTTP provider is the first real-model bridge.
- Vector embeddings. Long-term memory v0.1 uses tagged text search and deterministic ranking.

## 4. Architecture

The package is split into focused modules:

- `domain.py`: immutable contracts, states, validation, and serialization helpers.
- `ports.py`: protocols that isolate the engine from planners, workers, reviewers, repositories, clocks, and providers.
- `storage.py`: SQLite schema and persistence implementation.
- `memory.py`: short-term task context, long-term knowledge retrieval, and social performance updates.
- `selection.py`: transparent task-to-agent ranking.
- `engine.py`: outer and inner loops, budgets, retries, transitions, and recovery.
- `deterministic.py`: reproducible planner, workers, and reviewer for the bundled scenario and examples.
- `providers.py`: OpenAI-compatible HTTP model provider.
- `cli.py`: command parsing, configuration loading, and human/JSON reports.

Dependencies point inward: the CLI and adapters depend on the engine and ports; the engine depends on domain contracts and protocols; domain contracts depend only on the standard library.

## 5. Domain Model

### Goal

`Goal` contains `goal_id`, `title`, `description`, `status`, `created_at`, `updated_at`, and `failure_reason`. Terminal states are immutable. The engine alone performs validated transitions.

### Task

`Task` contains `task_id`, `goal_id`, `task_type`, `description`, `assigned_role`, `acceptance_criteria`, `dependencies`, `context`, `status`, `max_attempts`, and `position`. A task becomes ready only when all dependencies succeeded.

Acceptance criteria are structured dictionaries. Built-in deterministic review supports `required_terms`, `min_sources`, `min_length`, and `required_sections`; real reviewers may interpret additional names but must preserve them in the review record.

### Review

`Review` contains `verdict`, `score`, `defects`, and `summary`. A passing review requires `verdict=PASS` and a score at or above the engine threshold. A defect has `location`, `issue`, and `suggestion`.

### Agent and Performance

`AgentProfile` declares `agent_id`, `role`, `model_id`, supported task types, and enabled status. `PerformanceRecord` aggregates attempts, passes, average score, average duration, and recent outcomes by `(agent_id, task_type)`.

## 6. Runtime Flow

1. Create a goal and persist a `goal.created` event.
2. The planner produces a validated task DAG. Duplicate IDs, missing dependencies, and cycles reject the plan before execution.
3. The engine repeatedly finds the lowest-position ready task.
4. The selector ranks eligible workers and records the ranking explanation.
5. The memory manager builds execution context from goal data, dependency artifacts, relevant long-term knowledge, and prior review feedback.
6. The worker produces an artifact. The reviewer returns a structured review.
7. PASS completes the task; FAIL appends defects to short-term memory and retries until `max_attempts`.
8. Every attempt updates social performance in one SQLite transaction with its review and event records.
9. If no task can progress, retry or action budgets are exhausted, or a task exhausts attempts, the goal becomes `failed` or `blocked` with a concrete reason.
10. When all tasks succeed, the goal becomes `succeeded`. `resume` reloads the same persisted graph and runs only unfinished tasks.

## 7. Selection Algorithm

Eligible agents must be enabled, match the required role, and support the task type or wildcard `*`. The score is intentionally understandable:

`0.45 * success_rate + 0.35 * normalized_review_score + 0.10 * latency_score + 0.10 * confidence`

Cold-start defaults are neutral (`0.5` success, `50` review, `0.0` confidence). Confidence grows up to 1.0 over 10 attempts. Ties resolve by `agent_id`, making tests and audits reproducible. The selector returns both the winner and score breakdown.

## 8. Memory

- Short-term memory is persisted attempt feedback and dependency artifacts scoped to one goal run.
- Long-term memory is a tagged knowledge table. Retrieval tokenizes the query, scores term and tag overlap, then sorts by score and creation time.
- Social memory is the performance table plus bounded recent outcome records. Updates occur only from completed, reviewed attempts.

No memory entry can override engine budgets, task acceptance criteria, or safety configuration.

## 9. Reliability and Safety

- All IDs, transitions, scores, dependency graphs, and budget values are validated.
- Every run has `max_actions`, per-task `max_attempts`, and a minimum passing score.
- SQLite transactions keep attempts, reviews, events, and performance consistent.
- Provider secrets are read from environment variables and never persisted or printed.
- Provider responses are treated as untrusted input and must parse into the declared JSON contracts.
- Failed provider calls produce explicit failed attempts and bounded retries.
- The system records decision evidence; it does not claim sentience, consciousness, or unsupervised safety.

## 10. CLI Experience

- `seed-society demo --db PATH`: seed deterministic agents and knowledge, run the bundled scenario, and print the final report.
- `seed-society run SPEC.json --db PATH`: create and run a goal from JSON using the deterministic adapter in v0.1.
- `seed-society status GOAL_ID --db PATH [--json]`: inspect goal, task, attempt, and artifact summaries.
- `seed-society events GOAL_ID --db PATH [--json]`: inspect the ordered audit trail.
- `seed-society agents --db PATH [--json]`: inspect profiles and performance.
- `seed-society knowledge add|search ...`: manage long-term seed knowledge.

Commands return non-zero exit codes for invalid input, failed goals, missing records, and provider failures.

## 11. Test Strategy

- Domain tests cover validation, transitions, and task DAG rejection.
- Storage tests use temporary SQLite databases and reopen them to prove durability.
- Selector tests prove cold start, task-specific preference, and deterministic ties.
- Engine integration tests use real deterministic agents, storage, and memory; no mocks are required.
- CLI tests call `main(argv)` against temporary files and capture output.
- A final smoke test installs the wheel into a temporary virtual environment and runs the offline demo.

## 12. Open-Source Readiness

The repository includes an MIT license, contribution guide, code of conduct, security policy, issue templates, CI for supported Python versions, package metadata, runnable examples, architecture documentation, and bilingual READMEs. The README clearly labels v0.1 boundaries and provides evidence-producing commands rather than inflated capability claims.

