# Production-oriented deployment

Version 0.10 supports a bounded local-model deployment with PostgreSQL as the
execution authority. It does not install or manage a model server. Start an
OpenAI-compatible endpoint separately and validate it before starting workers.

## Local process deployment

Create a runtime configuration from `examples/local-model-agents.json`. Provider
secrets are environment-variable references; never put bearer values in the
configuration file.

```bash
export LOCAL_MODEL_API_KEY="replace-with-a-model-token"
seed-society model doctor examples/local-model-agents.json --json
export SEED_SOCIETY_DATABASE_URL="postgresql://user:password@db/agents"
seed-society enqueue examples/goal-spec.json --postgres-schema agent_society --json
seed-society worker run \
  --worker-id local-model-worker \
  --model-config examples/local-model-agents.json \
  --postgres-schema agent_society --json
```

The worker registers the exact endpoint-and-model identity. Reusing an agent ID
with a changed identity fails closed instead of overwriting the durable profile.

## Docker Compose

Set a non-default database password, set the model endpoint bearer value, and
edit the example model name:

```bash
export POSTGRES_PASSWORD="replace-with-a-secret"
export SEED_SOCIETY_DATABASE_URL="postgresql://agent_society:URL_ENCODED_PASSWORD@postgres:5432/agent_society"
export LOCAL_MODEL_API_KEY="replace-with-a-model-token"
export WEBHOOK_URL="https://integrations.example/events"
export WEBHOOK_TOKEN="replace-with-a-webhook-token"
docker compose config
docker compose up -d postgres
docker compose --profile tools run --rm operator \
  model doctor /work/examples/local-model-agents.json --json
docker compose --profile tools run --rm operator \
  enqueue /work/examples/goal-spec.json --postgres-schema agent_society --json
docker compose --profile worker up -d worker
docker compose --profile dispatcher up -d dispatcher
```

`POSTGRES_PASSWORD` is the raw server password.
`SEED_SOCIETY_DATABASE_URL` is a separate client DSN whose password component
must be percent-encoded. Keeping them separate avoids corrupting URLs when the
raw password contains `@`, `:`, `/`, or `%`.

The example uses `host.docker.internal:11434` to reach a model server on the
host. Change `base_url` when the model endpoint runs elsewhere. HTTP is accepted
without a bearer token only for loopback endpoints; the container-to-host
example therefore requires `LOCAL_MODEL_API_KEY`, explicitly sets
`allow_insecure_http`, and should also be isolated by the deployment network.
Non-loopback production endpoints should use HTTPS and remove that opt-in.

## Operations

```bash
seed-society health --postgres-schema agent_society --json
seed-society metrics --postgres-schema agent_society --json
seed-society outbox list --postgres-schema agent_society --json
seed-society outbox dispatch \
  --worker-id webhook-a \
  --topic webhook \
  --webhook-url https://integrations.example/events \
  --token-env WEBHOOK_TOKEN \
  --watch --postgres-schema agent_society --json
seed-society outbox purge \
  --before 2026-06-01T00:00:00Z \
  --limit 1000 --postgres-schema agent_society --json
```

`health` reports database readiness separately from degraded operational state.
Expired active claims and terminal outbox failures mark the snapshot degraded.
`metrics` contains counts only and does not expose task text, model prompts, or
credentials.

Outbox handlers receive `Idempotency-Key` and
`X-Agent-Society-Delivery-Token`. A receiver must enforce the idempotency key;
the runtime cannot guarantee exactly-once behavior in an external system that
does not participate in the database transaction.

The dispatch lease defaults to 60 seconds for a 30-second HTTP timeout. The
runtime rejects configurations without at least a five-second margin between
the timeout and lease. A dispatcher also renews ownership on an independent
database connection while the handler is active, so a slow response cannot be
reclaimed by another topic consumer.

`outbox list` defaults to 100 records and accepts status filtering. Schedule
`outbox purge` according to the receiver's audit-retention policy; it only
deletes bounded batches of delivered or failed records older than `--before`.

## Remaining boundaries

- PostgreSQL remains the multi-host execution authority; governance and guarded
  maintenance workflows are still SQLite-only.
- The worker drains a running Python call on shutdown and cannot forcibly cancel
  arbitrary provider code.
- Model and webhook clients enforce wall-clock response deadlines and byte
  ceilings. Production model gateways should still enforce their own request
  deadline, and workers should run under a process supervisor for failures
  below the Python transport layer.
- The included counters are a bounded operational baseline, not a complete
  OpenTelemetry exporter or alerting stack.
- Tenant isolation, autoscaling, inbound A2A service, and a web control plane
  are not included.
