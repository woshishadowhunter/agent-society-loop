# JSON Goal Specification

`agent-society run` accepts a UTF-8 JSON object.

## Goal fields

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `goal_id` | string | no | Stable ID; generated when omitted |
| `title` | string | yes | Short goal name |
| `description` | string | yes | Concrete desired outcome |
| `max_actions` | integer | no | Whole-run attempt budget; default `100` |
| `tasks` | array | yes | Non-empty task graph |

## Task fields

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `task_id` | string | yes | Unique within the goal |
| `task_type` | string | yes | Specialist and performance category |
| `description` | string | yes | Work to perform |
| `assigned_role` | string | no | Required role; default `worker` |
| `acceptance_criteria` | object | no | Structured checks described below |
| `dependencies` | string array | no | Task IDs that must succeed first |
| `output` | string | yes | Initial deterministic artifact |
| `repair_output` | string | no | Artifact used after failed review |
| `max_attempts` | integer | no | Per-task attempt limit; default `3` |
| `position` | integer | no | Stable ordering among ready tasks |

## Built-in criteria

| Criterion | Type | Check |
| --- | --- | --- |
| `required_terms` | string array | Every term appears case-insensitively |
| `required_sections` | string array | Every `## Section Name` heading appears |
| `min_sources` | integer | Counts `[Source: ...]` markers |
| `min_length` | integer | Minimum character count |

Any failed category creates a structured defect. A task passes only when no defect remains and its review score meets the engine threshold.

## Complete example

See [`examples/goal-spec.json`](../examples/goal-spec.json). Validate it by running:

```bash
agent-society run examples/goal-spec.json --db evidence.db --json
agent-society events evidence-brief-demo --db evidence.db
```

Task IDs must be unique, dependencies must exist, and the dependency graph must be acyclic. Invalid plans fail before any worker executes.

