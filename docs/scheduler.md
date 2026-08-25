# Scheduler And Worker Safety

Version 0.10 adds database-authoritative worker time and a transactional outbox to the runnable worker execution plane. A coordinator can persist a validated task graph without executing it; independent worker processes then discover ready tasks, maintain renewable ownership, execute outside database transactions, and commit reviewed outcomes and optional side-effect intents atomically.

## Execution Flow

```text
enqueue -> running goal + pending task DAG
worker session -> claim next ready task -> running task
lease maintainer -> heartbeat + renew on a second connection
worker -> execute -> review -> fenced atomic commit
```

Use SQLite for multiple processes on one host:

```bash
seed-society enqueue examples/goal-spec.json --db society.db --json
seed-society worker run --worker-id local-a \
  --agent-id spec-research --agent-id spec-writing \
  --max-tasks 3 --db society.db --json
```

Use PostgreSQL for workers on different hosts:

```bash
python -m pip install -e ".[postgres]"
export SEED_SOCIETY_DATABASE_URL="postgresql://user:password@db.example/agents"
seed-society enqueue examples/goal-spec.json --postgres-schema agent_society --json
seed-society worker run --worker-id worker-a \
  --agent-id spec-research --agent-id spec-writing \
  --max-tasks 3 --postgres-schema agent_society --json
```

Every process participating in one run must use the same backend and database authority. Do not put claims in PostgreSQL while keeping task outcomes in SQLite.

## Safety Invariants

1. One task has at most one active claim.
2. A claim binds one worker process generation (`worker_id` plus `session_id`) and one selected Agent.
3. Every replacement claim receives a task-local fencing token greater than all earlier tokens.
4. Renewal, release, approval pause, and outcome commit require the exact live identity before lease expiry.
5. A stale transaction writes no artifact, review, attempt, performance, event, task, goal, or approval transition.
6. No database transaction remains open while model, tool, filesystem, or network work runs.
7. An approval pause consumes no attempt: approval persistence, claim release, task reset, goal pause, and audit event share one fenced transaction.
8. Optional outbox intents from a passing review share the fenced outcome transaction and use independent leased delivery with monotonic tokens; failed review never publishes them.

`WorkerSession` identifies one process generation. A restart must use a fresh `session_id`; registering it under the same `worker_id` supersedes the previous generation. `TaskClaim` records the owner, selected Agent, fencing token, lease timestamps, and terminal state, and remains inspectable after commit, release, or expiry.

## Ready-Task Discovery

SQLite serializes discovery with a short `BEGIN IMMEDIATE` transaction. PostgreSQL locks candidate task rows with `FOR UPDATE SKIP LOCKED`, allowing competing consumers to skip work already being claimed. Both implementations recheck the live session, running goal, pending task, succeeded dependencies, task-type capability, and absence of another active claim before changing the task to `running`.

Candidate order is deterministic: `(goal_id, position, task_id)`. Database locks are released before execution starts.

## Lease Maintenance And Shutdown

The worker starts a daemon lease maintainer on an independent repository connection. It renews both the worker heartbeat and claim lease while foreground execution continues. Before commit, the foreground connection performs one final heartbeat and renewal. If either path detects a superseded session, expired lease, or replaced claim, the process returns `lost` and persists none of its local output.

`SIGINT` and `SIGTERM` set a stop event. The worker does not claim more work, but it drains the current claim before exiting. Python cannot safely force-cancel an arbitrary worker call; adapters still need their own timeout and cancellation controls.

## Outcome And Goal Reconciliation

`commit_claim_outcome` revalidates exact ownership and atomically writes the optional artifact, review, attempt, task-type performance, audit events, optional outbox intents, final task state, terminal goal transition when applicable, and committed claim. A failed review may retain its draft artifact while returning the task to `pending`. A successful task links only its accepted artifact.

The synchronous `LoopEngine` and legacy per-record write methods fail closed when an active scheduler claim owns a task. Scheduler-managed outcomes must use the fenced APIs.

## Approval Pause

`ApprovalRequired` does not become a failed attempt. `pause_claim_for_approval` atomically persists or validates the exact approval request, releases the claim, returns the task to `pending`, pauses the goal, and emits `approval.requested`. An operator can inspect and resolve it:

```bash
seed-society approvals GOAL_ID --db society.db --json
seed-society approve APPROVAL_ID --by operator --db society.db --json
```

For PostgreSQL, supply `--database-url` or `SEED_SOCIETY_DATABASE_URL` and the same `--postgres-schema`. Approval resumes the goal to `running` in the same transaction, so a daemon can claim the pending task again. Rejection moves the goal to `failed`.

## Expiry Recovery

Recovery is explicit and auditable:

```bash
seed-society scheduler reap \
  --at 2026-07-17T00:00:10+00:00 \
  --db society.db --json
```

Ordinary expired work returns to `pending` and can receive a higher-token claim. SQLite also inspects its durable A2A delegation state: `submitting`, `unknown`, or `interrupted` blocks the task instead of risking a duplicate remote submission; `accepted` and `completed` remain resumable under the no-resend rule. PostgreSQL v0.10 covers local execution-plane and outbox recovery only and does not store A2A governance or delegation state.

Production worker sessions, claims, renewals, and outcome commits use the selected repository's database clock. Explicit UTC timestamps remain part of the storage contract for deterministic conformance campaigns and operator-directed recovery.

## Backend Scope

| Capability | SQLite | PostgreSQL v0.10 |
| --- | --- | --- |
| Synchronous `LoopEngine` and full governance workflows | Yes | No |
| `enqueue`, worker run, status, events, agents | Yes | Yes |
| Worker sessions, claims, leases, fencing | Same-host | Multi-host |
| Artifacts, reviews, attempts, performance, goal reconciliation | Yes | Yes |
| Approval pause, resolution, and traces | Yes | Yes |
| Transactional outbox and leased delivery | Same-host | Multi-host |
| A2A governance, evaluation, publication, maintenance | Yes | No |
| A2A-aware expiry recovery | Yes | No |

The PostgreSQL adapter uses JSONB payloads plus normalized indexed scheduler columns. It is an execution-plane authority, not a partial dual-write bridge.

## Verification And Threat Boundary

```bash
seed-society scheduler self-test --json
```

The deterministic SQLite campaign proves exclusive claim, exact-owner renewal, monotonic takeover, stale-commit rejection with zero partial writes, and complete current-owner commit. CI runs the shared claim, dependency, ownership, expiry, session-generation, terminal reconciliation, and approval-pause contracts against PostgreSQL 17.

SQLite databases must stay on a local filesystem; WAL shared-memory coordination does not support cross-host consumers or network filesystems. See the official [SQLite WAL documentation](https://www.sqlite.org/wal.html) and [transaction documentation](https://www.sqlite.org/lang_transaction.html). PostgreSQL ready discovery follows the official [`SELECT` locking and `SKIP LOCKED` semantics](https://www.postgresql.org/docs/current/sql-select.html) and still depends on transactional isolation described in the [PostgreSQL transaction isolation documentation](https://www.postgresql.org/docs/current/transaction-iso.html).

Fencing protects writes through this repository. `WorkerExecution` may include outbox intents so the task outcome and delivery intent commit atomically. Each dispatcher is bound to one topic and renews its delivery lease through an independent connection while the handler runs. Outbox delivery sends an idempotency key and monotonic delivery token, but it still cannot make a non-participating receiver exactly-once. Direct model requests, tool calls, HTTP requests, and filesystem mutations require adapter-level idempotency or a remotely enforced fencing epoch.

---

# 调度与 Worker 安全

v0.10 在 worker 执行平面上增加了数据库权威时间和事务 Outbox。协调器只规划并持久化任务图；独立 worker 发现就绪任务、维护可续租所有权、在数据库事务外执行，最后原子提交经过质检的结果和可选副作用意图。

## 执行流程

```text
enqueue -> running 目标 + pending 任务图
worker session -> 领取下一个就绪任务 -> running 任务
租约维护线程 -> 用第二连接 heartbeat + renew
worker -> 执行 -> 质检 -> fenced 原子提交
```

SQLite 用于单机多进程，PostgreSQL 用于跨主机 worker。参与同一次运行的所有进程必须连接同一个后端和同一个权威数据库，不能把 claim 放在 PostgreSQL、结果放在 SQLite。

## 七项不变量

1. 一个任务最多只有一个 active claim。
2. Claim 同时绑定 `worker_id`、本次进程 `session_id` 和选定 Agent。
3. 同一任务每次重新领取都会获得更大的 fencing token。
4. 续租、释放、审批暂停和结果提交都要求租约到期前的精确身份。
5. 旧身份被拒绝时，不留下任何产物、质检、attempt、绩效、事件、任务、目标或审批状态的局部写入。
6. 模型、工具、文件系统和网络执行期间不持有数据库事务。
7. 审批暂停不消耗 attempt；审批、claim 释放、任务复位、目标暂停和事件在一个 fenced 事务内完成。

## 领取、租约与提交

SQLite 使用短 `BEGIN IMMEDIATE` 事务；PostgreSQL 使用 `FOR UPDATE SKIP LOCKED` 锁定候选任务行。两者都会重新检查 session、目标、任务、依赖、能力映射和 active claim，并按 `(goal_id, position, task_id)` 确定顺序。数据库锁在实际执行前释放。

worker 用独立数据库连接维护 heartbeat 和 lease，提交前再由前台连接做一次最终续租。session 被替换、lease 过期或 claim 已被接管时，本地结果会被丢弃。`SIGINT`/`SIGTERM` 只停止新任务发现，当前 claim 会先排空；任意 Python 调用仍需适配器自己的超时和取消机制。

`commit_claim_outcome` 在一个事务中写入可选产物、质检、attempt、任务类型绩效、事件、任务终态、必要时的目标终态和 committed claim。同步 `LoopEngine` 与旧逐记录写入接口会拒绝绕过 active claim。

## 审批暂停

`ApprovalRequired` 不计作失败。`pause_claim_for_approval` 会原子持久化审批、释放 claim、让任务回到 `pending`、暂停目标并写 `approval.requested`。批准会在同一事务中把目标恢复为 `running`，常驻 worker 可再次领取；拒绝则把目标转为 `failed`。

## 过期恢复与后端范围

普通过期任务回到 `pending`，下一次领取使用更大的 token。SQLite 还会检查 A2A 委派：`submitting`、`unknown` 或 `interrupted` 必须阻塞，`accepted` 和 `completed` 可按不重发规则恢复。PostgreSQL v0.10 恢复本地执行任务和 Outbox 投递，不存储 A2A 治理和委派状态。

SQLite 支持完整同步引擎和治理流程，但多进程限制在同一主机；PostgreSQL 支持跨主机的目标、本地任务、产物、质检、attempt、绩效、事件、worker、claim、审批与 trace，不支持 A2A 治理、评测、发布和维护。它是单一执行平面权威，不是双写桥接层。

生产 worker 的 session、claim、续租和结果提交使用所选数据库的权威时间。显式 UTC 时间仍保留给确定性一致性测试和操作者发起的恢复命令。

## 验证与威胁边界

SQLite 自检验证独占领取、精确续租、单调接管、旧提交零残留拒绝和当前持有者完整提交；CI 在 PostgreSQL 17 上运行同一套任务发现、依赖、所有权、过期、session 代际、目标对账和审批暂停契约。

SQLite 数据库只能放在本地文件系统。PostgreSQL 的 `SKIP LOCKED` 解决跨主机任务和 Outbox 领取，但 fencing 仍只保护仓库写入。Outbox 可以让任务结果与副作用意图原子提交，并向接收端发送幂等键和 delivery token；接收端仍必须强制幂等。直接模型、工具、HTTP 或文件系统副作用需要适配器级幂等或远端 fencing epoch，PostgreSQL 本身不等于外部 exactly-once。
