# Security Policy

## Supported versions

Security fixes are applied to the latest release on `main`.

## Reporting a vulnerability

Do not open a public issue for vulnerabilities, leaked credentials, or prompt/data exposure. Use GitHub's private vulnerability reporting feature on this repository. Include affected versions, reproduction steps, impact, and any proposed mitigation.

You should receive an acknowledgement within seven days. We will validate the report, coordinate a fix and disclosure timeline, and credit reporters who want attribution.

## Operational guidance

- Keep model credentials in environment variables or a secret manager.
- Treat model responses, memory entries, and JSON goal specs as untrusted input.
- Review tool permissions before connecting workers to external systems.
- Use bounded attempts and actions for every production run.
- Do not store personal or confidential data in demonstration databases.
- Protect the SQLite database as an operator trust boundary; policy, deployment, attestation, and decision records are not externally signed.
- Keep PostgreSQL credentials in a secret manager, require encrypted transport outside a trusted private network, and grant the runtime role access only to its intended database and schema.
- Treat `--postgres-schema` as a namespace, not an authorization or tenant-isolation boundary. Use separate credentials and databases where trust boundaries differ.
- Run the official A2A TCK outside the runtime at a reviewed pinned revision, preserve its artifact, and import only the bounded compatibility JSON.
- Treat TCK source revision and tool version fields as operator provenance, not cryptographic proof.
- Run `agent-society a2a doctor` before enabling remote production traffic and `agent-society a2a self-test` after transport changes.
- Run `agent-society scheduler self-test` after changing SQLite, claim, or recovery code.
- Give every worker process a fresh session ID; a restarted worker must not reuse a predecessor session.
- Renew leases before their deadline and treat `StaleClaim` as a terminal loss of local write authority.
- Run expiry recovery with an explicit trusted UTC time and review tasks blocked for unsafe remote delegation state.
- Synchronize and monitor worker clocks. Lease, renewal, commit, and reap decisions currently use explicit UTC timestamps supplied by trusted processes rather than exclusively using the database server clock.
- Keep a scheduler SQLite database on a local filesystem. WAL shared-memory coordination does not support clients on different hosts or a network filesystem.
- Treat fencing as repository-write protection, not exactly-once execution. External services must enforce an idempotency key or fencing epoch when duplicate side effects are unacceptable.
- Keep all state for one queued run in one authority. Do not combine PostgreSQL claims with SQLite goals or outcomes, and do not copy execution records between schemas while workers are active.
- Review pending approvals before resolving them. Approval IDs, worker IDs, session IDs, and fencing tokens identify state but are not authentication credentials.

## Scheduler threat boundary

The SQLite scheduler assumes one trusted host, a protected database file, and a sufficiently stable UTC clock. The PostgreSQL scheduler assumes trusted database authentication, protected network transport, one authoritative schema, and synchronized worker clocks. Worker IDs and session IDs separate process generations but are not authentication credentials. An operator or process able to modify the authoritative database directly remains inside the trust boundary.

A stale worker can continue computing after lease expiry. It cannot commit through `commit_claim_outcome`, but a model request, tool call, filesystem write, or HTTP request performed before rejection may already have had an effect. Adapters for those systems must use their own idempotency and authorization controls.

PostgreSQL v0.9 is an execution-plane backend. It does not store A2A governance, evaluation, publication, or maintenance state. Those workflows remain on SQLite and must not be partially dual-written into PostgreSQL.

