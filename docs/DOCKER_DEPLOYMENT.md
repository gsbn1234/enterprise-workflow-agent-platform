# Docker 部署与验证

这份文档记录如何用 Docker Compose 启动当前 Agent 平台。

仓库里有两个 compose 文件：

```text
docker-compose.yml        本地开发栈（SQLite + DB 队列，两个容器）
docker-compose.prod.yml   HR/生产化 Demo（PostgreSQL + Redis + RAG + 外部工单 + 本地 OIDC，11 个服务）
```

第 1-5 节走 `docker-compose.yml`；完整 HR Demo 走第 6 节。

## 1. 构建并启动

默认映射到宿主机 `8010`：

```powershell
docker compose up --build -d
```

如果要启用严格鉴权模式：

```powershell
$env:AGENT_AUTH_REQUIRED="true"
$env:AGENT_AUTH_TOKEN_SECRET="replace-with-a-long-random-secret"
docker compose up --build -d
```

如果要调整工具级重试：

```powershell
$env:AGENT_TOOL_RETRY_MAX_ATTEMPTS="3"
$env:AGENT_TOOL_RETRY_BACKOFF_SECONDS="0.5"
docker compose up --build -d
```

默认演示账号：

```text
admin    / AdminPass123
manager  / ManagerPass123
alice    / AlicePass123
```

如果本机 `8010` 已经被本地开发服务占用，可以临时改成 `8011`：

```powershell
$env:AGENT_HOST_PORT="8011"
docker compose up --build -d
```

如果 Docker Hub 临时访问失败，但本机已有旧的 `enterprise-workflow-agent:latest` 镜像，可以使用本地缓存基底重建：

```powershell
docker tag enterprise-workflow-agent:latest enterprise-workflow-agent:cache-base
$env:AGENT_BASE_IMAGE="enterprise-workflow-agent:cache-base"
$env:AGENT_SKIP_PIP_INSTALL="true"
$env:AGENT_HOST_PORT="8011"
docker compose up --build -d
```

打开：

```text
http://127.0.0.1:8010
```

或：

```text
http://127.0.0.1:8011
```

## 2. 查看状态

```powershell
docker compose ps
docker compose logs -f workflow-agent
docker compose logs -f workflow-worker
```

容器内置健康检查，开发 compose 与生产 compose 的 `workflow-agent` 都会请求：

```text
GET /api/readiness
```

`/api/readiness` 会同时校验数据库状态，所以容器只有在数据库可用后才会变成 `healthy`。`GET /api/health` 仍然提供，作为不检查依赖的轻量存活端点。

## 3. 运行 Docker 版烟测

默认检查 `8010`：

```powershell
python scripts\docker_smoke_test.py
```

如果使用 `8011`：

```powershell
python scripts\docker_smoke_test.py --base-url http://127.0.0.1:8011
```

如果启用了严格鉴权模式：

```powershell
python scripts\docker_smoke_test.py --base-url http://127.0.0.1:8011 --user-id admin --password AdminPass123
```

烟测会：

- 等待 `/api/readiness` 正常（服务本身与数据库都 ready）
- 发起一个退款类业务 workflow
- 验证分类为 `refund`
- 验证高风险请求停在 `waiting_approval`
- 创建一个异步 workflow job
- 等待 worker 执行 job 并生成 run_id

MCP stdio 工具服务不依赖容器端口，可以在宿主机直接测试：

```powershell
python scripts\mcp_smoke_test.py
```

它会验证：

- initialize
- tools/list
- tools/call
- resources/list
- resources/read
- prompts/list
- prompts/get

## 4. 停止服务

```powershell
docker compose down
```

如果要删除持久化数据：

```powershell
docker compose down
Remove-Item -Recurse -Force .\data
```

## 5. 查看 Worker 日志

```powershell
docker compose logs -f workflow-worker
```

Worker 启动后会持续轮询。开发 compose 没有设置 `AGENT_QUEUE_BACKEND`，默认是 `db`：

```text
workflow_jobs.status = queued
```

领取后会标记：

```text
queued -> running -> completed / failed
```

生产 compose 设置 `AGENT_QUEUE_BACKEND=redis`，worker 改为先阻塞等待 Redis 队列里的 job id 信号，再回到数据库原子领取对应行；数据库仍然是唯一的状态机（见 `docs/QUEUEING.md`）。

## 6. PostgreSQL 版 HR/生产化 Demo

完整版 HR Demo 会启动 11 个服务：Agent、Agent Worker、outbox 派发器、数据保留清理 worker、一次性迁移、Agent PostgreSQL、Redis、RAG、pgvector 库、外部工单系统和本地 OIDC Provider：

```powershell
Copy-Item .env.hr-demo.example .env.hr-demo
docker compose --env-file .env.hr-demo -f docker-compose.prod.yml up --build -d
```

这个 compose 中 Agent 主库已经使用 PostgreSQL：

```text
AGENT_DB_BACKEND=postgres
AGENT_DATABASE_URL=postgresql://agent:agent_password@agent-postgres:5432/agent
AGENT_POSTGRES_SCHEMA=agent_app
```

本地可以只启动 Agent PostgreSQL 做数据库烟测：

```powershell
docker compose --env-file .env.hr-demo -f docker-compose.prod.yml up -d agent-postgres
.\.venv\Scripts\python.exe scripts\postgres_smoke_test.py
```

如果没有启动 PostgreSQL，烟测会显示 `agent_postgres_smoke=skipped`，不会误报成功。

## 7. 当前 Docker 形态

`docker-compose.yml`（本地开发）只有两个容器：

```text
workflow-agent    FastAPI Web 服务
workflow-worker   后台任务 worker，领取并执行 workflow_jobs
```

数据通过宿主机目录持久化：

```text
./data:/app/data
```

`docker-compose.prod.yml`（HR/生产化 Demo）共 11 个服务：

```text
workflow-agent               FastAPI Web 服务，映射 8010
workflow-worker              异步 job worker，监听 Redis 队列
workflow-outbox-dispatcher   外发 outbox 派发（scripts/outbox_dispatcher.py）
workflow-retention-worker    按保留策略定期清理数据（scripts/retention_worker.py）
workflow-migrate             一次性迁移 + seed（--seed --demo-users，restart: "no"）
agent-postgres               Agent 主库 PostgreSQL 16，映射 5433
agent-redis                  Redis 7 队列，映射 6379
rag-postgres                 pgvector/pgvector:pg16，映射 5432
rag-app                      RAG 知识库服务，映射 8000
external-ticket-service      模拟外部工单系统，映射 8020
local-oidc-provider          本地 OIDC Provider，映射 8030
```

依赖顺序由 healthcheck 串起来：`workflow-migrate` 在 `agent-postgres`、`agent-redis` 健康后跑完并成功退出，`workflow-agent` 才启动；`workflow-worker`、`workflow-outbox-dispatcher`、`workflow-retention-worker` 都等 `workflow-agent` 健康后再起。

生产 compose 使用命名卷而不是宿主机目录：

```text
agent_postgres_data / agent_redis_data / agent_data / ticket_service_data / rag_app_data / rag_postgres_data
```

当前队列有两种后端（`app/services/queue.py`，配置见 `docs/QUEUEING.md`）：

```text
AGENT_QUEUE_BACKEND=db      本地默认，worker 轮询 workflow_jobs 表
AGENT_QUEUE_BACKEND=redis   生产 compose 默认，Redis 只承载 job id 信号，数据库仍是 source of truth
```

这些能力已经实现，不再属于"下一步"：

- PostgreSQL 主库：`AGENT_DB_BACKEND=postgres`，schema 迁移由 `scripts/migrate.py` + `schema_migrations` 表管理（未使用 Alembic），多租户行级安全见 `AGENT_POSTGRES_RLS_ENABLED`
- Redis 队列：`AGENT_QUEUE_BACKEND=redis`、`AGENT_REDIS_URL`、`AGENT_REDIS_QUEUE_NAME`
- API 鉴权和角色权限：本地账号 + OIDC 浏览器登录 + SCIM 用户同步（`local-oidc-provider`），工具级 `required_role` 校验
- 多租户隔离：`AGENT_TENANT_ISOLATION_ENABLED`、`AGENT_DEFAULT_TENANT_ID`
- OpenTelemetry：`AGENT_OTEL_ENABLED`、`OTEL_EXPORTER_OTLP_ENDPOINT`（`app/services/observability.py`，烟测 `scripts/observability_smoke_test.py`）

仍然属于后续工作：

- 官方 MCP SDK 版本（当前是 MCP-style JSON-RPC 实现）
- Celery/RQ 这类成熟任务框架（当前 Redis 队列直接由 `app/services/queue.py` 实现）
- LangSmith / Langfuse（OTel 之外的 LLM 可观测平台）
