# 简历能力实现核验

## 核验结论

| 简历表述 | 当前实现 | 结论 |
| --- | --- | --- |
| LangGraph 编排规划、检索、风控、审批、工具执行、复核 | `app/services/multi_agent/durable_executor.py` | 已实现，并补齐并行 fan-out/fan-in 与真实交接 |
| PDF/DOCX/MD/TXT 解析、Chunk、Embedding、引用 | RAG 项目的 parser/chunker/embeddings/rag | 已实现；另外支持 CSV/XLSX、OCR 与表格父子索引 |
| 向量 + BM25/关键词 + Rerank | RAG 项目的 retriever/reranker/pgvector_store | 已实现；Rerank 为可选 Qwen Provider |
| StateGraph + Checkpointer + thread_id | durable executor 与 LangGraph SQLite checkpointer | 已实现 |
| 中断恢复 | LangGraph `interrupt` + `Command(resume=...)` | 已补齐；审批前不再提前完成多智能体 run |
| 历史记录与复杂链路定位 | messages/tasks/handoffs/checkpoints/trace/golden diff | 已实现 |
| RAG、政策、CRM、工单、邮件、审批等 MCP-style Tools | tools registry、HTTP manifest、stdio JSON-RPC | 已实现 |
| 多租户 CRM 与企业工单闭环 | `services/tools/crm.py`、`ticketing.py`、外部工单服务 | 已补齐客户 CRUD/检索/互动时间线、工单状态机/SLA/时间线、RBAC 与 Outbox 更新重试 |
| 高风险人工审批 | approval workflow + multi-agent interrupt/resume | 已实现 |
| 文档 ACL、用户/部门/角色过滤、审计 | RAG access_control/auth/audit | 已实现；补充 Agent→RAG 服务令牌，避免匿名伪造用户上下文 |
| PostgreSQL/pgvector | RAG 生产配置使用 PostgreSQL + pgvector；Agent 生产配置使用 PostgreSQL | 已实现；离线 smoke 使用 SQLite |
| vLLM + Qwen2.5-32B-Instruct-AWQ | `docker-compose.vllm.yml` 与 Agent/RAG vLLM adapter | 已提供可部署配置；未在当前无 GPU 环境实际拉起 32B 模型 |

## 原实现中发现并修复的问题

1. 多 Agent 只是固定串行包装，Research/Risk/Memory 输出没有影响执行。
2. 执行器重复规划和重复检索，造成成本、延迟和轨迹含义不一致。
3. Checkpointer 虽存在，但没有真正的审批中断节点和恢复入口。
4. 高风险 workflow 等待审批时，多智能体 run 已被错误标记为 completed。
5. Critic 对 workflow 失败仅轻微扣分，失败链路仍可能通过质量门禁。
6. Agent 调用 RAG 时漏传 `user_role`，且生产模式没有服务到服务身份凭据。
7. 多个 RAG SQLite smoke 只设置 `RAG_DB_PATH`，但默认 backend 已变成 PostgreSQL，导致测试连接超时。
8. Trace replay 直接比较并行 Agent 的非确定完成顺序，容易产生假差异；现在使用规范化任务序列和交接边。
9. 低风险任务会跳过独立 Risk Agent；现在所有执行路径都经过双风险投票，只有不需要政策证据的现有工单命令会跳过 Research。
10. Evidence Synthesis 只统计数量，执行器仍重新拼装证据；现在发布去重后的共享 evidence 包并由执行器直接消费。
11. 审批重复/并发决策可能重复恢复执行；现在使用原子 compare-and-set 和工具幂等保护。
12. Critic 失败后可能在已产生外部副作用的情况下重跑完整 workflow；现在只允许无成功副作用的安全重试。
13. CRM 原先只有全局客户查询，缺少 tenant、CRUD 和互动记录；现在使用 tenant + email 唯一性、部门权限和客户时间线。
14. 工单状态可任意跳转，外部更新失败不可恢复；现在使用显式状态机、真实 SLA 时间、操作时间线和 `ticket.update` Outbox 重试。
15. 外部工单看板在启用服务 Token 后无法操作且 GET 会泄露数据；现在 API 支持 Bearer 服务身份，看板使用 Basic 登录，健康检查保持公开。

## 仍需如实说明的边界

- `docker-compose.vllm.yml` 是 GPU 部署能力，不等于已经在真实 GPU 服务器完成压测和上线。实际部署后再在简历使用“部署并接入”；此前建议写“提供/支持 vLLM 部署”。
- Rerank 默认关闭；需要配置 `RAG_RERANK_PROVIDER=qwen` 和密钥才会发生真实模型 Rerank。
- 本地 `local_hash` embedding 适合 smoke/demo；生产应使用稳定的语义 Embedding 模型并重建向量索引。
- 两个代码仓库作为 Agent 服务与 RAG 服务保持独立边界，由 `docker-compose.prod.yml` 统一编排；这是服务级合并，不是把两个 Python `app` 包硬塞进同一进程。
- 未启用 Qwen/vLLM 时，专家 Agent 是独立职责、独立状态和独立决策的确定性 Agent，不应描述成“多个自治 LLM 自由对话”；启用角色推理配置后才会发生多次独立模型调用。
- LangGraph 当前使用共享卷上的 SQLite checkpointer，适合单 Agent API 实例；需要水平扩容多个 API 实例时，应迁移到共享的 PostgreSQL checkpointer。
