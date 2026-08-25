# Agent Genome Experience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add v1.1 agent genome and experience distillation primitives.

**Architecture:** Add focused domain records, repository persistence, a deterministic distiller service, bounded context injection, and CLI commands. Do not alter task execution or promotion semantics.

**Tech Stack:** Python 3.10+, standard library dataclasses/json/hashlib, SQLite, PostgreSQL adapter parity, unittest.

## Global Constraints

- No new runtime dependencies.
- TDD: tests fail before production changes.
- Distilled experience must not alter criteria, budgets, deployments, or policy.
- Keep all records bounded and deterministic.

---

### Task 1: Domain and Persistence

**Files:**
- Modify: `src/seed_society/domain.py`
- Modify: `src/seed_society/storage.py`
- Modify: `src/seed_society/postgres_storage.py`
- Test: `tests/test_experience.py`

**Interfaces:**
- `AgentSelfModel`
- `AgentGenome.create(...)`
- `ExperienceRecord.create(...)`
- repository methods: `save_agent_genome`, `get_agent_genome`, `list_agent_genomes`, `save_experience`, `list_experience`

- [ ] Write failing tests for genome validation, persistence, and idempotent experience save.
- [ ] Implement domain records and SQLite persistence.
- [ ] Add PostgreSQL parity methods.
- [ ] Run targeted tests.

### Task 2: Experience Distiller and Context Injection

**Files:**
- Create: `src/seed_society/experience.py`
- Modify: `src/seed_society/memory.py`
- Test: `tests/test_experience.py`
- Test: `tests/test_memory.py`

**Interfaces:**
- `ExperienceDistiller.distill_goal(goal_id: str) -> list[ExperienceRecord]`
- `MemoryManager.build_context(...)[ "experience" ]`

- [ ] Write failing tests for extracting success/failure lessons and injecting relevant experience.
- [ ] Implement deterministic bounded distillation.
- [ ] Run targeted tests.

### Task 3: CLI and Documentation

**Files:**
- Modify: `src/seed_society/cli.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/architecture.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- `seed-society genome set AGENT_ID FILE --db DB --json`
- `seed-society genome show AGENT_ID --db DB --json`
- `seed-society experience distill GOAL_ID --db DB --json`
- `seed-society experience list [--agent-id ID] [--task-type TYPE] --db DB --json`

- [ ] Write failing CLI tests.
- [ ] Implement parser and command handlers.
- [ ] Update docs and changelog.
- [ ] Run full release verification.
