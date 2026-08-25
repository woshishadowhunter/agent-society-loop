# Champion/Challenger Evaluation

Version 0.5 separates evidence collection, recommendation, and production
authority.

1. A benchmark fixes case IDs, task type, inputs, context, acceptance criteria,
   and criticality. Its canonical JSON produces a SHA-256 digest.
2. Champion and challenger identities include both agent and model IDs.
3. Every candidate/case outcome is persisted before aggregate metrics are
   computed.
4. Passing all gates creates a recommendation only.
5. An operator explicitly promotes the run. Promotion verifies candidate
   identity and rejects a stale champion before atomically updating deployment.

Default gates require at least five cases, no critical challenger failure, no
pass-rate loss, at least two mean-score points of improvement, no case regressing
by more than ten points, and challenger p95 latency no greater than 1.5 times the
champion.

## Reproducible CLI workflow

The CLI format records already observed scores, pass/fail decisions, and
latencies. Evaluation does not call a model and is therefore replayable.

```bash
seed-society evaluate examples/evaluation-spec.json --db evolution.db --json
seed-society evaluations RUN_ID --db evolution.db --json
seed-society promote RUN_ID --by operator --db evolution.db --json
seed-society deployments --db evolution.db --json
```

Use a separate, trusted evaluation pipeline to produce the observed results.
Do not place secrets or sensitive model output in benchmark files. Task types
without an active deployment keep performance-weighted routing. Once a task type
has a deployment, an unavailable or identity-mismatched champion blocks the goal
instead of falling back to an unapproved agent.
