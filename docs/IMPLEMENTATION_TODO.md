# 企业多智能体 Agent 平台 TODO

这份 TODO 按“从 0 到可写进简历，再到生产级系统”的顺序组织。当前项目已经完成作品集核心闭环：多智能体编排、企业工具调用、人工审批、RAG 工具接入、MCP-style 服务、trace、审计、评测和 Docker。

## Phase 0：项目定位与基础工程

- [x] 明确目标：企业业务流程自动化 Agent 平台
- [x] 从普通 RAG 项目差异化为 Agentic Workflow + Multi-Agent 项目
- [x] 创建 `app/`、`scripts/`、`docs/`、`sample_data/`、`data/`
- [x] 选择 FastAPI + SQLite + 原生静态 UI + Docker Compose
- [x] 编写 `.env.example`、`requirements.txt`、`Dockerfile`、`docker-compose.yml`

## Phase 1：核心数据模型

- [x] `business_requests`：业务请求
- [x] `workflow_runs`：单 Agent workflow 运行
- [x] `workflow_steps`：每个节点/tool call 的 trace
- [x] `workflow_jobs`：异步任务队列
- [x] `approvals`：人工审批
- [x] `customers`：演示 CRM
- [x] `tickets`：演示工单系统
- [x] `emails`：演示邮件记录
- [x] `knowledge_articles`：本地政策知识库
- [x] `audit_logs`：审计日志
- [x] `eval_reports`：评测报告
- [x] `multi_agent_runs`：多智能体运行
- [x] `multi_agent_messages`：子 Agent 消息轨迹
- [x] `agent_memory`：成功案例与失败模式记忆
- [x] `agent_checkpoints`：业务可读的 LangGraph checkpoint 镜像
- [x] `golden_traces`：可复用的黄金轨迹
- [x] `trace_replays`：历史 run 重放和 diff 报告
- [ ] PostgreSQL + Alembic 迁移
- [x] tenant_id / workspace_id 多租户字段

## Phase 2：单 Agent Workflow

- [x] Guard：拒绝绕过审批、绕过审计、删除数据等危险指令
- [x] Planner：分类、金额抽取、邮箱识别、优先级、风险等级、审批判断
- [x] Tool Execution：RAG、知识库、CRM、工单、邮件、审批
- [x] 状态化结果：`completed`、`waiting_approval`、`cancelled`、`refused`
- [x] 审批通过后从暂停点恢复执行
- [x] 审批拒绝后安全取消 workflow
- [x] 每个节点记录 reasoning summary、输入、输出、耗时、状态
- [x] LangGraph durable executor 承载多智能体编排
- [x] SQLite checkpointer 保存 LangGraph thread 状态
- [ ] 单 Agent workflow 也迁移到 LangGraph durable executor
- [ ] checkpoint/resume 支持长任务中断恢复

## Phase 3：工具层与 MCP

- [x] `query_enterprise_rag`：企业 RAG 工具
- [x] `search_knowledge`：本地政策库检索
- [x] `lookup_customer`：CRM 查询
- [x] `create_ticket` / `update_ticket`：工单工具
- [x] `draft_email` / `send_email`：邮件工具
- [x] `request_approval`：审批工具
- [x] 工具注册表：JSON Schema、输出形态、副作用、审批要求、角色要求
- [x] MCP-style HTTP manifest
- [x] MCP stdio JSON-RPC：tools/list、tools/call、resources/read、prompts/get
- [x] MCP 工具调用写入 audit
- [ ] 替换为官方 MCP SDK
- [ ] 工具权限矩阵：role / department / tenant / data scope
- [ ] 真实系统适配：Jira/Linear、Salesforce/HubSpot、Gmail/Outlook、Slack/Teams

## Phase 4：多智能体平台

- [x] Supervisor Agent：拆解任务、路由子 Agent
- [x] RAG Research Agent：调用企业 RAG 与本地政策库，整理证据
- [x] Risk & Approval Agent：判断风险、审批必要性、合规 warning
- [x] Tool Execution Agent：复用底层 workflow 落地业务动作
- [x] Critic Agent：检查 RAG、工单、审批、失败 step、retry attempt
- [x] Memory Agent：检索相似历史案例，写入成功/失败记忆
- [x] 多智能体 API：run/list/detail/memory
- [x] 多智能体 UI：展示子 Agent 轨迹、critic 分数、workflow 关联
- [x] 子 Agent 失败时把 multi-agent run 标记为 failed
- [x] Supervisor 路由结果影响是否调用 Risk Agent
- [x] LangGraph `StateGraph` 编排多智能体节点
- [x] `thread_id` + SQLite checkpointer 保存 durable 状态
- [x] Critic-driven self-correction：critic 失败后自动生成修复要求并重新执行一次
- [x] Trace replay：基于历史 objective 和 requester 重新运行
- [x] Golden trace diff：比较 Agent 序列、tool 序列、checkpoint 序列和 workflow 状态
- [ ] 多 Agent 并行执行
- [ ] Multi-agent debate / vote：高风险场景多策略投票
- [ ] Memory-augmented planning：相似案例真正影响下一次计划

## Phase 5：Human-in-the-loop

- [x] 高风险动作进入人工审批
- [x] 审批 payload 保留 plan、ticket、email draft、knowledge evidence、RAG citations
- [x] manager/admin 可审批
- [x] 审批后恢复执行或取消
- [x] 审批行为写入审计
- [ ] 多级审批链
- [ ] 审批超时升级
- [ ] 审批人部门/金额权限

## Phase 6：可观测性、审计与指标

- [x] workflow trace
- [x] multi-agent message trace
- [x] audit log
- [x] metrics summary
- [x] retry attempt trace
- [x] durable agent checkpoint trace
- [x] trace replay / golden diff 报告
- [x] eval reports
- [ ] OpenTelemetry trace/span
- [ ] Langfuse/LangSmith trace adapter
- [x] trace replay CLI
- [x] golden trace diff CLI/API
- [ ] trace replay 页面
- [ ] golden trace diff 可视化页面

## Phase 7：评测体系

- [x] 单 Agent workflow eval：分类、审批、工具完整性、RAG 调用、任务完成、成本、延迟
- [x] 多智能体 harness：expected agents、critic score、workflow status、approval accuracy
- [x] self-correction smoke test
- [x] trace replay smoke test
- [x] 评测结果写入 JSON/JSONL 与 `eval_reports`
- [x] smoke tests 覆盖 workflow、async job、auth、RAG、MCP、retry、multi-agent
- [ ] LLM-as-judge adapter
- [ ] adversarial prompt injection 评测集
- [ ] 成本/延迟回归阈值
- [ ] CI 自动跑 smoke + harness

## Phase 8：管理台

- [x] 第一屏就是工作台，不做 landing page
- [x] 发起单 Agent workflow
- [x] 发起多智能体运行
- [x] 入队异步执行
- [x] 展示多智能体轨迹
- [x] 多智能体卡片支持保存 Golden 和 Replay
- [x] 展示 workflow trace
- [x] 展示审批、job、指标、工单、邮件、知识库
- [x] 登录/退出与 token 自动附加
- [ ] run detail 抽屉
- [ ] eval report 页面
- [ ] trace diff 页面
- [ ] 用户/角色管理页面

## Phase 9：部署与工程化

- [x] Dockerfile
- [x] Docker Compose Web + Worker
- [x] Docker healthcheck
- [x] Docker smoke test
- [x] 文档化本地与 Docker 启动
- [ ] pytest 单元测试拆分
- [ ] ruff / mypy
- [ ] GitHub Actions
- [ ] PostgreSQL 生产配置
- [ ] Redis/Celery 或 RQ
- [ ] secret management

## Phase 10：求职包装

- [x] README
- [x] 实现过程文档
- [x] 多智能体 TODO
- [x] 演示数据
- [x] 自测与评测脚本
- [x] Docker 可运行
- [ ] 架构图
- [ ] 2 分钟演示视频
- [ ] 简历项目 bullet
- [ ] 面试讲解稿
