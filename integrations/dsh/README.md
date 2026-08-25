# DeepSeek Harness 集成：把 Seed Society 装进八识

> **一键安装**：`powershell -ExecutionPolicy Bypass -File integrations\dsh\install-plugin.ps1`
> —— 安装 `dsh-seed-society` 插件包（mneme 调优 + llm-deepseek 修复 +
> MCP 桥 + 六个种子技能），详见
> [`plugin/dsh-seed-society/README.md`](plugin/dsh-seed-society/README.md)。

本目录是把 `seed-society` 作为插件体系接入 DeepSeek Harness（DSH）的
全部材料。理论总纲见 [`docs/yogacara-architecture.md`](../../docs/yogacara-architecture.md)。

## 一、种子技能（已热加载，无需重启）

DSH 的技能提供方 `@deepseek-ai/dsh-skill-filesystem` 会扫描项目根
`.agents/skills/`（rank 200）与用户根 `~/.dsh/skills`（rank 400），并热加载
新目录。仓库内的六个种子是唯一真源：

| 技能 | 识 |
| --- | --- |
| `yogacara-society` | 总纲：八识地图与个人种子契合工作流 |
| `yogacara-alaya` | 阿赖耶识：种子库、熏习、现行 |
| `yogacara-manas` | 末那识：个体性、路由、晋升 |
| `yogacara-mano` | 意识：双循环、goal 生命周期 |
| `yogacara-panca` | 前五识：工具门、网络边界、工作区 |
| `yogacara-sila` | 戒律：预算、审批、围栏、默认拒绝 |

让它们在任何工作区可用：

```powershell
powershell -ExecutionPolicy Bypass -File integrations\dsh\sync-skills.ps1
```

默认镜像到 `$env:DSH_HOME\skills`；`-Destination ~/.agents/skills` 可换到
通用 agents 根（Claude Code / Codex / 豆包 也能读）。

## 二、MCP 工具桥（需重启一次生效）

`society_server.py`（包内路径 `seed_society.mcp_server`，零依赖）把
运行时暴露为 MCP stdio 工具。注册方式：把
[`cordis.patch.example.yml`](cordis.patch.example.yml) 的条目合并进 profile
的 `cordis.patch.yml`，然后重启 DSH。

暴露的工具（模型视角为 `mcp__society__<name>`）：

| 工具 | 识 | 底层 CLI |
| --- | --- | --- |
| `society_plugins` | 总纲 | `plugins` |
| `society_demo` / `society_run_spec` / `society_enqueue` | 意识 | `demo` / `run` / `enqueue` |
| `society_status` / `society_events` / `society_agents` | 阿赖耶识（回看） | `status` / `events` / `agents` |
| `society_knowledge_add` / `society_knowledge_search` | 种子现行 | `knowledge ...` |
| `society_genome_set` / `society_genome_show` / `society_genome_recombine` | 末那识 | `genome ...` |
| `society_experience_distill` / `society_experience_list` | 熏习 | `experience ...` |
| `society_evaluate` / `society_promote` / `society_deployments` | 晋升门 | `evaluate` / `promote` / `deployments` |
| `society_approve` / `society_reject` | 身业戒 | `approve` / `reject` |
| `society_scheduler_self_test` / `society_product_self_test` | 戒律自证 | `scheduler self-test` / `product self-test` |
| `society_health` / `society_metrics` | 观照 | `health` / `metrics` |

本地烟测（不依赖 DSH）：

```powershell
$env:PYTHONPATH="src"
@'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"society_plugins","arguments":{"describe":true}}}
'@ | python -m seed_society.mcp_server
```

## 三、dsh-mneme 记忆桥（已安装，重启 DSH 生效）

已从插件超市安装 `@modusensus/dsh-mneme@0.1.6`（记忆主权 Markdown 镜像 +
autoDream LLM 巩固 + 快照哈希/CAS/receipt 审计），已对账进
`dsh.profile.bundles`。**重启 DSH 后**记忆库将落在 `~/.dsh/memory/`
（`memory.db` + 五个可人工编辑的 Markdown 镜像）。

与 seed-society 的分工：我们出**确定性晋升门与熏习动力学**，mneme 出
**会话侧现行面、人工主权镜像与 LLM 模糊仲裁**。双向桥：

```powershell
# 下行：晋升门产出的语义知识（source:consolidation）+ 高强度 PASS 教训
seed-society mneme sync --db society.db --mneme-dir ~/.dsh/memory [--include-experience] [--push]

# 上行：mneme 的 dream 总结/人工决策 → society 知识种子（回声防护 + 去重）
seed-society mneme import --db society.db --mneme-dir ~/.dsh/memory [--type summary] [--apply]

# 单命令联动：巩固 + 推送 + 遗忘联动（衰减种子 importance 跌破 3 即停止注入）
seed-society consolidate GOAL --db society.db --mneme-dir ~/.dsh/memory --apply
```

### autoDream 调优（2026-08-25 最终修复，已验证）

演进与结论：

1. **v1（08-18）**：`dreamMaxTokens` 4096→16384——仍失败；
2. **v2（08-24）**：确认 mneme 梦巩固每次打包全库，阈值只控触发频率；
3. **库卫生（08-24）**：归档 39 条（history/低值/内容级去重），182→143，
   审计：`integrations/dsh/maintenance/2026-08-24-mneme-hygiene.json`；
4. **根因（08-25 代码级实证）**：`dsh-llm-deepseek` 的 `modelInfoFor` 在
   `reasoningEffort` 未配置时把路由默认 effort 设为 **HIGH**，harness 把
   `thinking:enabled + reasoning_effort:high` 注入**每个未显式指定 effort 的
   调用**（包括 autoDream）。145 条全库任务下推理吞掉输出预算：
   deepseek-chat 截断（finish=length）、v4-flash 内容为空 → 全部
   "no json array in llm output"；
5. **修复**：`llm-deepseek` 配置 `reasoningEffort: off`（wire 只发
   `thinking:disabled`，等效纯文本调用）。主聊天不受影响——agent-default-model
   显式传 max，显式值优先于路由默认；
6. **验证（2026-08-25 实测）**：dream run `cbb738e3` → **applied=45、
   summary=1**（14 条无效决策被 CAS 跳过，诚实上报 degraded）；活跃记忆
   146→68，`summary.md` 主权镜像生成并注入会话。

最终配置（`cordis.patch.yml`）：

```yaml
- id: llm-deepseek
  config:
    reasoningEffort: off        # 路由默认 off：dream 等无显式 effort 的调用走纯文本
- id: dsh-mneme
  config:
    autoDream: true
    dreamMaxTokens: 32768
    dreamProvider: deepseek-official
    dreamModel: deepseek-chat   # 已实测输出有效决策 JSON
```

后续如果 autoDream 偶尔 degraded，属正常 LLM 噪音（部分决策被校验跳过），
重复触发会逐步收敛；若连续 failed，查 `dream_runs.error`。

戒律：双向默认干跑，`--push`/`--apply` 才落盘并写 `memory.mneme_pushed`/
`memory.mneme_imported` 审计事件；行 ID 内容寻址幂等；**不复活** mneme 侧
forget/archive 的行；只写 `memories` 表（WAL + busy_timeout 并发安全）；
向量留待 mneme 自身 reindex。

## 四、边界

- MCP 服务器**不使用 shell**：所有 CLI 参数由 JSON 校验后作为 argv 元素
  传入，无命令注入面；
- 审批门不被 MCP 绕过：`society_approve`/`reject` 仍要求操作员身份；
- 服务器不持有密钥、不开网络端口；DB 路径由调用方给出；
- 新插件实例需要 DSH 重启一次（HMR 只能热改配置，不能凭空加实例）；
  技能种子则完全热加载。
