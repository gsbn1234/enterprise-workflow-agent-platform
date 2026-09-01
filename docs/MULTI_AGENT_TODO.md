# 前沿多智能体 Agent 平台路线图

这份路线图专门服务于“简历上不只是普通 Agent demo，而是前沿多智能体 Agent 平台”的目标。当前已经完成作品集级核心闭环，后续可以继续向 LangGraph、self-correction、trace replay、observability 和生产权限体系升级。

## 已完成：多智能体骨架

- [x] Supervisor Agent：从业务目标生成计划，并决定需要哪些子 Agent
- [x] RAG Research Agent：同时调用企业 RAG 与本地政策库，输出 evidence 与 selected source
- [x] Risk & Approval Agent：判断风险等级、审批必要性、合规 warning 与执行决策
- [x] Tool Execution Agent：复用底层 workflow 执行工单、邮件、审批等工具动作
- [x] Critic Agent：检查底层 workflow trace，包括 RAG tool、create_ticket、request_approval、failed steps、retry attempts
- [x] Memory Agent：检索相似历史案例，写入 success_case / failure_pattern
- [x] `multi_agent_runs`：记录多智能体 run 状态、critic score、关联 workflow、最终摘要
- [x] `multi_agent_messages`：记录每个子 Agent 的输入输出、角色、耗时和状态
- [x] `agent_memory`：记录案例记忆
- [x] LangGraph `StateGraph` durable executor
- [x] SQLite checkpointer 与 `thread_id`
- [x] `agent_checkpoints`：每个 Agent 节点的业务可读 checkpoint
- [x] 子 Agent 失败时标记 multi-agent run 为 failed，并写入 critic_report

## 已完成：API 与 UI

- [x] `POST /api/multi-agent/run`
- [x] `GET /api/multi-agent/runs`
- [x] `GET /api/multi-agent/runs/{run_id}`
- [x] `GET /api/multi-agent/runs/{run_id}/checkpoints`
- [x] `GET /api/multi-agent/runs/{run_id}/trace`
- [x] `GET /api/agent-memory`
- [x] `POST /api/trace-replay`
- [x] `POST /api/golden-traces`
- [x] `POST /api/golden-traces/diff`
- [x] 管理台支持多智能体运行按钮
- [x] 管理台展示多智能体消息轨迹、critic score、workflow_run_id、final_summary
- [x] 管理台支持保存 Golden 和 Replay

## 已完成：Agent Harness

- [x] 多智能体场景集：`sample_data/eval/multi_agent_scenarios.jsonl`
- [x] 多智能体烟测：`scripts/multi_agent_smoke_test.py`
- [x] 多智能体 harness：`scripts/multi_agent_harness.py`
- [x] 检查 expected agents 是否出现
- [x] 检查 critic score 是否达到阈值
- [x] 检查底层 workflow status 是否符合预期
- [x] 检查 category / approval 是否正确
- [x] 输出 `data/eval_reports/multi_agent_latest_summary.json`
- [x] 输出 `data/eval_reports/multi_agent_latest_results.jsonl`
- [x] 写入 `eval_reports`
- [x] Self-correction 烟测：`scripts/self_correction_smoke_test.py`
- [x] Trace replay 烟测：`scripts/trace_replay_smoke_test.py`

## 已完成：Durable / Self-Correction / Replay

- [x] LangGraph 版 multi-agent executor
- [x] durable checkpoint：每个 Agent 节点写入 LangGraph checkpointer
- [x] checkpoint mirror：将关键状态写入 `agent_checkpoints`
- [x] critic-driven self-correction：critic 低分时生成修复要求并重新执行一次
- [x] trace replay CLI：从历史 run 重放同一任务
- [x] golden trace diff：对比当前 trace 与黄金 trace 的 Agent 序列、工具序列、checkpoint 序列、workflow 状态
- [x] parallel fan-out/fan-in：企业 RAG / 本地政策并行检索，合规 / 业务风险并行投票
- [x] task / handoff trace：记录任务依赖、执行状态和 Agent 间交接载荷
- [x] human approval interrupt：审批时暂停 LangGraph，并用同一 `thread_id` 恢复
- [x] memory-augmented routing：历史 failure pattern 会增加 Supervisor 风控约束

## 下一步优先级 P0：继续拉开差异

- [x] parallel branches：双 Research 与双 Risk 分支并行执行
- [x] dynamic routing policy：Supervisor 根据历史案例、风险与审批要求路由
- [x] approval checkpoint resume：人工审批后从原 LangGraph thread 恢复
- [ ] checkpoint fork API：从任意历史 checkpoint 分叉出诊断 run
- [ ] golden trace 可视化 diff 页面

## 下一步优先级 P1：评测与可观测性

- [ ] LLM-as-judge adapter：对最终业务答复、风险说明、审批理由打分
- [x] 基础 adversarial smoke：绕过审批、prompt injection、敏感信息与越权调用
- [ ] 扩展 adversarial 数据集与持续红队评测
- [ ] failure taxonomy：分类记录 tool_error、planner_error、approval_miss、rag_unavailable、permission_denied
- [x] OpenTelemetry spans：HTTP、workflow run、job 与 tool step
- [ ] Agent message、外部 HTTP 与数据库查询的细粒度 spans
- [ ] Langfuse / LangSmith adapter：把 trace 推送到外部可观测平台
- [ ] 成本/延迟回归阈值：harness 失败时标记 regression

## 下一步优先级 P2：生产化

- [x] PostgreSQL 生产后端与 schema migration
- [ ] Alembic 版本化迁移
- [x] Redis Queue + 独立 Worker
- [x] 多租户 tenant isolation + PostgreSQL RLS
- [x] 核心工具权限：role、department、tenant、ticket data scope
- [ ] 全连接器细粒度 action scope
- [ ] 多级审批链
- [ ] 审批超时升级
- [x] Jira、通用 HTTP 工单和 SMTP 邮件连接器
- [ ] Linear、Salesforce/HubSpot、Gmail/Outlook、Slack/Teams 连接器
- [ ] 官方 MCP SDK 替换手写 stdio JSON-RPC

## 简历对齐点

当前已能支撑这些简历表述：

- 多智能体协作：Supervisor / RAG Research / Risk / Tool Execution / Critic / Memory
- 状态化任务编排：底层 workflow trace + 多智能体 message trace
- Tool calling：CRM、工单、邮件、审批、RAG、知识库
- Human-in-the-loop：高风险任务审批与恢复执行
- RAG as Tool：企业知识库 RAG 作为正式 Agent 工具接入
- MCP-style Tool Service：工具 manifest、JSON Schema、resources、prompts、审计
- Agent Harness：expected agents、critic score、approval accuracy、workflow status accuracy
- Durable execution：LangGraph StateGraph、SQLite checkpointer、thread_id、agent checkpoints
- Self-correction：Critic 低分触发修复要求、重新执行和二次 Critic
- Replay / diff：trace export、trace replay、golden trace diff
- 可观测性：trace、audit、metrics、eval report、retry attempt
