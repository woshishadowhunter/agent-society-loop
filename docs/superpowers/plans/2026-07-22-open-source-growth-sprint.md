# Open Source Growth Sprint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve discovery, first-run conversion, distribution, evidence, and contributor entry points across the three published repositories.

**Architecture:** Keep product behavior unchanged and add adoption surfaces around each existing product. Repository-specific PRs isolate risk; GitHub settings and community issues are applied only after code and documentation are available on the default branch.

**Tech Stack:** Python packaging with setuptools and PyPA actions, GitHub Actions, Markdown, Node.js validation scripts, GitHub Discussions and Issues.

## Global Constraints

- Do not change product runtime behavior in this sprint.
- Do not overwrite or merge unrelated local branches.
- Use PyPI Trusted Publishing; do not store an upload token.
- Publish only reproducible results and label unmeasured comparisons as future work.
- Use synthetic data only for Lianxin demonstrations.

---

### Task 1: Agent Society Loop adoption surface

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `pyproject.toml`
- Create: `.github/workflows/publish.yml`
- Create: `docs/benchmark.md`
- Create: `tests/test_packaging.py`

**Interfaces:**
- Consumes: existing `agent-society demo`, `product self-test`, and `evaluate` commands.
- Produces: a validated wheel, Trusted Publishing workflow, concise quick start, and reproducible evidence guide.

- [ ] Write packaging tests that require canonical project URLs, package version alignment, and the publish workflow's OIDC permissions.
- [ ] Run `python -m unittest tests.test_packaging -v` and confirm it fails because the workflow and metadata are absent.
- [ ] Add the workflow and metadata, then revise both README introductions and add the evidence guide.
- [ ] Run the focused test, full suite, build, metadata check, fresh-wheel smoke test, and deterministic self-test.
- [ ] Commit the Agent Society Loop changes.

### Task 2: Alaya Protocol adoption surface

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `pyproject.toml`
- Replace: `LICENSE`
- Create: `.github/workflows/publish.yml`
- Create: `docs/integrations.md`
- Create: `tests/test_packaging.py`

**Interfaces:**
- Consumes: existing `alaya plant`, `reinforce`, and `activate` commands.
- Produces: Apache-2.0 metadata recognized by GitHub, a validated wheel, Trusted Publishing workflow, and adapter guidance.

- [ ] Write packaging tests for version consistency, full Apache license markers, project URLs, and OIDC publishing permissions.
- [ ] Run `python -m unittest tests.test_packaging -v` and confirm the expected failures.
- [ ] Correct package metadata and license, add publishing and integration docs, and simplify both README introductions.
- [ ] Run focused and full tests, compile, build, metadata check, and a fresh-wheel CLI lifecycle.
- [ ] Commit the Alaya changes.

### Task 3: Lianxin evaluation surface

**Files:**
- Create: `package.json`
- Create: `scripts/validate-project.js`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/demo-mode.md`

**Interfaces:**
- Consumes: source JavaScript/JSON files and `scripts/generate-demo-data.js`.
- Produces: `npm test`, `npm run demo:data`, and an explicit privacy-safe evaluation path.

- [ ] Add a Node test runner that validates JavaScript syntax, JSON parsing, and generated demo-data structure.
- [ ] Run `npm test` and confirm failure before the root package and validator exist.
- [ ] Add the package scripts, validator, CI command, and concise bilingual evaluation instructions.
- [ ] Run `npm test` and `npm run demo:data`, then inspect the generated data for synthetic identifiers only.
- [ ] Commit the Lianxin changes.

### Task 4: GitHub community and release operations

**Files:**
- No product files beyond Tasks 1-3.

**Interfaces:**
- Consumes: three tested and pushed `codex/growth-sprint` branches.
- Produces: three ready PRs, enabled Discussions, scoped contributor issues, and repository metadata aligned with merged content.

- [ ] Push each branch and create one ready PR per repository.
- [ ] Wait for and verify every required GitHub Actions check.
- [ ] Merge each PR only after its checks pass and verify the default branch.
- [ ] Enable Discussions and create labeled, acceptance-criteria-driven newcomer issues.
- [ ] Configure GitHub `pypi` environments and document the one-time PyPI Trusted Publisher account step if project ownership is unavailable.
- [ ] Recheck repository metadata, release/package state, and community URLs through the GitHub API.
