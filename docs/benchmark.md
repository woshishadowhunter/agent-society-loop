# Reproducible Evidence

Seed Society separates install-time safety evidence from model-quality
benchmarks. The commands below are deterministic, offline, and require no API
key.

## Product self-test

```bash
seed-society product self-test --json
```

This checks release metadata, required operator documentation, one complete
execute-review-repair cycle, SQLite scheduler fencing invariants, and local A2A
failure-safety scenarios. A passing result means the installed runtime preserves
those contracts on the current machine. It does not measure LLM quality.

## Champion/challenger gate

Run the bundled evaluation fixture from a source checkout:

```bash
seed-society evaluate examples/evaluation-spec.json --db benchmark.db --json
seed-society evaluations --db benchmark.db --json
```

The fixture demonstrates the decision mechanics: immutable case outcomes,
critical-failure rejection, pass-rate and score gates, per-case regression
limits, and p95 latency limits. It uses deterministic candidate outputs so the
same policy decision can be reproduced without paying for a model call.

## What is not claimed

The bundled fixture is not evidence that this runtime outperforms LangGraph,
CrewAI, AutoGen, or a single-agent baseline. A fair public comparison needs a
shared task corpus, pinned models and prompts, repeated runs, cost accounting,
and published raw outcomes. That broader benchmark is intentionally tracked as
community work rather than presented as a finished result.

When publishing a benchmark, include:

- corpus revision and case count;
- model/provider identity and generation parameters;
- prompt and acceptance-criteria digests;
- success rate, review score, latency, token usage, and cost;
- every failed case and raw machine-readable outcome;
- enough commands for an independent rerun.
