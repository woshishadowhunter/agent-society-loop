# Guarded A2A Delegation / A2A 安全委派

Agent Society Loop v0.7 supports the outbound A2A `1.0` `HTTP+JSON` polling subset with a fail-closed delegation trust control plane. This page is bilingual because remote trust and recovery rules must be unambiguous for operators.

智子社会循环 v0.7 支持出站 A2A `1.0` `HTTP+JSON` 轮询子集，并增加默认拒绝的委派信任控制面。本页采用中英双语，确保操作者能准确理解远端信任和故障恢复边界。

## Trust workflow / 信任流程

1. Inspect the public Agent Card with bounded HTTP and record its raw SHA-256.
2. Register the exact card URL, digest, interface URL, `HTTP+JSON` binding, protocol `1.0`, task-type-to-skill map, and bearer environment-variable name.
3. Run an external benchmark against the exact model identity `a2a:<full-card-sha256>` and explicitly promote it. Evaluation alone never changes routing.
4. Import a declarative policy, then activate its exact canonical digest for one task type.
5. Run the official `a2aproject/a2a-tck` externally at a pinned 40-character revision and import `reports/compatibility.json`.
6. Run `a2a doctor`; it fetches the card once, performs eight read-only checks, and never sends a business message.
7. Run the socket-free local reliability campaign with `a2a self-test`.
8. Start production with `run --allow-remote`. A new remote send requires deployment, policy, fresh passing evidence, and opt-in.

1. 使用有大小限制的 HTTP 检查公开 Agent Card，并记录原始字节的 SHA-256。
2. 固定卡片 URL、摘要、接口 URL、`HTTP+JSON` binding、协议 `1.0`、任务类型到技能的映射，以及 Bearer 环境变量名。
3. 针对精确身份 `a2a:<完整卡片SHA256>` 运行外部 benchmark 并显式晋级；评测本身不会改变路由。
4. 导入声明式策略，再为一个任务类型激活其精确规范摘要。
5. 在系统外部使用固定的 40 位源码修订运行官方 `a2aproject/a2a-tck`，导入 `reports/compatibility.json`。
6. 运行 `a2a doctor`：它只抓取一次卡片，执行八项只读检查，绝不发送业务消息。
7. 使用 `a2a self-test` 运行不打开 Socket 的本地故障安全活动。
8. 生产运行增加 `run --allow-remote`；新远端发送必须同时具备部署、策略、新鲜通过证明和显式 opt-in。

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

# Replace the all-zero digest in the example with the inspected card digest.
agent-society a2a policy validate examples/a2a-policy.json --json
agent-society a2a policy import examples/a2a-policy.json --db society.db --json
agent-society a2a policy activate research POLICY_DIGEST \
  --by operator --db society.db --json

# Run outside Agent Society Loop. This is the pinned upstream revision for v0.7.
git clone https://github.com/a2aproject/a2a-tck.git
cd a2a-tck
git checkout 5996b79f9cefa6fc390980e383e358a66fb9e49e
uv venv && uv pip install -e .
./run_tck.py --sut-host https://agent.example --transport http_json
cd ..

agent-society a2a attestation import research-agent \
  a2a-tck/reports/compatibility.json \
  --source-revision 5996b79f9cefa6fc390980e383e358a66fb9e49e \
  --tool-version 1.0.0 --db society.db --json
agent-society a2a policy simulate research-agent research --db society.db --json
agent-society a2a doctor research-agent research --db society.db --json
agent-society a2a self-test --json

agent-society run examples/a2a-goal-spec.json \
  --allow-remote --remote-timeout 60 --remote-max-polls 20 \
  --remote-poll-interval 0.25 --db society.db --json
agent-society a2a delegations --db society.db --json
agent-society a2a decisions remote-research-001 --db society.db --json
agent-society a2a cancel DELEGATION_ID --by operator --db society.db --json
```

Use `--allow-insecure-localhost` only for explicit `localhost`, `127.0.0.1`, or `::1` development services. Production interfaces require HTTPS. Redirects and URL-embedded credentials are rejected.

`--allow-insecure-localhost` 只能用于显式的 `localhost`、`127.0.0.1` 或 `::1` 开发服务。生产接口必须使用 HTTPS。系统拒绝重定向和 URL 内嵌凭据。

## Policy and evidence / 策略与证明

Policies are strict JSON with `default: "deny"`. Rules contain exact task types, agent IDs, card SHA-256 values, context sections, request/result byte limits, poll/deadline limits, required attestation kinds, and maximum evidence age. Unknown fields, duplicate values, wildcards, overlapping rule domains, callbacks, and non-positive limits are rejected. The policy digest is SHA-256 over canonical normalized JSON; local import time is excluded.

策略是 `default: "deny"` 的严格 JSON。规则包含精确任务类型、Agent ID、卡片 SHA-256、上下文分区、请求/结果字节、轮询/时限、必需证明类型和最大证明年龄。未知字段、重复值、通配符、规则域重叠、回调和非正限制都会被拒绝。策略摘要基于规范化 JSON 计算 SHA-256，不包含本地导入时间。

TCK import validates strict report shape, A2A spec `1.0`, at least one MUST requirement, `100%` MUST compatibility, zero failed `http_json` tests, and semantic interface/tenant/skill identity. A well-formed failing report is retained as `passed=false`; malformed or identity-drifted reports are rejected. Raw report bodies, report URLs, and credentials are not persisted.

TCK 导入会验证严格报告结构、A2A `1.0`、至少一条 MUST、MUST 兼容率 `100%`、`http_json` 零失败，以及接口/租户/技能语义身份。结构正确但测试失败的报告会以 `passed=false` 留证；畸形或身份漂移报告会被拒绝。原始报告正文、报告 URL 和凭据不会持久化。

The imported source revision and tool version are operator assertions. They are useful provenance but are not cryptographic proof that the reported command ran or that the report is authentic. Protect the SQLite database and TCK artifact in the operator trust boundary.

导入的源码修订和工具版本属于操作者声明，可用于追踪来源，但不能密码学证明命令真实运行或报告真实可信。SQLite 数据库和 TCK 产物必须处于操作者信任边界内。

## Data boundary / 数据边界

Every request always contains the task description, acceptance criteria, pinned skill ID, durable message ID, and `configuration.returnImmediately: true`. Additional context is limited to the registration allowlist:

每个请求固定包含任务描述、验收标准、已固定技能 ID、持久 message ID 和 `configuration.returnImmediately: true`。额外上下文仅能来自注册时的允许列表：

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

Polling is bounded by request timeout, total timeout, poll count, interval, response bytes, and result bytes. Policy can only tighten those runtime ceilings. Deadline exhaustion makes one best-effort cancellation. `input-required` and `auth-required` become `interrupted`; v0.7 does not send follow-up input or credentials in band.

轮询受到单请求超时、总超时、次数、间隔、响应字节和结果字节限制，策略只能进一步收紧这些运行时上限。耗尽期限时只尝试一次取消。`input-required` 和 `auth-required` 会变成 `interrupted`；v0.7 不会在协议内补充输入或凭据。

## Non-goals / 非目标

- No inbound A2A server, streaming, webhooks, or push notifications.
- No multi-turn input/auth exchange or credential acquisition.
- No arbitrary file/media transfer or remote tool exposure.
- No automatic discovery, trust-on-first-use, JWS/JCS verification, or card rotation.
- No automatic resend, fallback, benchmark scoring, promotion, or online experimentation.
- No runtime TCK download/execution, signed attestations, or policy-generated code.

- 不提供入站 A2A 服务、流式、Webhook 或推送通知。
- 不提供多轮输入/认证交换或凭据获取。
- 不提供任意文件/媒体传输或远端工具暴露。
- 不提供自动发现、首次使用即信任、JWS/JCS 验签或卡片轮换。
- 不提供自动重发、回退、benchmark 打分、晋级或在线实验。
- 不在运行时下载/执行 TCK，不提供签名证明或策略生成代码。
