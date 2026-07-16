# Scheduler Safety

Version 0.8 supplies the ownership and persistence kernel needed before Agent Society Loop can run task execution in multiple worker processes. The bundled implementation is safe for processes sharing a local SQLite database on one host. It is not a distributed scheduler.

## Invariants

1. One task has at most one active claim.
2. A claim binds one worker process generation (`worker_id` plus `session_id`) and one selected specialist Agent.
3. Every new claim for a task receives a fencing token larger than every previous token for that task.
4. Renewal, release, and outcome commit require the exact active identity before the lease deadline.
5. A stale outcome transaction writes nothing.
6. No database transaction remains open while model, tool, filesystem, or network work runs.

## Records

`WorkerSession` is a process-generation record. Starting or restarting a worker requires a fresh `session_id`; registering it under the same `worker_id` supersedes the previous session. Heartbeats from the previous session fail closed.

`TaskClaim` records the worker/session, selected `agent_id`, task-local `fencing_token`, acquisition/renewal/deadline timestamps, and terminal state. Claim history remains inspectable after commit, release, or expiry.

The SQLite schema is additive. Opening a v0.7 database creates `scheduler_workers`, `task_claim_fences`, and `task_claims` without rewriting existing goal, task, A2A, policy, or evidence payloads.

## Claim And Execute

Claim acquisition uses a short `BEGIN IMMEDIATE` transaction. It revalidates that the exact worker session is live, the goal is running, the task is pending, all dependencies succeeded, and no active claim exists. The transaction increments the task fence, inserts the claim, and changes the task to running.

Execution happens after commit and outside every database lock. A future worker service should renew well before the deadline, commonly near one third to one half of the lease duration. Renewal at or after `expires_at` is rejected.

Do not run the synchronous `LoopEngine` against a goal with an active scheduler claim. The engine detects this condition and fails closed instead of applying its legacy interrupted-task recovery. Complete, release, or explicitly reap the claim first.

Legacy per-task mutation methods (`save_task`, `save_artifact`, `save_review`, and `save_attempt_outcome`) also reject active scheduler ownership. Scheduler-managed results must use the fenced commit API.

The result must be persisted with `commit_claim_outcome`. This call revalidates the current durable claim and atomically writes:

- the optional artifact;
- the review and attempt;
- updated task-type performance;
- completion/retry/block events;
- the final task payload;
- the committed claim state.

A failed review may retain its audit artifact while returning the task to pending. A successful task links its accepted artifact from the final task payload.

## Recovery

Recovery is explicit, deterministic, and auditable:

```bash
agent-society scheduler reap \
  --at 2026-07-16T00:00:10+00:00 \
  --db society.db --json
```

An expired ordinary task returns to pending and can receive a higher-token claim. A latest A2A delegation in `submitting`, `unknown`, or `interrupted` changes the task to blocked because replay could duplicate or mishandle remote work. `accepted` work may safely resume polling, and `completed` work may replay its stored result under the v0.7 no-resend rule.

Inspect current evidence with:

```bash
agent-society scheduler workers --db society.db --json
agent-society scheduler claims --goal-id GOAL_ID --db society.db --json
agent-society events GOAL_ID --db society.db --json
```

## Safety Campaign

```bash
agent-society scheduler self-test --json
```

The campaign uses fixed UTC timestamps, a temporary file database, and two independent SQLite connections. It proves exclusive claim, renewal, monotonic takeover, stale-commit rejection without partial records, and current-token atomic commit. It uses no network, socket, or sleep.

## Deployment Boundary

SQLite WAL allows readers and a writer to overlap, but all participating processes must be on the same host. Do not put the scheduler database on NFS, SMB, a shared cloud drive, or another network filesystem. See the official [SQLite WAL documentation](https://www.sqlite.org/wal.html) and [transaction documentation](https://www.sqlite.org/lang_transaction.html).

Fencing protects writes performed through this repository. It cannot undo an external action already sent by a stale worker. Forward an idempotency key or fencing epoch to external systems that support it, and retain the existing approval, content-addressing, A2A no-resend, and policy controls.

The next backend should implement the same `SchedulerRepository` conformance suite using a network-safe transactional database. PostgreSQL queue consumers can use row locking with `SKIP LOCKED`, but backend adoption does not remove the need for monotonic fencing and adapter-level side-effect controls.

---

# 调度安全

v0.8 提供了把任务执行迁移到多个 worker 进程之前所需的所有权与持久化内核。当前实现适用于同一台主机上、共享本地 SQLite 数据库的多个进程，不是跨主机分布式调度器。

## 六项不变量

1. 一个任务最多只有一个 active claim。
2. Claim 同时绑定 `worker_id`、本次进程的 `session_id` 和所选专家 `agent_id`。
3. 每次重新领取同一任务，fencing token 都必须大于该任务历史上的所有 token。
4. 续租、释放和结果提交都必须在截止时间前使用精确的 active 身份。
5. 旧 worker 的结果被拒绝时，不得留下任何局部记录。
6. 模型、工具、文件系统和网络执行期间不持有数据库事务。

## 领取、执行与提交

领取使用短 `BEGIN IMMEDIATE` 事务，事务内重新检查 worker session、目标状态、任务状态、依赖和 active claim，然后同时递增 token、写入 claim、把任务改为 running。耗时执行发生在事务之外。

不要对存在 active scheduler claim 的目标运行同步 `LoopEngine`。引擎会拒绝执行，而不会使用旧版中断恢复逻辑重置该任务；必须先完成、释放或显式 reap claim。

旧的逐任务写入方法 `save_task`、`save_artifact`、`save_review` 和 `save_attempt_outcome` 也会拒绝 active scheduler ownership；调度任务的结果必须通过 fenced commit API 写入。

结果必须通过 `commit_claim_outcome` 提交。它会再次验证当前持有者，并在一个事务中写入 artifact、review、attempt、performance、events、最终 task 和 committed claim。质检失败的草稿可以作为审计 artifact 保留，但 task 回到 pending；质检通过时 task 才链接最终 artifact。

## 过期恢复

使用显式 UTC 时间执行恢复：

```bash
agent-society scheduler reap \
  --at 2026-07-16T00:00:10+00:00 \
  --db society.db --json
```

普通任务回到 pending。最新 A2A 委派若为 `submitting`、`unknown` 或 `interrupted`，任务会进入 blocked，避免自动重放造成重复副作用；`accepted` 可以继续轮询，`completed` 可以复用持久化结果。

## 部署边界

SQLite WAL 要求所有进程位于同一台主机，不能把调度数据库放在 NFS、SMB、共享云盘或其他网络文件系统。Fencing 只能保护通过本仓库执行的写入，无法撤销旧 worker 已经发出的外部请求。不能接受重复副作用的外部系统必须同时校验幂等键或 fencing epoch。

未来的 PostgreSQL 等网络后端必须复用同一套 `SchedulerRepository` 一致性测试；更换数据库并不能替代单调 token 和外部副作用控制。
