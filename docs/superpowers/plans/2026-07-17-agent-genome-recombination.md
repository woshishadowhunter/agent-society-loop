# Agent Genome Recombination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add v1.2 deterministic genome recombination so the system can create auditable child agent seed candidates from parent genomes and reviewed experience.

**Architecture:** Keep recombination advisory and explicit. Add focused domain/report records plus a new `GenomeRecombiner` service that reads existing genomes, performance, and experience, then saves a child genome through existing repository methods. Wire one CLI command and bilingual docs; do not alter routing, deployment, promotion, tool permissions, or safety policy.

**Tech Stack:** Python 3.10+, stdlib dataclasses/unittest/argparse, existing SQLite/PostgreSQL repositories, existing CLI.

## Global Constraints

- Use TDD for every production behavior.
- Recombination must be deterministic.
- Child genomes are candidates only and must not become active deployments.
- Child tool profile must be the intersection of parent tools.
- Child risk policy must be the strictest parent policy.
- Do not add third-party dependencies.

---

### Task 1: Domain Report and Recombiner Core

**Files:**
- Modify: `src/seed_society/domain.py`
- Create: `src/seed_society/evolution.py`
- Test: `tests/test_evolution.py`

**Interfaces:**
- Produces: `GenomeRecombinationReport`
- Produces: `GenomeRecombiner(repository).recombine(child_id: str, parent_ids: Sequence[str], task_type: str) -> GenomeRecombinationReport`

- [x] **Step 1: Write failing tests**

Add tests that create two parent genomes plus PASS/FAIL experience, then assert:
- child parents equal sorted parent IDs
- child generation is max parent generation + 1
- risk policy is strictest parent policy
- tool profile is parent tool intersection
- PASS experience becomes success signals
- FAIL experience becomes failure modes
- child is saved in repository

Run: `python -m unittest tests.test_evolution -v`
Expected: FAIL because `seed_society.evolution` does not exist.

- [x] **Step 2: Implement minimal domain/report and recombiner**

Add immutable `GenomeRecombinationReport` to `domain.py`.
Create `evolution.py` with deterministic parent loading, validation, risk ranking, tool intersection, experience filtering, and child save.

- [x] **Step 3: Verify tests pass**

Run: `python -m unittest tests.test_evolution -v`
Expected: PASS.

### Task 2: Rejection Paths and CLI Command

**Files:**
- Modify: `src/seed_society/cli.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_evolution.py`

**Interfaces:**
- CLI: `seed-society genome recombine CHILD_ID --parents A B --task-type TYPE --db DB --json`

- [x] **Step 1: Write failing tests**

Add tests for:
- missing parent rejection
- child overwrite rejection
- CLI recombine saves and returns child genome report JSON

Run: `python -m unittest tests.test_evolution tests.test_cli -v`
Expected: FAIL because rejection and CLI command are missing.

- [x] **Step 2: Implement minimal rejection and CLI wiring**

Import `GenomeRecombiner`; add `genome recombine` parser and handler.

- [x] **Step 3: Verify tests pass**

Run: `python -m unittest tests.test_evolution tests.test_cli -v`
Expected: PASS.

### Task 3: Docs, Version, and Product Verification

**Files:**
- Modify: `src/seed_society/__init__.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/architecture.md`
- Modify: `CHANGELOG.md`
- Test: `tests/test_storage.py`
- Test: `tests/test_readiness.py`

**Interfaces:**
- Version: `1.2.0`

- [x] **Step 1: Write failing version/readiness expectations**

Update tests expecting the public version and readiness self-test version to `1.2.0`.

Run: `python -m unittest tests.test_storage tests.test_readiness -v`
Expected: FAIL with version mismatch.

- [x] **Step 2: Update version and docs**

Document `genome recombine`, the candidate-only safety boundary, and v1.2 changelog.

- [x] **Step 3: Full verification**

Run:
- `python -W error::ResourceWarning -m unittest discover -s tests -q`
- `python -m seed_society.cli product self-test --json`
- `python -m compileall -q src tests examples`
- `python -m build`

Expected: all commands pass.
