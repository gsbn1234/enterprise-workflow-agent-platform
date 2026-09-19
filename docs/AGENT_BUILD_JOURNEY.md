# 从 0 搭建企业多智能体 Agent 平台：实现过程与设计说明

本文记录这个项目从“企业业务流程自动化 Agent”逐步升级为“前沿多智能体 Agent 平台”的过程。它的目标不是只说明代码放在哪里，而是说明每一步为什么做、解决什么问题、现在如何实现、后续如何继续生产化。

## 1. 为什么不再做一个普通 RAG

你已经有企业知识库 RAG 系统，它能完成文档入库、检索、问答、引用溯源、权限与评测。如果第二个项目仍然只是“上传文档后问答”，简历信号会重复。

AI Agent 应用开发岗更关心的是：

```text
Agent 能不能拆解任务
Agent 能不能调用企业工具
Agent 能不能进入真实业务流程
Agent 能不能处理状态、失败、审批和恢复
Agent 能不能被审计、被观测、被评测
Agent 能不能作为多智能体系统协作
```

所以本项目选择“企业业务流程自动化”作为业务场景，用退款、采购、安全、故障、普通运营请求来展示 Agentic Workflow、tool calling、MCP、human-in-the-loop、eval 和 multi-agent orchestration。

## 2. 第一阶段：先跑通单 Agent Workflow

最小可运行闭环是：

```text
业务请求
  -> guard
  -> plan
  -> retrieve enterprise RAG
  -> retrieve local policy
  -> lookup customer
  -> create ticket
  -> draft email
  -> request approval 或 send email
  -> finalize
```

对应代码：

```text
app/services/agent/planner.py
app/services/agent/executor.py
app/services/agent/retry.py
app/services/agent/state.py
```

这里使用规则型 planner，而不是一开始就依赖 LLM。原因是作品集阶段需要可重复评测，规则 planner 能保证 smoke test 和 harness 稳定。后续可以把 planner 替换为 LLM planner 或 LangGraph 节点。

单 Agent workflow 的核心价值是 trace。每一步都写入 `workflow_steps`，记录：

```text
node_name
action_type
tool_name
tool_input_json
tool_output_json
status
attempt_count
max_attempts
retryable
error_type
attempts_json
reasoning_summary
latency_ms
```

这让系统能回答“Agent 为什么这么做”“调了哪些工具”“哪一步失败”“为什么暂停审批”等问题。

## 3. 第二阶段：把企业 RAG 升级为正式 Agent Tool

一开始 RAG 只是知识检索的一部分，但在简历里更有价值的表达是：企业 RAG 是 Agent 可调用工具。

因此新增：

```text
tool_name=query_enterprise_rag
```

位置：

```text
app/services/tools/knowledge.py
```

输入包含：

```text
question
top_k
user_id
user_department
user_role
```

输出包含：

```text
answer
can_answer
citations
retrieved_chunks
refusal_reason
log_id
eval_reports_url
```

如果 `.env` 没有配置 `KNOWLEDGE_RAG_BASE_URL`，工具不会让 workflow 崩溃，而是返回：

```text
available=false
reason=KNOWLEDGE_RAG_BASE_URL is not configured.
```

随后 workflow 自动回退到本地政策库 `search_knowledge`。这是一种更符合企业系统的降级策略：知识服务不可用不应导致所有业务流程完全中断。

## 4. 第三阶段：工具层与 MCP-style 服务

工具集中在：

```text
app/services/tools/
```

当前工具包括：

```text
query_enterprise_rag
search_knowledge
lookup_customer
create_ticket
update_ticket
draft_email
send_email
request_approval
```

工具注册表在：

```text
app/services/tools/registry.py
```

它不仅保存函数映射，还保存：

```text
JSON Schema
输出形态
是否有副作用
是否需要审批
所需角色
```

系统提供两种 MCP-style 暴露方式：

```text
GET /api/mcp/tools
POST /api/mcp/call
python scripts/mcp_stdio_server.py
```

stdio JSON-RPC 支持：

```text
initialize
tools/list
tools/call
resources/list
resources/read
prompts/list
prompts/get
```

这一步让项目能展示“工具可以被标准化暴露给 Agent 客户端”的能力。生产化时可以替换为官方 MCP SDK。

## 5. 第四阶段：Human-in-the-loop

企业 Agent 不能直接执行所有动作。退款、安全、采购、故障等高风险场景必须进入人工审批。

当前实现：

```text
workflow status = waiting_approval
approval payload = ticket + plan + email draft + knowledge evidence + rag citations
```

审批通过：

```text
send_email 或 update_ticket
complete workflow
write audit log
```

审批拒绝：

```text
cancel workflow
do not execute risky action
write audit log
```

对应代码：

```text
app/services/tools/approvals.py
app/services/agent/executor.py
```

API：

```http
GET /api/approvals
POST /api/approvals/{approval_id}/decide
```

这对应岗位 JD 里的 human-in-the-loop、approval gates、safe automation。

## 6. 第五阶段：异步 Job 与 Worker

真实 Agent workflow 可能等待模型、等待外部系统、等待审批，不能完全依赖同步 HTTP。

因此新增：

```text
workflow_jobs
scripts/worker.py
```

API：

```http
POST /api/workflow/jobs
GET /api/jobs
GET /api/jobs/{job_id}
POST /api/jobs/{job_id}/retry
POST /api/jobs/run-next
```

当前实现分两种后端，配置项是 `AGENT_QUEUE_BACKEND`（见 `app/services/queue.py` 和 `docs/QUEUEING.md`）：

```text
db     本地默认。worker 轮询 workflow_jobs 表，用数据库锁原子领取任务。
redis  生产默认（docker-compose.prod.yml）。job 创建或重试时把 job id 推到 Redis 队列，
       worker 阻塞弹出信号后再回到数据库领取对应行，数据库始终是 source of truth。
```

也就是说 “Redis + Celery/RQ” 里的 Redis 部分已经落地：Redis 负责唤醒与横向扩展，durable 状态仍在 `workflow_jobs` 表里。

仍然保留为后续可选项的是：

```text
Celery / RQ
PostgreSQL advisory lock
Temporal / Prefect
```

另外，多智能体编排这一层已经接入 LangGraph durable execution（`app/services/multi_agent/durable_executor.py`），主库也已经支持 PostgreSQL。

## 7. 第六阶段：工具级 Retry / Backoff

Job 级 retry 只能处理整条 workflow 失败，但真实系统里更常见的是单个工具临时失败。

因此加入工具级 retry：

```text
app/services/agent/retry.py
```

默认策略：

```text
query_enterprise_rag   retryable
search_knowledge       retryable
lookup_customer        retryable
draft_email            retryable
update_ticket          retryable
create_ticket          non-retryable
send_email             non-retryable
request_approval       non-retryable
```

这个取舍很重要。创建工单、发送邮件、创建审批都有副作用，盲目重试会产生重复业务动作；查询类和幂等更新更适合自动重试。

trace 中记录：

```text
attempt_count
max_attempts
retryable
error_type
attempts_json
```

对应测试：

```text
scripts/tool_retry_smoke_test.py
```

## 8. 第七阶段：升级为多智能体平台

普通 Agent workflow 仍然容易被看成“一个状态机 demo”。为了更贴近前沿 Agent 平台，本阶段引入多智能体协作。

新增目录：

```text
app/services/multi_agent/
  agents.py
  orchestrator.py
  memory.py
```

新增表：

```text
multi_agent_runs
multi_agent_messages
agent_memory
```

当前 Agent 分工：

```text
Supervisor Agent
  负责规划和路由，判断需要哪些子 Agent。

RAG Research Agent
  调用企业 RAG 与本地政策库，输出 evidence_count 和 selected_source。

Risk & Approval Agent
  判断风险、审批必要性、合规 warning 和执行决策。

Tool Execution Agent
  复用底层 workflow，把任务真正落到工单、邮件、审批。

Critic Agent
  读取 workflow trace，检查 RAG tool、create_ticket、request_approval、failed steps、retry attempts。

Memory Agent
  检索相似历史案例，并根据 critic 结果写入 success_case 或 failure_pattern。
```

多智能体 API：

```http
POST /api/multi-agent/run
GET /api/multi-agent/runs
GET /api/multi-agent/runs/{run_id}
GET /api/agent-memory
```

多智能体 run 会关联底层 workflow run：

```text
multi_agent_runs.workflow_run_id -> workflow_runs.id
```

这样既能展示多 Agent 协作轨迹，也能继续复用已有的业务 trace、审批、审计和评测。

## 9. 第八阶段：Critic Agent 与 Harness

只做多 Agent 名字还不够，关键是能评测。于是新增 Critic Agent 和多智能体 harness。

Critic 检查：

```text
是否调用 query_enterprise_rag
是否创建 create_ticket
高风险是否进入 waiting_approval 或已完成审批路径
Risk Agent 要求审批时是否调用 request_approval
是否有 failed steps
是否出现 retry attempts
```

输出：

```text
score
passed
findings
tool_sequence
retry_attempts
workflow_status
```

多智能体 harness：

```text
scripts/multi_agent_harness.py
sample_data/eval/multi_agent_scenarios.jsonl
```

它检查：

```text
expected_agents
min_critic_score
expected_workflow_status
expected_category
expected_approval
```

报告输出：

```text
data/eval_reports/multi_agent_latest_summary.json
data/eval_reports/multi_agent_latest_results.jsonl
```

并写入：

```text
eval_reports
```

当前一次运行结果：

```json
{
  "total_count": 4,
  "passed_count": 4,
  "pass_rate": 1.0,
  "agent_accuracy": 1.0,
  "expected_agent_coverage": 1.0,
  "workflow_status_accuracy": 1.0,
  "approval_accuracy": 1.0,
  "avg_critic_score": 100.0
}
```

这一步让简历中的“agent 轨迹级评测框架”有代码、数据集和报告支撑。

## 10. 第九阶段：LangGraph Durable Executor

前一版多智能体编排是顺序函数调用，能跑通，但不够像真实前沿 Agent 平台。真实系统需要 checkpoint、thread、replay、fork 和故障恢复能力，所以这一阶段把多智能体编排迁移到 LangGraph。

新增实现：

```text
app/services/multi_agent/durable_executor.py
```

核心结构：

```text
StateGraph(MultiAgentState)
  START
  -> memory_retrieve
  -> supervisor
  -> rag_research
  -> risk_approval / risk_bypass
  -> tool_execution
  -> critic
  -> self_correction / memory_write
  -> finalize
  -> END
```

LangGraph 的 durable 部分使用：

```text
SqliteSaver
thread_id = multi-agent:{run_id}
```

LangGraph 自己会把状态写入 checkpoint 数据库。同时，为了让业务 API 和 UI 更容易展示，本项目还把每个关键节点的状态镜像到：

```text
agent_checkpoints
```

这样可以通过 API 查询：

```http
GET /api/multi-agent/runs/{run_id}/checkpoints
GET /api/multi-agent/runs/{run_id}/trace
```

这一步让项目从“多 Agent 顺序 demo”升级成“有 durable execution 形态的平台”。

## 11. 第十阶段：Critic-driven Self-correction

Critic Agent 不应该只是打分器。如果它发现轨迹质量低，系统应该尝试修正。

新增：

```text
CorrectionAgent
```

逻辑是：

```text
critic score < 80
  -> 分析 findings
  -> 生成 self-correction requirements
  -> 追加到 active_objective
  -> 重新执行 Tool Execution Agent
  -> 再跑 Critic Agent
```

当前修复策略覆盖：

```text
missing_rag_tool      -> 要求先检索企业 RAG 与本地政策证据
missing_ticket        -> 要求创建可审计工单
missing_approval_tool -> 高风险动作必须进入人工审批
approval_state_wrong  -> 风险路径必须暂停或经过审批完成
failed_steps          -> 只重试 retryable 工具并保留审计
```

对应烟测：

```text
scripts/self_correction_smoke_test.py
```

它会强制第一轮 Critic 低分，验证系统是否能进入 self_correction 节点、重新执行 workflow，并最终由第二轮 Critic 通过。

## 12. 第十一阶段：Trace Replay 与 Golden Trace Diff

Agent 系统上线后，最有价值的评测不是只看最终回答，而是比较轨迹是否退化。因此新增 trace replay 和 golden diff。

新增：

```text
app/services/multi_agent/trace_tools.py
scripts/trace_replay.py
scripts/trace_replay_smoke_test.py
```

标准化 trace 会提取：

```text
agent_sequence
agent_roles
checkpoint_sequence
tool_sequence
workflow.status
workflow.category
workflow.needs_approval
critic_score
critic_findings
correction_count
```

Golden trace 能保存一条已知正确轨迹：

```http
POST /api/golden-traces
```

Replay 会用历史 run 的 objective 和 requester 上下文重新运行：

```http
POST /api/trace-replay
```

Diff 会比较：

```text
Agent 顺序是否变化
工具顺序是否变化
checkpoint 顺序是否变化
workflow 状态是否变化
分类是否变化
审批触发是否变化
critic 是否仍然通过
```

CLI：

```powershell
python scripts\trace_replay.py export --run-id ma_xxx
python scripts\trace_replay.py save-golden --run-id ma_xxx --name refund_high_golden
python scripts\trace_replay.py replay --run-id ma_xxx
python scripts\trace_replay.py diff --run-id ma_new --golden-id golden_xxx
```

这一步让“trace-level regression testing”变成可演示能力。

## 13. 第十二阶段：管理台升级

前端源码位于：

```text
frontend/
```

这是 Vite + React 应用，构建产物输出到：

```text
app/static/react/
```

对应 `vite.config.js` 的 `root: "frontend"`、`outDir: "../app/static/react"`、`base: "/static/react/"`。服务端 `_frontend_entry()` 优先返回 `app/static/react/index.html`，只有构建产物不存在时才回退到旧的 `app/static/index.html`。

当前管理台支持：

```text
发起多智能体运行
发起单 Agent workflow
入队异步任务
查看多智能体轨迹
查看 workflow trace
查看审批
查看 job
查看指标
查看工单、邮件、知识库
登录/退出
```

多智能体轨迹会展示：

```text
critic score
correction count
executor type
workflow_run_id
final_summary
每个子 Agent 的 role/status/latency
critic findings
保存 Golden / Replay
```

这让面试演示不只是命令行输出，而是能直接在浏览器里展示 Agent 平台。

## 14. 当前项目的简历价值

现在项目已经能支撑这些表述：

```text
构建企业级多智能体 Agentic Workflow 平台，支持 Supervisor/RAG Research/Risk/Tool/Critic/Memory 多 Agent 协作、多步骤状态化任务编排、工具调用、人工审批、失败重试与审计追踪。

将企业知识库 RAG 作为 Agent 工具接入，支持基于用户/部门/角色上下文的检索、引用溯源、外部 RAG 不可用回退、本地政策检索与轨迹级评测。

实现 MCP-style 工具服务，封装工单、CRM、邮件、审批、知识检索等企业系统接口，支持 JSON Schema、resources/prompts 与可观测 tool calling。

基于 LangGraph 实现 durable multi-agent executor，使用 SQLite checkpointer 与 thread_id 保存节点状态，并支持 trace replay、golden trace diff 和 checkpoint 查询。

设计多智能体 harness 与 Critic-driven self-correction，覆盖 expected agents、工具调用正确性、任务完成率、人工审批触发率、成本、延迟、失败重试和自修正次数指标。
```

## 15. 后续生产化路线

这份路线最初写下的 9 项里，有 4 项已经落地，因此从"后续"移到这里记录当前实现位置：

```text
已完成：
5. OpenTelemetry
   app/services/observability.py 的 configure_opentelemetry()，
   AGENT_OTEL_ENABLED / OTEL_EXPORTER_OTLP_ENDPOINT / AGENT_OTEL_EXPORT_CONSOLE，
   烟测 scripts/observability_smoke_test.py。
   （Langfuse / LangSmith 仍未接入。）

6. PostgreSQL（Alembic 未使用）
   AGENT_DB_BACKEND=postgres + app/db.py 的 PostgresConnection，
   schema 迁移由 scripts/migrate.py 与 schema_migrations 表管理，
   烟测 scripts/postgres_smoke_test.py、scripts/postgres_rls_smoke_test.py。

7. Redis 队列（Celery/RQ 未使用）
   app/services/queue.py + AGENT_QUEUE_BACKEND=redis / AGENT_REDIS_URL，
   生产 compose 默认开启，见 docs/QUEUEING.md 与 scripts/queue_smoke_test.py。

8. 多租户与工具权限
   多租户：app/services/tenancy.py、AGENT_TENANT_ISOLATION_ENABLED、
   PostgreSQL 行级安全（app/db.py 的 postgres_rls_status）。
   工具权限：app/services/tools/registry.py 的 required_role 与
   app/services/it/rbac.py 的分级校验，烟测 scripts/tenant_isolation_smoke_test.py、
   scripts/it_rbac_smoke_test.py。
```

仍然待做：

```text
1. LangGraph 并行分支：RAG Research 与 Memory Retrieve 并行执行
   （注：research_dispatch -> enterprise_rag_research / local_policy_research 与
   risk_dispatch -> compliance_risk / operational_risk 已经是并行分支，
   memory_retrieve 目前仍在 supervisor 之前串行执行。）
2. Checkpoint fork：从指定 checkpoint 派生新 run
3. Golden trace 可视化 diff 页面
4. LLM-as-judge adapter
9. 官方 MCP SDK
```

如果要继续向“更前沿”冲，优先做 checkpoint fork、并行分支和 golden trace 可视化 diff，因为它们最容易在面试中讲出工程深度。
