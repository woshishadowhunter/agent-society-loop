# Production Runbook

This runbook defines the minimum operating procedure for Seed Society
v1.0. It assumes PostgreSQL for multi-host execution and an operator-managed
OpenAI-compatible model endpoint.

## 1. Preflight

Install the package and run the product readiness check:

```bash
python -m pip install seed-society
seed-society product self-test --json
```

The report must return `"passed": true`. It proves the deterministic goal loop,
SQLite scheduler safety kernel, A2A failure-safety campaign, release metadata,
and required operator documentation are present.

For a local checkout, run the stricter developer gate:

```bash
python -W error::ResourceWarning -m unittest discover -s tests -q
python -m compileall -q src tests examples
```

## 2. Database Authority

Use one database authority per run. Do not split claims, outcomes, approvals,
and inspection commands across SQLite and PostgreSQL.

SQLite is supported for local single-host operation:

```bash
seed-society enqueue examples/goal-spec.json --db society.db --json
seed-society worker run --worker-id worker-a \
  --agent-id spec-research --agent-id spec-writing \
  --max-tasks 3 --db society.db --json
```

PostgreSQL is the production choice for multi-host workers:

```bash
export SEED_SOCIETY_DATABASE_URL="postgresql://user:password@db.example/agents"
seed-society enqueue examples/goal-spec.json --postgres-schema agent_society --json
seed-society worker run --worker-id worker-a \
  --agent-id spec-research --agent-id spec-writing \
  --max-tasks 3 --postgres-schema agent_society --json
```

Run PostgreSQL migrations implicitly through the first command for the selected
schema. Use separate schemas for staging and production.

## 3. Model Workers

Keep bearer tokens in environment variables, not in JSON config:

```bash
export LOCAL_MODEL_API_KEY="replace-with-a-model-token"
seed-society model doctor examples/local-model-agents.json --json
```

Only start workers after the doctor passes. The worker records endpoint-bound
model identities and fails closed if an existing agent ID changes model or task
ownership.

```bash
seed-society worker run --worker-id model-worker-a \
  --model-config examples/local-model-agents.json \
  --postgres-schema agent_society --json
```

Run workers under a process supervisor. `SIGINT` and `SIGTERM` stop new claims
and drain the active claim, but they do not forcibly cancel arbitrary provider
code below Python.

## 4. Health and Recovery

Use bounded health and metrics snapshots for operations:

```bash
seed-society health --postgres-schema agent_society --json
seed-society metrics --postgres-schema agent_society --json
seed-society scheduler workers --postgres-schema agent_society --json
seed-society scheduler claims --postgres-schema agent_society --json
```

If a worker dies, expired local work can be recovered explicitly:

```bash
seed-society scheduler reap \
  --at 2026-07-17T00:00:00Z \
  --postgres-schema agent_society --json
```

Ambiguous or interrupted remote A2A work blocks instead of replaying an unsafe
submission. Inspect the delegation before taking operator action:

```bash
seed-society a2a delegations --db society.db --json
```

## 5. Outbox Delivery

Side-effect intents commit only with passing worker outcomes. Dispatch them with
a topic-bound worker:

```bash
export WEBHOOK_TOKEN="replace-with-a-webhook-token"
seed-society outbox dispatch \
  --worker-id webhook-a \
  --topic webhook \
  --webhook-url https://integrations.example/events \
  --token-env WEBHOOK_TOKEN \
  --watch --postgres-schema agent_society --json
```

Receivers must enforce the supplied `Idempotency-Key`. The runtime provides
fencing and retry evidence, but it cannot make a non-transactional external
system exactly once.

Inspect and prune bounded history:

```bash
seed-society outbox list --status failed --postgres-schema agent_society --json
seed-society outbox purge \
  --before 2026-06-01T00:00:00Z \
  --limit 1000 --postgres-schema agent_society --json
```

## 6. Backup and Upgrade

Before upgrading:

1. Stop dispatchers.
2. Stop workers and wait for active claims to drain.
3. Back up PostgreSQL or the SQLite database files.
4. Install the new package.
5. Run `seed-society product self-test --json`.
6. Run `seed-society health --json` against the target database.
7. Restart workers, then dispatchers.

Do not reuse a production schema for experiments with model identities,
delegation policies, or benchmark candidates. Use a separate schema or database.

## 7. Production Boundaries

- The project is a runtime and CLI, not a hosted web control plane.
- PostgreSQL covers the multi-host execution plane and outbox. A2A governance,
  evaluation, guarded maintenance, and publication remain SQLite workflows.
- Model endpoints, secrets, TLS, process supervision, and infrastructure
  monitoring are operator responsibilities.
- Direct model, tool, HTTP, filesystem, and webhook effects need idempotent
  adapters or remote fencing when replay would be unsafe.
