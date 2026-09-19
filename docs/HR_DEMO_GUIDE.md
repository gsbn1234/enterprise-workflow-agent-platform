# HR 试用部署与演示指南

这份文档用于把 Agent 平台部署成一个 HR 可以直接打开试用的完整 Demo 环境。目标是让对方不用安装 Python、Node、PostgreSQL，也不用配置 SMTP，只通过浏览器体验完整功能。

## 1. 环境组成

完整版 HR Demo 由 `docker-compose.prod.yml` 定义，共 11 个服务：

| 服务 | 默认地址 | 作用 |
| --- | --- | --- |
| Agent 用户端/后台端（`workflow-agent`） | `http://127.0.0.1:8010` | 业务请求、审批、trace、workflow、审计、IT 服务面板 |
| Agent Worker（`workflow-worker`） | 无网页端口 | 从 Redis 队列领取并执行异步任务 |
| Outbox 派发（`workflow-outbox-dispatcher`） | 无网页端口 | 把 outbox 事件派发到外部系统 |
| 数据保留清理（`workflow-retention-worker`） | 无网页端口 | 按保留策略定期清理审计、邮件、日志等数据 |
| 迁移与初始化（`workflow-migrate`） | 无网页端口 | 一次性执行 schema 迁移与 demo 数据 seed，跑完即退出 |
| Agent PostgreSQL（`agent-postgres`） | `127.0.0.1:5433` | Agent 业务主库 |
| Redis（`agent-redis`） | `127.0.0.1:6379` | 异步任务队列信号 |
| 外部工单系统（`external-ticket-service`） | `http://127.0.0.1:8020` | 模拟 Jira/Zendesk/内部工单系统 |
| 本地 OIDC Provider（`local-oidc-provider`） | `http://127.0.0.1:8030` | 演示用 OIDC 登录与 JWKS |
| RAG 知识库（`rag-app`） | `http://127.0.0.1:8000` | 企业知识库问答、文档检索、引用依据 |
| PostgreSQL + pgvector（`rag-postgres`） | `127.0.0.1:5432` | RAG 业务库和向量库 |

## 2. 目录要求

完整版 HR Demo 需要 RAG 项目和 Agent 项目并列放置。本仓库就是 Agent 项目，它的父目录下需要有一个同级的 RAG 项目目录：

```text
<parent>\
  enterprise-workflow-agent-platform\   ← 本仓库（Agent 平台）
  enterprise-knowledge-rag\             ← RAG 知识库项目
```

`docker-compose.prod.yml` 中 `rag-app` 的构建上下文默认就是这个相对路径：

```text
../enterprise-knowledge-rag
```

如果你的 RAG 目录名不同，用 `RAG_BUILD_CONTEXT` 覆盖：

```powershell
$env:RAG_BUILD_CONTEXT="..\你的RAG项目目录"
```

如果你在云服务器上部署，也建议保持同样的并列目录结构。

## 3. 本地或云服务器启动

在 Agent 项目（本仓库）根目录执行下面的命令：

复制 HR Demo 环境变量模板：

```powershell
Copy-Item .env.hr-demo.example .env.hr-demo
```

如果是云服务器，把 `.env.hr-demo` 里的这一项改成 HR 能访问的地址：

```text
TICKET_SERVICE_PUBLIC_BASE_URL=http://你的服务器IP:8020
```

启动完整环境：

```powershell
docker compose --env-file .env.hr-demo -f docker-compose.prod.yml up --build -d
```

查看状态：

```powershell
docker compose --env-file .env.hr-demo -f docker-compose.prod.yml ps
```

查看日志：

```powershell
docker compose --env-file .env.hr-demo -f docker-compose.prod.yml logs -f workflow-agent
docker compose --env-file .env.hr-demo -f docker-compose.prod.yml logs -f rag-app
docker compose --env-file .env.hr-demo -f docker-compose.prod.yml logs -f external-ticket-service
```

停止：

```powershell
docker compose --env-file .env.hr-demo -f docker-compose.prod.yml down
```

停止并清空演示数据：

```powershell
docker compose --env-file .env.hr-demo -f docker-compose.prod.yml down -v
```

## 4. 给 HR 的访问地址

本机演示：

```text
用户端：http://127.0.0.1:8010
后台端：http://127.0.0.1:8010/admin
外部工单：http://127.0.0.1:8020
RAG 知识库：http://127.0.0.1:8000
```

云服务器演示：

```text
用户端：http://你的服务器IP:8010
后台端：http://你的服务器IP:8010/admin
外部工单：http://你的服务器IP:8020
RAG 知识库：http://你的服务器IP:8000
```

如果你配置了域名和 HTTPS，可以改成：

```text
用户端：https://agent-demo.example.com
后台端：https://agent-demo.example.com/admin
外部工单：https://ticket-demo.example.com
RAG 知识库：https://rag-demo.example.com
```

## 5. 试用账号

Agent 平台账号：

| 角色 | 账号 | 密码 | 可演示内容 |
| --- | --- | --- | --- |
| 普通员工 | `alice` | `AlicePass123` | 提交业务请求、查看处理结果 |
| 全局管理员 | `admin` | `AdminPass123` | 查看全部审批、trace、workflow、审计、工单、邮件 |
| 客服审批 | `cs_manager` | `ManagerPass123` | 审批客服/退款类请求 |
| 财务审批 | `finance_manager` | `ManagerPass123` | 审批退款/财务动作 |
| 安全审批 | `security_manager` | `ManagerPass123` | 审批安全事件 |
| IT 权限审批 | `it_manager` | `ManagerPass123` | 审批权限申请 |
| 采购审批 | `procurement_manager` | `ManagerPass123` | 审批采购流程 |
| 故障审批 | `sre_manager` | `ManagerPass123` | 审批 P1/P0 故障流程 |
| IT 支持 | `E001` | `E001Pass123` | IT 服务面板：提交 IT 请求、查看处理链路（角色 `it_support`） |

`E001` 属于 IT demo 数据（`app/services/it/mock_data.py`），随 IT 目录数据一起 seed，和上面的 HR 演示账号来源不同。

RAG 知识库默认关闭强鉴权，HR 可以直接查看问答页。Agent 调用 RAG 不需要 HR 单独登录。

## 6. 推荐给 HR 的完整试用流程

### 6.1 低风险自动处理

打开用户端，用 `alice / AlicePass123` 登录。

点击：

```text
远程办公
```

预期效果：

- 用户端显示执行进度。
- Supervisor 识别为远程办公/People Ops 场景。
- RAG Agent 查询远程办公政策。
- Risk Agent 判断低风险，无需人工审批。
- Tool Agent 创建工单并自动完成。
- 外部工单系统出现对应工单，状态为 `approved` 或已完成。

这一步证明：Agent 能自动处理低风险业务，不只是生成文本。

### 6.2 高风险退款审批

用户端点击：

```text
退款投诉
```

或输入：

```text
客户 Orbit Retail 投诉上月服务中断，要求退费 800 元，请创建工单并准备回复 fjsmlfy@gmail.com
```

预期效果：

- Agent 识别为退款投诉。
- RAG 返回客服/退款政策依据。
- Risk Agent 判断涉及退款金额，需要人工审批。
- 用户端显示进入审批等待。

切到后台端，用 `admin / AdminPass123` 登录。

展示：

- 审批队列中出现退款请求。
- 审批卡片包含用户请求、拟创建工单、金额、风险、审批链、知识依据。
- 点击“通过并创建工单”。

通过后展示：

- workflow 继续推进。
- 外部工单系统出现退款工单。
- 邮件区域出现邮件记录。

默认 HR Demo 使用 `AGENT_EMAIL_PROVIDER=mock`，所以邮件会记录为已发送/已生成，但不会真的从你的私人邮箱发出。

### 6.3 拒绝审批

用户端点击：

```text
采购审批
```

后台端找到对应审批，点击拒绝。

预期效果：

- 流程终止。
- 不继续创建高风险外部工单。
- 审计日志记录审批人、审批结果和拒绝原因。

这一步证明：人工审批不是装饰，审批结果会真正影响工具调用。

### 6.4 安全事件

用户端点击：

```text
安全事件
```

预期效果：

- Agent 识别为安全事件。
- Risk Agent 阻止删除数据等破坏性动作。
- 只创建安全工单、通知安全团队、保留审计。
- 后台审批后，外部工单进入 `investigating`。

这一步证明：Agent 有安全边界，不会盲目执行危险指令。

### 6.5 自然语言查工单

用户端点击：

```text
查询工单
```

或输入：

```text
查询 Customer Success 部门工单
```

预期效果：

- Agent 识别为查询动作。
- 不创建新工单。
- 返回当前可见工单列表。

### 6.6 自然语言改工单

先在外部工单系统复制一个工单号，例如：

```text
EXT-000004
```

用户端输入：

```text
把 EXT-000004 优先级改成 urgent，并备注：客户已二次催促，升级处理
```

预期效果：

- Agent 识别为修改工单。
- 检查权限和风险。
- 外部工单优先级变成 `urgent`。
- 工单时间线追加备注。

这一步证明：Agent 能查询和修改业务对象，不只是创建工单。

### 6.7 IT 服务闭环（入口指针）

完整的 IT 演示流程由 `docs/AGENT_DEMO_PLAYBOOK.md` 承载，这里只列入口：

- 入口在用户端的「IT 服务」面板，提交后由前端调用 `POST /api/it/requests`；请求体字段是 `objective`（不是 `text`）。
- 链路读回用 `GET /api/it/requests/{ticket_id}/chain`，一次返回 `triage`、`resolution`、`risk_decision`、`execution`、`history`、`approval`、`events` 和 `audit`，不用重放 run。
- 演示账号：`E001 / E001Pass123`，角色 `it_support`。

## 7. 后台重点展示区域

后台端重点展示：

- 审批队列：高风险流程暂停等待人工决策。
- 多智能体轨迹：Supervisor、RAG Research、Risk Approval、Tool Execution、Critic、Memory 的输出。
- 工作流轨迹：每个节点、工具输入、工具输出、重试、失败原因。
- 业务产物：工单、邮件、知识依据。
- 审计记录：谁在什么时候做了什么动作。
- 指标概览：请求数、工单数、邮件数、任务数、健康状态。

## 8. 邮件策略

HR Demo 默认不要使用你的私人 SMTP 授权码。

默认配置：

```text
AGENT_EMAIL_PROVIDER=mock
```

效果：

- 页面会产生邮件记录。
- workflow 和审计会记录邮件动作。
- 不会真的发送邮件到外部邮箱。

如果你一定要演示真实发送，建议使用专门的 Demo 邮箱，并在 `.env.hr-demo` 中配置：

```text
AGENT_EMAIL_PROVIDER=smtp
AGENT_EMAIL_ALLOWLIST=hr@example.com,@your-demo-domain.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=agent.demo@example.com
SMTP_PASSWORD=你的 Demo 邮箱授权码
SMTP_FROM_EMAIL=agent.demo@example.com
SMTP_USE_TLS=true
```

不要把 QQ/Gmail 私人授权码放到公开服务器或提交到 Git。

## 9. 给 HR 的简短说明

可以直接发：

```text
这是一个企业业务流程自动化 Agent Demo。

用户端：http://你的服务器IP:8010
后台端：http://你的服务器IP:8010/admin
外部工单：http://你的服务器IP:8020

普通用户账号：alice / AlicePass123
后台管理员：admin / AdminPass123
IT 服务演示账号：E001 / E001Pass123

建议试用顺序：
1. 用 alice 在用户端提交“远程办公”，看低风险自动完成。
2. 提交“退款投诉”，看 Agent 查询知识、判断风险并进入审批。
3. 用 admin 在后台审批，通过后看外部工单和邮件记录。
4. 提交“安全事件”，看危险动作被阻止和审计保留。
5. 尝试“查询工单”和“修改工单”，看自然语言操作业务对象。
6. 用 E001 打开「IT 服务」面板提交一个 IT 请求，看链路与风险门禁。
```

## 10. 安全注意事项

- `.env.hr-demo` 不要提交到 Git。
- `AGENT_AUTH_TOKEN_SECRET`、`RAG_AUTH_TOKEN_SECRET`、`TICKET_SERVICE_TOKEN` 上云前要替换。
- 默认不要开启真实 SMTP。
- 只使用演示数据，不上传真实客户资料。
- HR 试用结束后执行 `docker compose --env-file .env.hr-demo -f docker-compose.prod.yml down -v` 清空数据。
- 如果是临时云服务器，试用结束后直接关机或释放实例。
