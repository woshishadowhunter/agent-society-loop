# Agent Genome Recombination Design

## Goal

Add the next pragmatic evolution layer after v1.1: generate auditable child genome proposals from parent genomes, performance records, and distilled experience, without automatically deploying or granting authority to the child.

## Concept Mapping

- Parent genomes are the inherited seeds.
- Distilled experience is conditioning evidence from completed work.
- Recombination creates a child candidate with explicit lineage, generation, inherited traits, and experience-backed self-model refinements.
- The candidate is only a saved genome record. It is not active until normal evaluation and explicit promotion paths accept a matching agent/model identity.

## In Scope

- Deterministic `GenomeRecombiner` service.
- A `GenomeRecombinationReport` domain record explaining parents, child, inherited traits, supporting experience, and safety notes.
- Repository-neutral implementation using existing `list_agent_genomes`, `get_performance`, `list_experience`, and `save_agent_genome` methods.
- CLI command:
  - `agent-society genome recombine CHILD_ID --parents A B --task-type TYPE --db DB --json`
- Bilingual docs and version bump to `1.2.0`.

## Out of Scope

- Automatic route changes, promotion, deployment, policy mutation, tool permission changes, or source-code mutation.
- Stochastic genetic algorithms.
- Online learning loops that rewrite prompts without operator-visible artifacts.

## Safety Rules

- Parent count must be at least two.
- All parent genomes must already exist.
- The child ID must not overwrite an existing genome unless the operator passes an explicit overwrite flag in a future version; v1.2 does not expose overwrite.
- Child risk policy is the strictest parent policy by rank.
- Child tool profile is the intersection of parent tools, never the union.
- Child generation is `max(parent.generation) + 1`.
- Child self-model lessons come only from reviewed PASS experience with score at least 80 for the selected task type.
- Failed experience can influence failure modes but cannot add traits or tools.

## Acceptance Criteria

- Recombiner creates deterministic child genomes with parent lineage and strict risk/tool inheritance.
- Recombiner rejects missing parents and child overwrite.
- Recombiner uses high-scoring PASS experience as success signals and failed experience as failure modes.
- CLI can recombine genomes and show the saved child.
- Docs describe that recombination creates candidates only, not active deployments.
