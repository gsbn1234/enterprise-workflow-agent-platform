# Enterprise Multi-Agent Platform

面向企业业务流程的多智能体 Agent 平台。系统使用 LangGraph 协调知识检索、风险判断、人工审批和工具执行，可处理退款、采购、安全事件、权限申请和客户工单等场景。

它不是聊天机器人 Demo：每次运行都有任务依赖、Agent 交接、持久化状态、审批恢复、工具轨迹和审计记录。

配套知识服务：[enterprise-knowledge-rag](https://github.com/smlfy/enterprise-knowledge-rag)

## 功能概览

- 多智能体协作：Supervisor 分派任务，双 Research Agent 并行检索，双 Risk Agent 独立投票，Critic 执行质量门禁。
- 专家推理：默认使用可测试的确定性策略；启用 Qwen/vLLM 后，Evidence、Compliance、Operational Risk 和 Critic 使用独立角色提示词推理，模型只能升级风险，不能降低审批要求。
- 企业 RAG：把独立 RAG 服务作为正式工具调用，返回 ACL 过滤后的 Chunk、引用和文档元数据。
- Human-in-the-loop：高风险操作通过 LangGraph `interrupt` 暂停，审批后使用同一 `thread_id` 恢复。
- 企业工具：多租户 CRM（客户档案、检索、健康度、互动时间线）、带状态机和操作时间线的工单、邮件、通知和审批，支持 MCP-style HTTP/stdio 调用。
- 可追踪执行：保存 tasks、handoffs、messages、checkpoints、workflow steps、audit logs 和 metrics。
- 企业能力：Bearer Token、OIDC、SCIM、RBAC、多租户、PostgreSQL RLS、Redis Queue 和 Outbox。

## 工作流程

```text
用户请求
  -> Memory Retrieve
  -> Supervisor 生成任务图
  -> [Enterprise RAG Research || Local Policy Research]
  -> Evidence Synthesis
  -> [Compliance Risk || Operational Risk]
  -> Risk Consensus
  -> Tool Execution
  -> Human Approval（高风险）
  -> Critic
  -> Memory Write
```

Research 和 Risk 分支由 LangGraph fan-out/fan-in 并行执行。执行器直接消费上游计划、证据和风险共识，不会重新规划或重复检索。
现有工单查询/更新不需要政策证据，Supervisor 会跳过 Research 分支，但仍执行独立风险投票和权限检查。

## 快速启动

默认配置使用 SQLite、Mock 工单/邮件和本地政策数据，不需要外部 API Key。

### Windows PowerShell

```powershell
git clone https://github.com/smlfy/enterprise-workflow-agent-platform.git
Set-Location enterprise-workflow-agent-platform

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env
uvicorn app.main:app --reload --port 8010
```

打开：

- 用户端：<http://127.0.0.1:8010>
- API 文档：<http://127.0.0.1:8010/docs>
- 管理端：<http://127.0.0.1:8010/admin>

默认 `AGENT_AUTH_REQUIRED=false`，可直接体验。启用认证后可使用演示账号：

```text
admin   / AdminPass123
manager / ManagerPass123
alice   / AlicePass123
```

## 运行一个多智能体任务

```powershell
$body = @{
  objective = "客户申请退款 800 元，请核对政策、创建工单并准备回复 support@orbit.example"
  requester_user_id = "alice"
  requester_department = "Customer Success"
  requester_role = "manager"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8010/api/multi-agent/run `
  -ContentType "application/json" `
  -Body $body
```

高风险请求会返回 `waiting_approval`。在管理台批准后，workflow 和 multi-agent graph 会继续运行。

## 启动完整 Agent + RAG 系统

完整环境需要同时克隆两个仓库：

```powershell
git clone https://github.com/smlfy/enterprise-workflow-agent-platform.git
git clone https://github.com/smlfy/enterprise-knowledge-rag.git

Set-Location enterprise-workflow-agent-platform
Copy-Item .env.hr-demo.example .env.hr-demo

$env:RAG_BUILD_CONTEXT = "../enterprise-knowledge-rag"
docker compose --env-file .env.hr-demo -f docker-compose.prod.yml up --build -d
```

该 Compose 会启动 Agent、Worker、Redis、Agent PostgreSQL、RAG、pgvector、外部工单服务和后台任务。

外部工单 API 使用 `TICKET_SERVICE_TOKEN`，浏览器工单台使用 `.env.hr-demo` 中的 `TICKET_DASHBOARD_USERNAME` / `TICKET_DASHBOARD_PASSWORD` 登录。启动后访问 <http://127.0.0.1:8020>。

可选 vLLM + Qwen2.5-32B-Instruct-AWQ：

```powershell
docker compose --env-file .env.hr-demo `
  -f docker-compose.prod.yml `
  -f docker-compose.vllm.yml `
  --profile gpu up --build -d
```

GPU 部署参数见 [Qwen/vLLM 配置](docs/QWEN_LLM.md)。

## 核心 API

| API | 用途 |
| --- | --- |
| `POST /api/multi-agent/run` | 运行多智能体任务 |
| `GET /api/multi-agent/runs/{id}` | 查看任务、交接和 Agent 输出 |
| `GET /api/multi-agent/runs/{id}/trace` | 查看标准化执行轨迹 |
| `POST /api/workflow/run` | 运行单 workflow |
| `GET /api/approvals` | 查看待审批操作 |
| `POST /api/approvals/{id}/decide` | 审批并恢复执行 |
| `GET/POST /api/customers` | 查询或创建租户内客户档案 |
| `PATCH /api/customers/{id}` | 更新客户状态、健康度和负责人 |
| `GET/POST /api/customers/{id}/interactions` | 查询或记录客户互动时间线 |
| `GET /api/tickets` | 按状态、优先级、部门和关键字查询工单 |
| `PATCH /api/tickets/{id}/ops` | 按合法状态机更新工单 |
| `GET /api/tickets/{id}/events` | 查看工单操作时间线 |
| `GET /api/mcp/tools` | 查看 MCP-style 工具清单 |
| `POST /api/mcp/call` | 调用注册工具 |
| `GET /api/metrics/summary` | 查看运行指标 |

完整接口可在 `/docs` 查看。

## 项目结构

```text
app/
  main.py                    FastAPI 入口
  services/agent/            业务 workflow
  services/multi_agent/      LangGraph 多智能体协调
  services/tools/            RAG、CRM、工单、邮件和审批工具
  static/                    用户端与管理端
frontend/                    React 前端源码
scripts/                     Smoke tests、评测和运维脚本
docs/                        架构、部署与安全说明
docker-compose.prod.yml      Agent + RAG 完整部署
docker-compose.vllm.yml      可选 GPU 推理服务
```

## 测试

```powershell
.\.venv\Scripts\python.exe scripts\smoke_test.py
.\.venv\Scripts\python.exe scripts\multi_agent_smoke_test.py
.\.venv\Scripts\python.exe scripts\multi_agent_coordination_smoke_test.py
.\.venv\Scripts\python.exe scripts\memory_routing_smoke_test.py
.\.venv\Scripts\python.exe scripts\self_correction_smoke_test.py
.\.venv\Scripts\python.exe scripts\trace_replay_smoke_test.py
.\.venv\Scripts\python.exe scripts\multi_agent_harness.py
.\.venv\Scripts\python.exe scripts\crm_ticket_smoke_test.py
.\.venv\Scripts\python.exe scripts\external_ticket_service_smoke_test.py
.\.venv\Scripts\python.exe scripts\ticket_http_outbox_smoke_test.py
```

## 进一步阅读

- [多智能体协作架构](docs/MULTI_AGENT_COORDINATION.md)
- [实现能力核验](docs/IMPLEMENTATION_AUDIT.md)
- [Docker 部署](docs/DOCKER_DEPLOYMENT.md)
- [企业生产准备度](docs/ENTERPRISE_READINESS.md)

## 安全说明

仓库中的密码和 Token 仅供本地演示。公开部署前必须替换密钥、启用认证，并配置服务令牌、邮件白名单和真实的企业身份提供商。
