# Phase 5 最终报告 — Enterprise IT Service Agent 工程化收尾

**HEAD 未变**：`b08da7f` · **0 个未推送提交** · **未 commit、未 push**（遵守 §十九）

---

## 1. 修改的文件

### 本次 Phase 5 我改的

| 文件 | 改动 |
|---|---|
| `README.md` | **整体重写**（181 → 546 行） |
| `.gitignore` | 重写：补 `*.sqlite3`/`*.db`/OS 杂物；删除失效的 `frontend/dist/` 规则；加产物同步说明 |
| `docs/AGENT_DEMO_PLAYBOOK.md` | 重写（134 → 735 行） |
| `docs/DOCKER_DEPLOYMENT.md` | 修正 7 处失真（最严重的一份） |
| `docs/AGENT_BUILD_JOURNEY.md` | 队列/前端路径/已完成项 三处修正 |
| `docs/HR_DEMO_GUIDE.md` | 删除 `F:\VScode-project\...` 本机路径；服务表 5 → 11 |

### Phase 1–4 遗留的未提交改动（本次未动，需一并提交）

`app/` 下 14 个模块、`frontend/src/{App.jsx,styles.css}`、`scripts/evaluate.py`、`.github/workflows/ci.yml`、`app/static/react/index.html`

---

## 2. 删除的文件

```
app/static/react/assets/index-ChIr6tUZ.js    (254 KB, 旧构建)
app/static/react/assets/index-DM-aUyuA.css   ( 23 KB, 旧构建)
```

删除依据（三条独立验证）：

1. 两个哈希在全仓库无任何引用；
2. `index.html` 只指向新的 `index-Q3VXjl5m.js` / `index-Btz-jzM3.css`；
3. **重跑 `npm run build` 后哈希与已提交产物完全一致，且未再生成这两个文件** —— 证明它们是旧构建残留而非活文件。

恢复：`git checkout HEAD -- app/static/react/assets/index-ChIr6tUZ.js`

---

## 3. 新增文档

| 文档 | 行数 | 覆盖 |
|---|---|---|
| `docs/EVALUATION.md` | 558 | §八 |
| `docs/ENGINEERING_IMPROVEMENTS.md` | 480 | §九 |
| `docs/KNOWN_LIMITATIONS.md` | 190 | §十 |
| `docs/INTERVIEW_PROJECT_OVERVIEW.md` | 1433 | §十六 |
| `docs/PHASE3_REPORT.md` / `docs/PHASE4_REPORT.md` | — | 移入 `docs/` 保留 |

---

## 4. README 的改动

**修掉的头号缺陷**：原架构图是 `Risk Consensus → Tool Execution`，**`risk_gate` 在全文出现 0 次** —— 读起来像是「LLM 投票说了算」，与实际相反。现已插入确定性门并标红。

其余改动：

- 新增 §五 Mermaid 架构图（零新前端依赖）
- 21 个节点全部列出并附完整边列表
- IT 分支与 `app/services/it/` 项目结构
- §四 的 17 个问题逐条作答
- §十五 定位句
- 测试入口（§十一 只做文档，不加 runner）
- 新文档链接

### 写 README 时我自己抓出并修掉的两条错误断言

两条都经**实测**，不是转述：

1. `/api/audit-logs` **不支持** `event_type` 过滤（只收 `limit`，clamp 1..500）—— 我原稿写错了；
2. `AGENT_AUTH_REQUIRED=false` **不等于 IT 接口免登录**：`POST /api/it/requests` 未带 token 直接 **401**。
   源码注释写明理由：

   > *"Authentication is mandatory here regardless of `settings.auth_required`: the IT intake's
   > authorization model is entirely built on who is asking, so an anonymous submission has no meaning."*

   7 个 IT 路由里只有 `GET /api/it/triage/preview` 是匿名的。

---

## 5. Demo 场景

| 场景 | 输入 | 实测读数 |
|---|---|---|
| **A** | `VPN 连不上，远程办公中断了` | `auto_execute` / `diagnostic_read` / `executed: true` / `mutating_tool_calls: 0` |
| **B** | `预发环境的 Redis 缓存需要清理` | 资产 REDIS-001 = production+critical → `require_approval` / `production_side_effect` |
| **C** | `预发环境的 Redis 需要重启一下` + 拒绝 | run `cancelled` / 工单 `rejected` / **Tool Calls = 0** |

- 场景 A 的读数取自 `phase4_results.jsonl` 的 `it-vpn-gateway-down`，**非推测**。
- **未采用规格里的「Redis 服务当前是否正常？」** —— 它被判成 `service_restart` 走审批路径，不是 ALLOW。

---

## 6. Evaluation 结果（§十二，第三次独立复现）

`phase5_it`：**20/21，pass_rate 0.9524**，16 项数值指标与 Phase 4 及 `phase4_conf` **逐项一致**。

| 指标 | 值 |
|---|---|
| passed / total | 20 / 21 |
| pass_rate | 0.9524 |
| triage_accuracy | 1.0 |
| retrieval_recall_at_3 | 0.9524（**与 Phase 3 相同，未提升**） |
| resolution_action_accuracy | 1.0 |
| risk_decision_accuracy | 1.0 |
| approval_accuracy | 1.0 |
| unsafe_tool_execution_count | 0（相同） |
| **unexpected_auto_execution_count** | **0** |
| historical_hit_accuracy | 1.0 |
| historical_reference_accuracy | 1.0 |

唯一失败：`it-paid-software-request` / `failure = retrieval_ok` —— **未隐藏、未调松期望**。

---

## 7. 测试结果（§十二，未修改任何结果）

| 项 | 结果 |
|---|---|
| `compileall app scripts` | **PASS** |
| 15 个 smoke test（Phase 1–4 + 平台） | **15 / 15 PASS** |
| IT Evaluation | 20/21 |
| workflow Evaluation | 2/5（0.4，与 Phase 3/4 一致） |
| 前端构建 | **PASS**，哈希可复现 |
| compose YAML 校验 | **3/3 有效**（prod = 11 服务） |
| **Docker 实跑** | **NOT RUN — 本机未安装 Docker** |

15 个 smoke test 明细：`it_smoke_test` · `it_triage_smoke_test` · `it_resolution_smoke_test` · `it_history_smoke_test` · `it_rbac_smoke_test` · `it_evaluation_smoke_test` · `it_regression_smoke_test` · `multi_agent_smoke_test` · `multi_agent_coordination_smoke_test` · `self_correction_smoke_test` · `audit_integrity_smoke_test` · `mcp_smoke_test` · `memory_routing_smoke_test` · `trace_replay_smoke_test` · `crm_ticket_smoke_test`

---

## 8. Docker 状态

**未安装，未运行。**

`docker` / `docker-compose` 均不在 PATH。`docker-compose.prod.yml` 仅通过 YAML 语法解析，`docker compose config` 与容器启动**均未验证**。

该路径在 `docs/AGENT_DEMO_PLAYBOOK.md` 中已明确标注「本次未重新执行」。

> 如实记为未执行，**不以 YAML 校验冒充 Docker 验证**。

---

## 9. Git 状态

分支 `main`，与 `origin/main` 同步，**0 提交可推送**，工作区有未提交改动。

---

## 10. 未提交的改动

- **27 个已修改**（tracked）
- **2 个已删除**
- **18 个未跟踪**（含 `app/services/it/` 整包、7 个 IT 测试、2 个评测集、4 份新文档）

> `analysis_plan.md`（106 KB）是基线 `b08da7f` 的**正式设计文档**
> （「Enterprise IT Service Agent 二次开发方案」，设计日期 2026-09-18），
> **不是临时文件，已保留**。

---

## 11. 已知限制

完整 14 条见 `docs/KNOWN_LIMITATIONS.md`。最关键的五条：

1. `it_research_query` 仍未被消费（§二-13 明确不处理）；
2. 仍有 1 个评测失败（`retrieval_ok`），Phase 3 遗留的第四个独立问题；
3. **IT 数据全部是 Mock / 种子数据**，工具执行默认 mock 模式 —— 「工具被执行了」= mock 工具被调用，**不是企业系统真被改了**；
4. 未接入真实 ServiceNow / Jira / ERP；**不是生产部署**，无真实客户、真实工单量、QPS、SLA；
5. 评测是 21 条人工策划案例 + mock 数据，**不是生产基准**。

---

## 12. 建议的 commit message

```
docs: Phase 5 engineering closure — README rewrite, eval/limitations/interview docs, demo playbook

Documentation closure for the IT service agent platform. No core business logic
changed: the LangGraph graph, the deterministic risk gate, the RAG path and the
evaluation numbers from Phase 4 are all untouched.

README.md (181 → 546 lines)
- add the deterministic risk_gate to the architecture diagram. The old diagram
  went Risk Consensus -> Tool Execution, so it read as if the LLM risk vote were
  the last word; durable_executor.py:330-334 inserts a fail-closed pure function
  between them and that is what the executor actually obeys
- add a Mermaid system diagram, all 21 graph nodes with their edges, the IT
  branch, app/services/it/ in the project tree
- answer the 17 required questions; state the positioning sentence
- correct two wrong API claims found while writing it: /api/audit-logs accepts
  only `limit` (no event_type filter), and AGENT_AUTH_REQUIRED=false does NOT
  open the IT routes -- POST /api/it/requests returns 401 without a token,
  because IT intake authorizes on who is asking

New docs
- EVALUATION.md            metrics, Phase 3 baseline vs Phase 4, full runs
- ENGINEERING_IMPROVEMENTS.md  P0/P1/P2 in Problem -> Detection -> Root Cause ->
                           Fix -> Regression Test -> Evaluation Result form
- KNOWN_LIMITATIONS.md     14 current limits + 7 future directions
- INTERVIEW_PROJECT_OVERVIEW.md  30s/1min/3min intros and the full case study

Rewritten / corrected
- AGENT_DEMO_PLAYBOOK.md   134 -> 735 lines; every step re-verified against a
                           live server (auth, objective field, approve/reject,
                           chain keys). Drops unverifiable claims
- DOCKER_DEPLOYMENT.md     healthcheck is /api/readiness not /api/health; prod
                           compose runs 11 services not 6; queue defaults
- AGENT_BUILD_JOURNEY.md   Redis queue has landed; frontend path is
                           frontend/ -> app/static/react/
- HR_DEMO_GUIDE.md         drop hardcoded F:\VScode-project paths

.gitignore
- ignore *.sqlite3 / *.db anywhere (they hold audit trails and ticket content),
  drop the dead frontend/dist/ rule, document the committed-build convention

Cleanup
- remove two orphaned Vite bundles that no index.html referenced. Verified dead
  by rebuilding: the build reproduces the committed hashes and does not
  regenerate them.

Tests: compileall PASS; 15/15 smoke PASS; IT eval 20/21 (unchanged from Phase 4);
workflow eval 2/5; frontend build reproducible. Docker was NOT available on this
machine, so the container path was not executed -- only its YAML was parsed.

Mock/seed data throughout; not a production deployment.
```

---

**Phase 5 停止。** 未 commit、未 push。
