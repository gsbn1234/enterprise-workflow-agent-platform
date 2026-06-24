# Docker 部署与验证

这份文档记录如何用 Docker Compose 启动当前 Agent 平台。

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

容器内置健康检查，会请求：

```text
GET /api/health
```

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

- 等待 `/api/health` 正常
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

Worker 启动后会持续轮询：

```text
workflow_jobs.status = queued
```

领取后会标记：

```text
queued -> running -> completed / failed
```

## 6. PostgreSQL 版 HR/生产化 Demo

完整版 HR Demo 会同时启动 Agent、Agent Worker、Agent PostgreSQL、RAG、pgvector 和外部工单系统：

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

当前 compose 包含两个应用容器：

```text
workflow-agent    FastAPI Web 服务
workflow-worker   后台任务 worker，领取并执行 workflow_jobs
```

数据通过宿主机目录持久化：

```text
./data:/app/data
```

当前队列是 DB-backed queue，适合本地演示和作品集验收。生产化下一步建议：

- PostgreSQL 替代 SQLite
- Redis + Celery/RQ 执行长任务
- 官方 MCP SDK 版本
- API 鉴权和角色权限
- OpenTelemetry / LangSmith / Langfuse
