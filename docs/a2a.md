# Guarded A2A Delegation / A2A 安全委派

Agent Society Loop v0.6 supports the outbound A2A `1.0` `HTTP+JSON` polling subset. This page is bilingual because remote trust and recovery rules must be unambiguous for operators.

智子社会循环 v0.6 支持出站 A2A `1.0` `HTTP+JSON` 轮询子集。本页采用中英双语，确保操作者能准确理解远端信任和故障恢复边界。

## Trust workflow / 信任流程

1. Inspect the public Agent Card with bounded HTTP and record its raw SHA-256.
2. Register the exact card URL, digest, interface URL, `HTTP+JSON` binding, protocol `1.0`, task-type-to-skill map, and bearer environment-variable name.
3. Run an external benchmark against the exact model identity `a2a:<full-card-sha256>` and inspect per-case evidence.
4. Explicitly promote a recommended challenger. Evaluation alone never changes routing.
5. Start production with `run --allow-remote`. Without that flag, an active remote deployment blocks.

1. 使用有大小限制的 HTTP 检查公开 Agent Card，并记录原始字节的 SHA-256。
2. 固定卡片 URL、摘要、接口 URL、`HTTP+JSON` binding、协议 `1.0`、任务类型到技能的映射，以及 Bearer 环境变量名。
3. 针对精确身份 `a2a:<完整卡片SHA256>` 运行外部 benchmark，并检查逐案例证据。
4. 由操作者显式晋级通过门禁的挑战者；评测本身不会改变路由。
5. 生产运行必须增加 `run --allow-remote`；缺少该参数时，远端 active deployment 会阻塞。

## Commands / 命令

The following uses reserved example domains and placeholder identities. Replace them with operator-approved values; never take an endpoint or digest from model output.

以下命令使用保留的示例域名和占位身份。必须替换为操作者批准的值，不能从模型输出中接受端点或摘要。

```bash
agent-society a2a inspect-card \
  https://agent.example/.well-known/agent-card.json --json

export ACME_A2A_TOKEN="..."
agent-society a2a register research-agent \
  https://agent.example/.well-known/agent-card.json \
  --sha256 CARD_SHA256 \
  --interface https://agent.example/a2a \
  --skill research=deep-research \
  --auth-env ACME_A2A_TOKEN \
  --allow-context review_feedback \
  --db society.db --json

agent-society a2a agents --db society.db --json
agent-society deployments --db society.db --json
agent-society run examples/a2a-goal-spec.json \
  --allow-remote --remote-timeout 60 --remote-max-polls 20 \
  --remote-poll-interval 0.25 --db society.db --json
agent-society a2a delegations --db society.db --json
agent-society a2a cancel DELEGATION_ID --by operator --db society.db --json
```

Use `--allow-insecure-localhost` only for explicit `localhost`, `127.0.0.1`, or `::1` development services. Production interfaces require HTTPS. Redirects and URL-embedded credentials are rejected.

`--allow-insecure-localhost` 只能用于显式的 `localhost`、`127.0.0.1` 或 `::1` 开发服务。生产接口必须使用 HTTPS。系统拒绝重定向和 URL 内嵌凭据。

## Data boundary / 数据边界

Every request always contains the task description, acceptance criteria, pinned skill ID, durable message ID, and `returnImmediately: true`. Additional context is limited to the registration allowlist:

每个请求固定包含任务描述、验收标准、已固定技能 ID、持久 message ID 和 `returnImmediately: true`。额外上下文仅能来自注册时的允许列表：

- `goal`
- `task_context`
- `dependency_artifacts`
- `review_feedback` (default / 默认)
- `knowledge`

Sensitive key names are redacted before serialization. Response artifacts accept only non-empty text and structured data. Structured data becomes canonical JSON text. File, URL, audio, video, unknown, empty, and oversized outputs are rejected before local artifact creation.

敏感字段名会在序列化前脱敏。响应产物仅接受非空文本和结构化数据，结构化数据会转成规范 JSON 文本。文件、URL、音频、视频、未知类型、空结果和超限结果都不能进入本地产物。

## Durable recovery / 持久恢复

Delegations advance through `prepared -> submitting -> accepted -> completed`, with terminal `failed`, `canceled`, `rejected`, `interrupted`, or `unknown` branches. Message ID and payload digest are saved before the POST. Accepted tasks resume by polling the same remote task ID; completed results replay from SQLite without a network call.

委派按 `prepared -> submitting -> accepted -> completed` 推进，并可能进入 `failed`、`canceled`、`rejected`、`interrupted` 或 `unknown`。系统在 POST 前保存 message ID 和 payload 摘要。已接受任务只轮询同一个远端 task ID；已完成结果直接从 SQLite 重放，不再联网。

A timeout, connection loss, 5xx, oversized response, or malformed successful response after Send Message is ambiguous: the remote may already have executed it. The record becomes terminal `unknown`; the runtime never resends automatically and the local goal blocks for operator investigation.

Send Message 后发生超时、断线、5xx、响应超限或成功响应格式错误时，远端可能已经执行，因此结果属于“模糊提交”。记录会进入终态 `unknown`；运行时绝不自动重发，本地目标会阻塞并等待操作者调查。

Polling is bounded by request timeout, total timeout, poll count, interval, response bytes, and result bytes. Deadline exhaustion makes one best-effort cancellation. `input-required` and `auth-required` become `interrupted`; v0.6 does not send follow-up input or credentials in band.

轮询受到单请求超时、总超时、次数、间隔、响应字节和结果字节限制。耗尽期限时只尝试一次取消。`input-required` 和 `auth-required` 会变成 `interrupted`；v0.6 不会在协议内补充输入或凭据。

## Non-goals / 非目标

- No inbound A2A server, streaming, webhooks, or push notifications.
- No multi-turn input/auth exchange or credential acquisition.
- No arbitrary file/media transfer or remote tool exposure.
- No automatic discovery, trust-on-first-use, JWS/JCS verification, or card rotation.
- No automatic resend, fallback, benchmark scoring, promotion, or online experimentation.

- 不提供入站 A2A 服务、流式、Webhook 或推送通知。
- 不提供多轮输入/认证交换或凭据获取。
- 不提供任意文件/媒体传输或远端工具暴露。
- 不提供自动发现、首次使用即信任、JWS/JCS 验签或卡片轮换。
- 不提供自动重发、回退、benchmark 打分、晋级或在线实验。
