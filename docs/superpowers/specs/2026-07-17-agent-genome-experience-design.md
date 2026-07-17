# Agent Seed Genome and Experience Distillation Design

## Goal

Add a pragmatic evolution substrate to Agent Society Loop: each agent can carry a structured seed genome, and completed task attempts can be distilled into durable experience records that explain success and failure patterns without granting the system authority to rewrite its own safety rules.

## Concept Mapping

- Alaya-like base: the model runtime plus durable knowledge and experience stores.
- Seed: a structured `AgentGenome` describing role seed, self-model, traits, tool profile, memory profile, risk policy, and lineage.
- Manas-like self model: the agent's mission, success signal, and failure modes to avoid.
- Perfume/conditioning: `ExperienceRecord` entries distilled from reviewed attempts.
- Karma: performance, risk, and defect history attached to `(agent_id, task_type)`.
- Evolution: later versions can recombine genomes, but v1.1 only creates auditable material for that process.

## Scope

v1.1 implements:

- Domain records for `AgentGenome`, `AgentSelfModel`, and `ExperienceRecord`.
- SQLite and PostgreSQL persistence for genomes and experiences.
- `ExperienceDistiller`, a deterministic service that turns reviewed attempts into bounded lessons.
- CLI:
  - `agent-society genome set AGENT_ID FILE`
  - `agent-society genome show AGENT_ID`
  - `agent-society experience distill GOAL_ID`
  - `agent-society experience list [--agent-id ID] [--task-type TYPE]`
- Context injection: future tasks receive relevant distilled experience as a bounded `experience` context section.

v1.1 does not implement automatic genome recombination, mutation, autonomous promotion, or production routing changes. Those remain explicit future work.

## Data Model

`AgentGenome` is keyed by `agent_id`. It is independent from `AgentProfile` so existing profile compatibility stays stable.

`ExperienceRecord` is immutable and keyed by a deterministic digest of goal, task, attempt, agent, verdict, score, and defect text. Re-running distillation is idempotent.

## Safety Rules

- Distilled experience can guide context and explain routing, but it cannot change acceptance criteria, budgets, risk policy, or deployment records.
- A genome can describe tool tendencies but does not grant tool permissions; existing tool policy remains authoritative.
- Failed attempts are useful experience, but they cannot auto-promote or auto-disable an agent.
- All generated summaries are deterministic and bounded.

## Testing

Tests must prove:

- Genome validation rejects empty identity and unsafe risk policies.
- Experience distillation extracts success/failure lessons and defect tags.
- Re-running distillation does not duplicate records.
- `MemoryManager.build_context` includes relevant experience for the task type.
- CLI can set/show genome and distill/list experience.
