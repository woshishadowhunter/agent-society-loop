# Seed Society Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and publish a durable, auditable Python runtime that realizes the Agent Society double-loop architecture and runs a complete offline scenario.

**Architecture:** A typed domain core and protocol layer drive a dependency-aware loop engine. SQLite provides all three memory scopes, checkpoints, events, and performance records; adapters provide deterministic agents and an optional OpenAI-compatible model bridge.

**Tech Stack:** Python 3.10+, standard library, SQLite, `unittest`, `pyproject.toml`, GitHub Actions.

## Global Constraints

- Required runtime dependencies: none.
- Supported Python versions: 3.10 through 3.14.
- Production code uses typed public interfaces and focused modules.
- All runtime state changes are persisted and auditable.
- No autonomous source-code or policy mutation.
- New behavior follows red-green-refactor TDD.

---

### Task 1: Package Skeleton and Domain Contracts

**Files:**
- Create: `pyproject.toml`
- Create: `src/seed_society/__init__.py`
- Create: `src/seed_society/__main__.py`
- Create: `src/seed_society/domain.py`
- Create: `src/seed_society/ports.py`
- Test: `tests/test_domain.py`

**Interfaces:**
- Produces: enums `GoalStatus`, `TaskStatus`, `Verdict`; dataclasses `Goal`, `Task`, `Artifact`, `Defect`, `Review`, `AgentProfile`, `PerformanceRecord`, `SelectionDecision`, `RunBudget`, `RunReport`; protocols `Planner`, `Worker`, `Reviewer`, `Repository`, `Memory`, `AgentSelector`.

- [ ] Write tests that validate score ranges, attempt budgets, goal transitions, duplicate task IDs, missing dependencies, and cycles.
- [ ] Run `python -m unittest tests.test_domain -v` and verify failure because the package does not exist.
- [ ] Implement only the domain types, validation helpers, transition map, DAG validation, and protocols needed by the tests.
- [ ] Re-run the domain tests and verify they pass.
- [ ] Commit with `feat: define agent society domain contracts`.

### Task 2: Durable SQLite Memory and Event Journal

**Files:**
- Create: `src/seed_society/storage.py`
- Create: `src/seed_society/memory.py`
- Test: `tests/test_storage.py`
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: domain dataclasses and repository/memory protocols.
- Produces: `SQLiteRepository`, `MemoryManager`, CRUD for goals/tasks/artifacts/reviews/attempts/events/knowledge/agents/performance, `build_context()`, `record_outcome()`, and `search_knowledge()`.

- [ ] Write persistence tests that close and reopen a temporary database, then recover goals, tasks, artifacts, events, and performance.
- [ ] Run storage tests and verify they fail because implementations are missing.
- [ ] Implement schema creation and transactional repository methods with JSON serialization for structured fields.
- [ ] Run storage tests and verify they pass.
- [ ] Write memory tests for dependency artifacts, feedback context, tagged knowledge ranking, and task-specific performance aggregation.
- [ ] Run memory tests and verify they fail because memory behavior is missing.
- [ ] Implement the memory manager and atomic outcome aggregation.
- [ ] Run both storage and memory tests and verify they pass.
- [ ] Commit with `feat: add durable three-scope memory`.

### Task 3: Transparent Agent Selection

**Files:**
- Create: `src/seed_society/selection.py`
- Test: `tests/test_selection.py`

**Interfaces:**
- Consumes: enabled `AgentProfile` objects and task-type performance records.
- Produces: `PerformanceWeightedSelector.select(task, candidates, records) -> SelectionDecision` with component scores and deterministic tie-breaking.

- [ ] Write tests for no eligible worker, neutral cold start, preference for relevant history, ignoring unrelated task history, and deterministic ties.
- [ ] Run `python -m unittest tests.test_selection -v` and verify expected failures.
- [ ] Implement eligibility filtering and the documented weighted score formula.
- [ ] Re-run selector tests and the existing suite.
- [ ] Commit with `feat: add performance weighted agent selection`.

### Task 4: Goal Engine and Double Loop

**Files:**
- Create: `src/seed_society/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `Planner`, worker registry, `Reviewer`, `Repository`, `Memory`, `AgentSelector`, and `RunBudget`.
- Produces: `LoopEngine.create_goal()`, `LoopEngine.run()`, `LoopEngine.resume()`, terminal `RunReport`, append-only lifecycle events.

- [ ] Write an integration test proving planning, dependency order, one failed review, feedback-based correction, final success, and performance updates.
- [ ] Run the test and verify it fails because `LoopEngine` is missing.
- [ ] Implement validated planning and one ready-task execution path.
- [ ] Re-run and preserve the next expected failure at retry behavior.
- [ ] Implement the inner review/fix loop, action budget, attempt exhaustion, and goal terminal transitions.
- [ ] Add tests for invalid plans, blocked dependency graphs, failed tasks, budget exhaustion, and resume skipping completed tasks.
- [ ] Run `python -m unittest tests.test_engine -v` and the full suite.
- [ ] Commit with `feat: implement auditable double loop engine`.

### Task 5: Deterministic Scenario and Provider Adapter

**Files:**
- Create: `src/seed_society/deterministic.py`
- Create: `src/seed_society/providers.py`
- Create: `examples/quantum_mug_launch.py`
- Test: `tests/test_deterministic.py`
- Test: `tests/test_providers.py`

**Interfaces:**
- Produces: `QuantumMugPlanner`, `TemplateWorker`, `CriteriaReviewer`, `OpenAICompatibleProvider`, and `build_demo_engine()`.

- [ ] Write deterministic criteria tests for required terms, sections, source counts, and repair from review feedback.
- [ ] Verify failures, implement the scenario agents, and verify passes.
- [ ] Write provider tests using a local HTTP server for request shape, JSON response parsing, HTTP errors, malformed responses, and secret-safe messages.
- [ ] Verify failures, implement the `urllib` provider with timeout and explicit configuration, and verify passes.
- [ ] Run the example twice against temporary databases and verify reproducible terminal success.
- [ ] Commit with `feat: add offline scenario and model provider`.

### Task 6: CLI and JSON Goal Specifications

**Files:**
- Create: `src/seed_society/cli.py`
- Create: `examples/goal-spec.json`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `main(argv: Sequence[str] | None) -> int`; commands `demo`, `run`, `status`, `events`, `agents`, and `knowledge add/search`.

- [ ] Write CLI tests for help, demo success, status JSON, event ordering, agent metrics, knowledge round trip, invalid spec, and missing goal exit codes.
- [ ] Run CLI tests and verify expected failures.
- [ ] Implement argument parsing and command handlers with stable JSON output.
- [ ] Re-run CLI tests and the complete suite.
- [ ] Run `python -m seed_society demo --db :memory:` and verify a successful goal plus at least one retry.
- [ ] Commit with `feat: add agent society command line interface`.

### Task 7: Bilingual Documentation and Open-Source Governance

**Files:**
- Create: `README.md`
- Create: `README.zh-CN.md`
- Create: `docs/architecture.md`
- Create: `docs/goal-spec.md`
- Create: `CONTRIBUTING.md`
- Create: `CODE_OF_CONDUCT.md`
- Create: `SECURITY.md`
- Create: `CHANGELOG.md`
- Create: `LICENSE`
- Create: `.gitignore`
- Create: `.github/workflows/ci.yml`
- Create: `.github/ISSUE_TEMPLATE/bug_report.yml`
- Create: `.github/ISSUE_TEMPLATE/feature_request.yml`

**Interfaces:**
- Documents every public command, JSON field, extension protocol, safety boundary, and verification command implemented in Tasks 1-6.

- [ ] Write English and Chinese quick starts that run the offline demo first.
- [ ] Add a Mermaid architecture diagram, state flow, extension guide, and limitations.
- [ ] Add governance, security reporting, issue forms, MIT license, changelog, and CI across Python 3.10-3.14.
- [ ] Run README commands exactly as documented and fix any mismatch.
- [ ] Run `rg -n "TB[D]|TO[DO]|coming so[o]n" .` and resolve every documentation placeholder.
- [ ] Commit with `docs: prepare bilingual open source release`.

### Task 8: Packaging and Release Verification

**Files:**
- Modify only files whose verification exposes a concrete defect.

**Interfaces:**
- Produces: installable sdist/wheel, clean test run, working console script, and public GitHub repository.

- [ ] Run `python -m unittest discover -s tests -v` and verify zero failures and warnings.
- [ ] Run `python -m compileall -q src examples`.
- [ ] Run `python -m pip install --upgrade build` only if `python -m build` is unavailable, then build sdist and wheel.
- [ ] Create a temporary virtual environment, install the wheel, run `seed-society demo`, and inspect its event and status output.
- [ ] Review `git diff --check`, repository status, package contents, and secrets scan.
- [ ] Commit any verification fixes with focused messages.
- [ ] Create the public GitHub repository `woshishadowhunter/seed-society`, push `main`, and verify GitHub Actions.
- [ ] Tag `v0.1.0` only after local and remote verification both pass.
