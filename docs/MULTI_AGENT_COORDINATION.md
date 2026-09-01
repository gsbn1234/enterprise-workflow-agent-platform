# 多智能体协作架构

## 为什么不再是“串行函数换名字”

旧实现虽然把步骤命名为 Supervisor、RAG、Risk、Tool、Critic 和 Memory，但存在三个关键断点：

1. Research 的证据没有交给 Tool Execution，执行器会再次调用 RAG。
2. Risk 的结论没有约束执行计划，只用于展示。
3. Memory 检索结果没有影响 Supervisor 的路由。

当前实现把这些输出变成显式共享状态和可审计交接记录。执行器接收 `plan_override`、`knowledge_override` 和 `risk_override`；风险共识只能升级风险或增加审批，不能绕过 Supervisor 已设置的审批要求。

## 任务图

```text
memory.retrieve
      |
plan.supervisor
      |
      +-------------------------------+
      |                               |
research.enterprise_rag     research.local_policy
      |                               |
      +------ research.synthesis -----+
                       |
             +---------+---------+
             |                   |
 risk.compliance_vote   risk.operational_vote
             |                   |
             +--- risk.consensus-+
                       |
                 action.execute
                       |
          waiting approval? ---- no ----> quality.critic
                 |
        LangGraph interrupt
                 |
       approval.resume (same thread_id)
                 |
             quality.critic
                 |
        pass -> memory.write -> END
        fail -> self_correction -> supervisor
```

两个 Research 节点和两个 Risk 节点由 LangGraph fan-out/fan-in 执行。它们写入不同的 State 字段，再由 synthesis/consensus 节点汇合，因此既有并行工作，也有明确的输入依赖。现有工单查询/更新由 Supervisor 动态走 `research_bypass`，避免无意义检索；风险投票不会被低风险标签跳过。

默认模式下，专家节点使用确定性策略，方便离线运行和稳定评测。启用 `AGENT_LLM_MULTI_AGENT_REASONING_ENABLED` 后，Evidence Synthesis、Compliance Risk、Operational Risk 和 Critic 会分别调用 Qwen/vLLM，并保留各自角色提示词和输出。模型建议只允许升级风险或增加质检问题，不能取消确定性策略要求的审批。

## 协作数据

- `multi_agent_tasks`：任务键、负责 Agent、依赖、轮次、输入、输出和状态。
- `multi_agent_handoffs`：发送 Agent、接收 Agent、对应任务和交接载荷。
- `multi_agent_messages`：每个 Agent 的角色输出、耗时和成功/失败状态。
- `agent_checkpoints`：面向业务排障的节点级状态快照。
- LangGraph checkpointer：面向执行恢复的内部状态，按 `thread_id` 持久化。

`GET /api/multi-agent/runs/{run_id}` 会同时返回 `messages`、`tasks` 和 `handoffs`；`GET /api/multi-agent/runs/{run_id}/trace` 会返回标准化任务序列和交接边。

## 人工审批与恢复

当 Tool Execution 创建审批并返回 `waiting_approval` 时，图进入 `human_approval` 节点并调用 LangGraph `interrupt`。此时：

- `multi_agent_runs.status=waiting_approval`；
- workflow 与 approval id 已持久化；
- checkpointer 保存尚未完成的 `human_approval` 节点；
- Critic 和 Memory 不会提前运行。

审批接口完成底层 workflow 后，`resume_multi_agent_for_workflow()` 使用原 `thread_id` 和 `Command(resume=...)` 恢复图，再执行 Critic、Memory 和 Finalize。

## 安全边界

- Prompt guard 始终在 Tool Execution 内再次执行，不接受上游 Agent 绕过。
- Risk Consensus 采用 `max_risk_and_any_approval_vote`：任一风险 Agent 要求审批即暂停。
- 历史 failure memory 会作为两个风险 Agent 的约束输入；重复执行失败可将低风险任务升级为人工审批。
- Self-correction 仅在此前没有成功副作用时自动重试；若已经创建工单、更新状态、发信、通知或创建审批，则停止重放并要求人工复核。
- 审批决策使用数据库 compare-and-set；重复或冲突决策不会二次创建工单或发送邮件。
- 共享计划只从进程内 Supervisor 传入，API 不开放任意 `plan_override`。
- Agent 到 RAG 的用户/部门/角色上下文使用 `X-RAG-Service-Token` 做服务身份认证。
- 外部工单、邮件等副作用仍经过现有审批、幂等、审计和 allowlist 约束。

## 验证

```powershell
.\.venv\Scripts\python.exe scripts\multi_agent_smoke_test.py
.\.venv\Scripts\python.exe scripts\multi_agent_coordination_smoke_test.py
.\.venv\Scripts\python.exe scripts\memory_routing_smoke_test.py
.\.venv\Scripts\python.exe scripts\self_correction_smoke_test.py
.\.venv\Scripts\python.exe scripts\trace_replay_smoke_test.py
.\.venv\Scripts\python.exe scripts\multi_agent_harness.py
```
