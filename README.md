# Agent Society Loop

[简体中文](README.zh-CN.md) | English

An auditable Python runtime for organizing specialized AI agents around a goal. It implements goal lifecycle management, an outer planning loop, an inner execute-review-repair loop, durable memory, and performance-based agent selection.

The default demo is deterministic and needs no API key. You can inspect every task, review, artifact, routing decision, and state transition in SQLite.

## Why this project

Single-agent workflows often mix planning, execution, and evaluation in one opaque prompt. Agent Society Loop separates those responsibilities and makes their contracts observable:

- **One lifecycle:** goals move through explicit planning, running, success, failure, and blocked states.
- **Two loops:** the orchestrator advances the task graph; workers and reviewers repeat until criteria pass or budgets stop the run.
- **Three memories:** task feedback, seed knowledge, and task-specific agent performance persist in SQLite.
- **Four roles:** planner, specialist worker, reviewer, and memory manager communicate through typed interfaces.
- **Bounded autonomy:** retries and total actions are limited, acceptance criteria cannot be silently rewritten, and every decision emits an event.

## Quick start

Requires Python 3.10 or newer.

```bash
git clone https://github.com/woshishadowhunter/agent-society-loop.git
cd agent-society-loop
python -m pip install -e .
agent-society demo --db demo.db
```

Expected result:

```text
Goal quantum-mug-demo: succeeded (4/4 tasks, 1 retries)
```

Inspect what happened:

```bash
agent-society status quantum-mug-demo --db demo.db --json
agent-society events quantum-mug-demo --db demo.db
agent-society agents --db demo.db --json
```

The bundled scenario deliberately produces an incomplete first market report. The reviewer rejects it, the defect enters short-term memory, and the specialist repairs the report on its second attempt.

## Inspect or implement a GitHub issue with real model agents

The default workflow remains read-only. Configure an OpenAI-compatible endpoint and run a reviewed maintenance intake:

```bash
export MODEL_API_KEY="..."
export MODEL_ID="your-model"
agent-society maintain owner/repository 123 --workspace . --db maintain.db --json
agent-society traces maintain-owner-repository-123 --db maintain.db --json
```

Version 0.3 also provides an opt-in guarded execution mode. Checks are configured by the operator and selected by name; the model cannot provide shell text:

```bash
agent-society maintain owner/repository 123 \
  --workspace . --db ../maintain.db --apply \
  --check "tests=python -m unittest discover -s tests -v" --json
```

The run pauses before each content-addressed write and named check. Approve the displayed request with `agent-society approve APPROVAL_ID --by NAME --db ../maintain.db`, then repeat the same `maintain` command to resume. Writes are atomic, stale hashes are rejected, checks are bounded and shell-free, and pre-change content is durably recoverable. A deterministic reviewer gate rejects PASS unless every configured check passed against the current workspace digest. This mode still does not commit, push, or create a pull request.

Version 0.4 can publish the verified result from an allowed feature branch. Keep the database outside the workspace and provide a GitHub token:

```bash
export GITHUB_TOKEN="..."
agent-society maintain owner/repository 123 \
  --workspace . --db ../maintain.db --apply \
  --check "tests=python -m unittest discover -s tests -v" \
  --publish --base main --remote origin --branch-prefix "agent-society/" --json
```

Publication has its own exact approval containing the base HEAD, branch policy, changed paths, workspace digest, checks, title, and final PR body. It stages only goal-owned paths and resumes idempotently through commit, push, and PR creation. It never merges or force-pushes.

## Evaluate and promote agent upgrades

Version 0.5 adds a reproducible champion/challenger gate. Evaluation persists every case result and produces a recommendation; it never changes production routing. Promotion is a separate operator action:

```bash
agent-society evaluate examples/evaluation-spec.json --db evolution.db --json
agent-society evaluations RUN_ID --db evolution.db --json
agent-society promote RUN_ID --by operator --db evolution.db --json
agent-society deployments --db evolution.db --json
```

The default policy requires at least five cases, no critical failure, no pass-rate loss, a mean-score gain, bounded per-case regression, and bounded p95 latency. Agent and model identities are rechecked at promotion. Once deployed, the approved champion is mandatory for that task type; an unavailable champion blocks instead of silently falling back. See [Evaluation and promotion](docs/evaluation.md).

External tools can be adapted from stable MCP `2025-11-25` stdio servers. Discovery is not authority: only tools with an operator-supplied local risk classification are registered, and every adapted call still uses the existing schema, approval, tracing, and budget controls. See [MCP tool integration](docs/mcp.md).

## Delegate to governed A2A specialists

Version 0.7 adds a trust control plane to the A2A `1.0` `HTTP+JSON` polling subset. Discovery and registration are not authority. Every new production delegation requires four independent gates: an exact promoted deployment, an active content-addressed policy, fresh passing official A2A TCK evidence, and explicit `run --allow-remote` opt-in.

```bash
agent-society a2a inspect-card https://agent.example/.well-known/agent-card.json --json
export ACME_A2A_TOKEN="..."
agent-society a2a register research-agent \
  https://agent.example/.well-known/agent-card.json \
  --sha256 CARD_SHA256 --interface https://agent.example/a2a \
  --skill research=deep-research --auth-env ACME_A2A_TOKEN --json

# Evaluate and promote the exact a2a:CARD_SHA256 identity first.
# Replace the digest in examples/a2a-policy.json with CARD_SHA256.
agent-society a2a policy validate examples/a2a-policy.json --json
agent-society a2a policy import examples/a2a-policy.json --db society.db --json
agent-society a2a policy activate research POLICY_DIGEST \
  --by operator --db society.db --json

# Run the pinned official TCK externally, then import its report.
agent-society a2a attestation import research-agent compatibility.json \
  --source-revision 5996b79f9cefa6fc390980e383e358a66fb9e49e \
  --tool-version 1.0.0 --db society.db --json
agent-society a2a doctor research-agent research --db society.db --json
agent-society a2a self-test --json
agent-society run examples/a2a-goal-spec.json --db society.db --allow-remote --json
agent-society a2a decisions remote-research-001 --db society.db --json
agent-society a2a delegations --db society.db --json
agent-society a2a cancel DELEGATION_ID --by operator --db society.db --json
```

Policy decisions are persisted before payload construction and network I/O. Policy ceilings can only reduce runtime context, request/result bytes, polls, and deadlines. Both ALLOW and DENY decisions remain inspectable. Existing accepted or completed delegations resume under their stored authority and are never resent because policy changed.

The runtime imports but never downloads or executes the TCK. The source revision and tool version are operator-supplied provenance, not a signature or trust root. Bearer values remain outside SQLite. See [Guarded A2A delegation](docs/a2a.md) for the external TCK procedure, policy schema, doctor checks, recovery rules, and threat boundary.

## Run durable worker processes

Version 0.9 separates planning from execution. `enqueue` persists a validated task graph, and one or more worker processes discover ready work, renew their leases on an independent database connection, review each result, and commit the complete outcome through a fencing token.

Run the bundled two-task specification on SQLite:

```bash
agent-society enqueue examples/goal-spec.json --db society.db --json
agent-society worker run --worker-id local-a \
  --agent-id spec-research --agent-id spec-writing \
  --max-tasks 3 --db society.db --json
agent-society status evidence-brief-demo --db society.db --json
```

SQLite supports multiple processes on one host. For multi-host workers, install the optional PostgreSQL adapter and point every coordinator, worker, and inspection command at one PostgreSQL authority:

```bash
python -m pip install -e ".[postgres]"
export AGENT_SOCIETY_DATABASE_URL="postgresql://user:password@db.example/agents"
agent-society enqueue examples/goal-spec.json --postgres-schema agent_society --json
agent-society worker run --worker-id worker-a \
  --agent-id spec-research --agent-id spec-writing \
  --max-tasks 3 --postgres-schema agent_society --json
agent-society status evidence-brief-demo --postgres-schema agent_society --json
```

PostgreSQL ready-task discovery uses row locking with `SKIP LOCKED`. Both backends enforce process generations, renewable leases, monotonic task-local fencing tokens, atomic outcome/goal reconciliation, and fenced approval pauses. A worker that loses its session or claim discards its local result. `SIGINT` and `SIGTERM` stop new discovery only after the active claim has drained.

Prove the SQLite ownership kernel and inspect live scheduler evidence:

```bash
agent-society scheduler self-test --json
agent-society scheduler workers --db society.db --json
agent-society scheduler claims --goal-id GOAL_ID --db society.db --json
agent-society scheduler reap --at 2026-07-16T00:00:10+00:00 --db society.db --json
```

The self-test opens two independent SQLite connections and proves five invariants: exclusive claim, exact-owner renewal, increasing token after takeover, zero-partial-write rejection of a stale worker, and complete commit by the current worker. The same backend-neutral conformance contracts run against PostgreSQL 17 in CI. SQLite expiry recovery preserves the v0.7 no-resend rules for A2A state; the PostgreSQL adapter deliberately covers the local execution plane, not A2A governance or remote-delegation recovery.

See [Scheduler safety](docs/scheduler.md) for worker lifecycle, backend scope, recovery, approval, and threat boundaries.

## Architecture

```mermaid
flowchart LR
    G[Goal lifecycle] --> O[Orchestrator / outer loop]
    O --> P[Planner]
    O --> S[Deployment gate / performance selector]
    S --> W[Specialist worker]
    W --> R[Reviewer / inner loop]
    R -->|FAIL + defects| W
    R -->|PASS| O
    M[(SQLite or PostgreSQL execution state)] --> O
    M --> S
    W --> M
    R --> M
    O --> E[(Append-only events)]
```

See [Architecture](docs/architecture.md) for state transitions, module boundaries, selection scoring, persistence, and failure behavior.

## Run your own goal specification

```bash
agent-society run examples/goal-spec.json --db my-goal.db --json
```

A task supplies an initial `output`, optional `repair_output`, dependencies, and structured acceptance criteria. This makes local experiments reproducible before connecting a model provider.

```json
{
  "goal_id": "brief-001",
  "title": "Create an evidence brief",
  "description": "Produce a reviewed artifact",
  "tasks": [
    {
      "task_id": "draft",
      "task_type": "writing",
      "description": "Write the draft",
      "acceptance_criteria": {"required_terms": ["evidence"]},
      "output": "A vague draft",
      "repair_output": "A clear recommendation supported by evidence"
    }
  ]
}
```

The complete format is documented in [Goal specification](docs/goal-spec.md).

## Commands

| Command | Purpose |
| --- | --- |
| `agent-society demo` | Run the offline quantum mug scenario |
| `agent-society run SPEC.json` | Execute a deterministic JSON task graph |
| `agent-society enqueue SPEC.json` | Plan and persist a task graph without executing it |
| `agent-society worker run ...` | Claim, renew, execute, review, and commit queued tasks |
| `agent-society status GOAL_ID` | Inspect goal, tasks, reviews, and artifacts |
| `agent-society events GOAL_ID` | Read the ordered audit trail |
| `agent-society agents` | Inspect agent profiles and performance |
| `agent-society evaluate SPEC.json` | Compare a challenger with a reproducible benchmark |
| `agent-society evaluations [RUN_ID]` | Inspect evaluation decisions and raw case outcomes |
| `agent-society promote RUN_ID --by NAME` | Explicitly promote a recommended challenger |
| `agent-society deployments` | Inspect active task-type champions |
| `agent-society scheduler workers` | Inspect durable worker sessions and expiry |
| `agent-society scheduler claims [--goal-id ID]` | Inspect lease and fencing-token history |
| `agent-society scheduler reap --at UTC` | Explicitly recover expired claims |
| `agent-society scheduler self-test` | Prove five local scheduler safety invariants |
| `agent-society a2a inspect-card URL` | Inspect and hash a bounded Agent Card |
| `agent-society a2a register ...` | Register an exact card, interface, and skill map |
| `agent-society a2a agents` | Inspect remote trust records |
| `agent-society a2a policy ...` | Validate, import, activate, list, or simulate delegation policy |
| `agent-society a2a attestation ...` | Import or list official TCK evidence |
| `agent-society a2a doctor AGENT TASK_TYPE` | Check eight production-readiness gates without sending work |
| `agent-society a2a self-test` | Run five local A2A failure-safety scenarios |
| `agent-society a2a decisions [GOAL_ID]` | Inspect durable ALLOW/DENY evidence |
| `agent-society a2a delegations [ID]` | Inspect durable remote execution state |
| `agent-society a2a cancel ID --by NAME` | Cancel a known remote task |
| `agent-society maintain OWNER/REPO ISSUE` | Produce a reviewed, read-only maintenance proposal |
| `agent-society maintain ... --apply --check NAME=COMMAND` | Apply approved local changes and run approved named checks |
| `agent-society maintain ... --publish` | Publish a verified allowed branch as an approved pull request |
| `agent-society traces GOAL_ID` | Inspect linked model and tool trace spans |
| `agent-society approvals GOAL_ID` | Inspect pending and resolved tool approvals |
| `agent-society approve APPROVAL_ID` | Approve a paused write or execute tool call |
| `agent-society reject APPROVAL_ID` | Reject a paused write or execute tool call |
| `agent-society knowledge add` | Add long-term seed knowledge |
| `agent-society knowledge search` | Retrieve relevant seed knowledge |

SQLite commands accept `--db`. Execution-plane commands also accept `--database-url` or `AGENT_SOCIETY_DATABASE_URL` plus `--postgres-schema`. Inspection commands and execution reports accept `--json`.

## Connect an OpenAI-compatible provider

The optional provider uses the Python standard library and does not persist or print its API key:

```python
import os

from agent_society_loop.providers import OpenAICompatibleProvider

provider = OpenAICompatibleProvider(
    api_key=os.environ["MODEL_API_KEY"],
    base_url=os.environ.get("MODEL_BASE_URL", "https://api.openai.com/v1"),
    model=os.environ["MODEL_ID"],
)

text = provider.complete([
    {"role": "system", "content": "Return a concise, evidence-based answer."},
    {"role": "user", "content": "Summarize the supplied research."},
])
```

Use `ModelPlanner`, `ModelWorker`, and `ModelReviewer` from `model_agents.py` when strict JSON role adapters are appropriate. `ModelWorker` accepts only one structured tool call or final artifact per turn, applies a separate tool-step budget, and delegates every action to the policy-controlled tool runtime.

## What self-evolution means here

After each reviewed attempt, the runtime updates performance for `(agent_id, task_type)`. Task types without an active deployment use success rate, review score, latency, and sample confidence. Agent upgrades can additionally be compared on an immutable benchmark and promoted through an explicit champion/challenger gate.

The runtime never approves its own mutations and does **not** rewrite prompts, acceptance criteria, or safety policy. Guarded maintenance may change the selected workspace only through exact, durable approvals. That boundary keeps changes reviewable and prevents a weak result from redefining what “good” means.

## Current boundaries

Version 0.9 provides a durable worker service and a PostgreSQL execution backend for multi-host task processing. It is not a complete control plane: PostgreSQL owns goals, local tasks, artifacts, reviews, attempts, performance, events, workers, claims, approvals, and traces, while A2A governance, evaluation, publication, and maintenance workflows remain on the SQLite path and must not be split across databases. The worker cannot forcibly cancel an arbitrary Python call; shutdown drains the current claim, and lease loss rejects the eventual repository commit. Runtime lease decisions use explicit UTC timestamps supplied by trusted worker processes, so production hosts still require clock synchronization. Fencing protects repository writes, not external exactly-once execution; model, tool, HTTP, and filesystem adapters need idempotency keys or a remotely enforced fencing epoch. MCP remains stable stdio only. A2A remains outbound `HTTP+JSON` polling only, without inbound service, streaming, webhooks, automatic resend, or automatic credential acquisition. Message-broker delivery, autoscaling, tenant isolation, and a web control plane remain future work.

## Development

```bash
python -m unittest discover -s tests -v
python -m compileall -q src examples
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Security issues should follow [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).

