# Phase 6 最终报告 — 功能分类 + 一键 Demo / Showcase

**HEAD 未变**：`e67dc21` · **未 commit、未 push、未改 `git config user.name/user.email`**（遵守 原则十四）
**后端业务逻辑零改动**：`risk_gate.py` / `triage.py` / `actions.py` / `execution.py` / `intake.py` / `history.py` /
`rbac.py` / `tools.py` / `mock_data.py` / `durable_executor.py` / `agents.py` / `tools/knowledge.py` / `registry.py` /
`db.py` / `schemas.py` / `sample_data/eval/*.jsonl` / `scripts/evaluate.py` / 既有 smoke test / `Dockerfile` /
`docker-compose*.yml` **一个字节都没动**。

---

## 1. 修改的文件

### 新增 6 个源文件

| 文件 | 行数 | 作用 |
|---|---|---|
| `app/services/it/demo_scenarios.py` | 269 | 场景投影：从**已提交**的评测集读出 21 条场景 + 7 类能力 + 10 项能力亮点 + 历史评测数字。纯读取，不 import 应用、不连库、不调 LLM |
| `frontend/src/ShowcasePage.jsx` | 684 | `/showcase` 页面：8 个区块，一键跑真实场景 |
| `frontend/src/ITChain.jsx` | 288 | 执行链组件。**从 `App.jsx` 整体搬出**，让 IT 面板与 Showcase 共用同一份链路渲染 |
| `frontend/src/ui.jsx` | 37 | `Card` / `PanelHeader` / `IconButton` / `Status` / `short`。**同样是从 `App.jsx` 搬出的既有组件**，不是新写的 |
| `demo.bat` | 170 | 一键启停：`start` / `stop` / `status` |
| `scripts/it_showcase_smoke_test.py` | 642 | 新增 17 项测试 |

### 修改 4 个文件

| 文件 | 改动 |
|---|---|
| `app/main.py` | +19 行：`GET /showcase` 页面路由；`GET /api/it/demo-scenarios` 只读元数据接口。**没有新增任何业务逻辑** |
| `frontend/src/App.jsx` | **净减 293 行**：新增 `/showcase` 路由分支与一个导航按钮；把 `ITChain` 与 5 个 UI 组件搬去独立文件；其余是 import 调整 |
| `frontend/src/styles.css` | +315 行：`.showcase-*` 布局、`.it-risk-facts`。只新增，未改任何既有规则 |
| `app/static/react/index.html` + `assets/` | 重新构建的产物（`index-MvZqG46J.js` / `index-V5JMN3Zp.css`），旧 hash 已删除，仓库里不留孤儿文件 |

### 关于 `ui.jsx`（比批准的「新增 5」多 1 个）

批准的方案是「新增 5 / 修改 4」。实际新增 6 个源文件，多出来的就是 `ui.jsx`。
它不是新写的代码 —— `Card` / `PanelHeader` / `IconButton` / `Status` / `short` 本来就定义在 `App.jsx` 里，
只是从未 `export`。要让 `ShowcasePage.jsx` 和 `ITChain.jsx` 用上它们，只有两条路：
在 `App.jsx` 里加 `export`（`App.jsx` 是默认导出组件，加命名导出会很怪），或者把它们搬到一个共享模块。
后者一次改动同时满足「不复制现有逻辑」和「`App.jsx` 只做必要的路由与导航修改」两条要求 ——
`App.jsx` 因此**变短了 293 行**，而不是变长。

---

## 2. 每个按钮背后的真实调用

页面不预置任何成功状态。执行链严格按 原则二 的次序，每一步都是真实 HTTP 调用，**没有一行是伪造的**：

| 步骤 | 真实调用 |
|---|---|
| 登录 | `POST /api/auth/login` |
| 建单 | `POST /api/it/requests` |
| 执行 Agent | `POST /api/it/requests/{ticket_id}/resolve` |
| 读真实链路 | `GET /api/it/requests/{ticket_id}/chain` |
| 查待审批队列 | `GET /api/approvals?status=pending` |
| 人工决定 | `POST /api/approvals/{approval_id}/decide` |
| 再读链路 | `GET /api/it/requests/{ticket_id}/chain` |
| 知识检索面板 | `GET /api/knowledge/search?q=…&limit=3` |
| 评测实时面板 | `GET /api/eval-reports`（admin） |
| 页头状态 | `GET /api/health` |

页面上的「调用链」区块逐条打印实际发出的请求与真实结果（状态码、ticket_id、decision、approval id），
**它只在调用真的发生之后才写那一行**。

> 实现过程中发现并修掉的一处自身缺陷：这一区块最初为 `GET /api/approvals?status=pending` 预写了一条
> 「已查询」的 trace 行，而那次调用实际上并没有发出。这不是「显示不准确」，是**在陈述一件没发生的事**。
> 现在它是一个真实的 `await api(...)`，employee 账号下它如实记录 `HTTP 403 · Manager or admin role is required.`。

---

## 3. Showcase 的功能分类（原则三：至少 7 类）

`DEMO_CAPABILITIES` 覆盖 7 类，每类都带 `id` / 标题 / 说明 / 真实 API 路径 / 真实模块与符号：

| # | 分类 | 页面标出的真实入口 |
|---|---|---|
| 1 | 智能受理与分流 | `POST /api/it/requests` · `app/services/it/triage.py · classify()` |
| 2 | 知识检索 / 历史工单检索 | `GET /api/knowledge/search` · `app/services/tools/knowledge.py` |
| 3 | Risk Gate 风险决策 | `GET /api/it/requests/{id}/chain` · `app/services/it/risk_gate.py · evaluate()` |
| 4 | Human Approval 人工审批 | `POST /api/approvals/{id}/decide` · `durable_executor.py · _human_approval_node()` |
| 5 | Mock Tool 工具执行 | `GET /api/it/requests/{id}/chain` · `app/services/tools/registry.py · call_tool()` |
| 6 | Audit / Observability 审计与可观测 | `GET /api/audit-logs` · `GET /api/metrics/summary` |
| 7 | Evaluation 评测结果 | `GET /api/eval-reports` · `scripts/evaluate.py` |

测试 `_capabilities_cover_the_seven_categories` 断言这 7 个 id 一个不少；
`_the_backend_entries_are_real_endpoints` 会**回到 `app/main.py` 的源码里**核对每个 API 路径真的存在，
`_highlights_are_ten_and_point_at_real_code` 核对每个「模块 · 符号」在真实文件里真的定义着。
写死的字符串会腐烂，所以让测试去读源码。

---

## 4. 十项差异化能力（原则四）

`DEMO_HIGHLIGHTS` 恰好 10 条，逐条指向真实代码，页面顶部独立成区（不是聊天窗口）：

LangGraph 21 节点执行链 · Triage · Knowledge Retrieval · Historical Ticket Retrieval · Risk Gate ·
RBAC · Human-in-the-loop Approval · Mock Tool Execution · Audit Trail · Checkpoint/Resume

---

## 5. Risk Gate 展示（原则五）

执行链的 **Risk Gate 风险门禁** 一步现在直接列出**服务端返回的**门禁输入：

`action_type` · `environment` · `criticality` · `evidence_count` · `actor_role` ·
`triage_needs_approval` · `llm_confidence`（含 `used=false`）· `missing_information` · `risk_class` · `mode`

外加标题行的 `decision` / `rule_id` / `executable` / `tool_name` / `reasons` —— 原则五点名的 9 个字段全部在列。
取值位置就是页面读取的位置（`risk_decision` 与 `risk_decision.inputs`），**服务器没给的字段渲染成 `-`，不在前端推断**。

**关于 `rule_id`**：只显示真实返回值。实测出现过的是
`read_only_action` / `production_side_effect` / `critical_asset_side_effect` /
`action_class_requires_approval:service_restart` / `action_class_requires_approval:permission_change` /
`request_more_information` 这类字符串。源码注释里的 `R0..R7` **是注释，不是值**，页面不会显示它们；
新增测试 `_the_risk_gate_block_is_backed_by_the_payload` 里有 `assert not re.fullmatch(r"R[0-7]", rule_id)` 守住这一点。

> 这一块是本轮**真正补上的缺口**。此前 Risk Gate 一步只显示 `decision` / `rule_id` / `executable` / `tool_name`，
> 而「为什么这次需要人？」恰恰是 Showcase 存在的理由 —— 输入就摆在响应里，没有理由不显示。

实测（本机 `/showcase`，admin 账号，场景「内网 DNS 解析失败，服务无法访问」）：

```
action_type = no_action          environment = production     criticality = critical
evidence_count = 0               actor_role = admin           triage_needs_approval = false
llm_confidence = 0 · used=false  missing_information = environment
risk_class = informational       mode = deterministic
→ require_approval
```

---

## 6. 检索的诚实性（原则六）

页面**不写「向量 RAG」**。原因不是措辞偏好，而是事实：`/api/health` 在本机返回 `rag=not_configured`，
检索走的是本地策略库的确定性关键词匹配。

- 链内的 Knowledge 证据与 Historical 证据**分成两步**展示，标签分别是
  「Knowledge 检索（正式知识 / 政策 / Runbook）」与「Historical Ticket 检索（历史工单，仅供参考）」。
- 独立的「知识检索（实时）」面板展示 `article_id` / `title` / `category` / `snippet` / `score` / `source` 六个字段，
  并逐字标注「**本地策略库 · 确定性关键词检索 —— 不是向量 RAG**」。
- 运行结果下方标注真实出处：`正式知识 N 条 · source=local_policy_db` /
  `历史工单 N 条 · source=historical_ticket_index` / `决议 mode=<真实值>`。
- 历史工单那一栏明写「仅作参考，不构成政策，也不会被用来选择动作」，与
  `resolution.historical_note` 里的措辞一致（测试 `_historical_evidence_is_a_separate_channel` 断言「仅供参考」确实存在）。

**一处如实记录的形状限制**：链里的 `resolution.evidence[]` 每条只有
`article_id / title / snippet / score / source`，**没有 `category`**。所以六字段齐全的那个面板
必然是一次**独立的实时检索**（`GET /api/knowledge/search`），与执行当时取到的那份证据不是同一次。
页面把两者的 `source` 分别标出，读者不会误认为同一份数据。详见 `docs/KNOWN_LIMITATIONS.md` 第 16 条。

**`resolution.mode` 的真实值**：不是 `deterministic_keyword`，实测有两种 ——
检索到正式知识时是 `deterministic`，没有可用知识时是 `deterministic_no_knowledge`。
页面直接打印这个字段本身，所以两种都会如实出现。

---

## 7. Mock Tool 标注（原则七）

工具执行一步展示 **Mock 工具被调用**，并在运行结果下方逐字写明：

> 工具执行一节显示的是 **Mock 工具**被调用，表示平台记录了这次操作，不代表任何真实基础设施被改动。
> 这里不会出现「已重启生产 Redis」这类说法，因为没有任何真实基础设施被操作过。

全仓库（`frontend/src/` + `app/services/it/demo_scenarios.py`）grep「已重启 / 已清理生产 / 已删除生产 / 已变更生产」，
**唯一的命中就是上面这句否定句本身** —— 也就是说，不存在任何一处声称操作了真实基础设施的文案。

`demo.bat` 把 `AGENT_TOOL_MODE=mock` 固定下来，不依赖环境里恰好是什么。

---

## 8. Evaluation（原则八）

- 标题逐字用「**Phase 4 Evaluation Result · 历史结果，非实时**」。
- 出处写在旁边：`record_id=eval_c345178b1227f15d`、`recorded_at=2026-09-18T17:20:36+00:00`、
  来源 `docs/EVALUATION.md §6`。
- 复现命令给出：`python scripts/evaluate.py --suite it --report-prefix phase4`。
- **不假装有实时结果**：「实时」的那一块挂在真实接口 `GET /api/eval-reports` 上，
  本机实测该接口要求 admin，且 `eval_reports` 表当前 0 行 —— 页面于是如实显示
  「0 条」，并附一句「全新 clone 上没有这些产物，本页不代为编造」。
  worker 400/401/403 分别给不同提示（需要登录 / 需要 admin 角色）。
- 历史数字**读的是代码里的常量**，不读 `data/eval_reports/`（该目录被 `.gitignore` 忽略，全新 clone 上不存在）。
  代价是重跑评测后常量不会自动更新 —— 已记入已知限制第 17 条。

---

## 9. 一键 `demo.bat`（原则九）

```
demo.bat            # 等价于 start
demo.bat start      # 检查 .venv → 启动 uvicorn :8010 → 等 /api/health → 打开 /showcase
demo.bat stop       # 只杀 server.pid 里记录的那一个 PID
demo.bat status     # 报告是否在跑
```

- **不依赖 Docker**：FastAPI 同时提供 API 和已构建的 UI，一个进程就够。
- **runtime 文件全部在 `data/demo/`**（`server.pid` / `server.log` / `server.err.log`）；
  `data/` 已在 `.gitignore` 中，无需新增规则。
- **`stop` 只杀一个进程**：`taskkill /PID %SERVER_PID% /T /F`。文件里**没有任何按镜像名杀进程的语句**，
  全文 grep `taskkill /im` / `taskkill /f /im` 均为零命中 —— 一个 demo 不该能带走机器上所有 python/node 进程。
- 已有实例在跑时不会重复启动；pid 文件过期（进程已死）会自动清理后重新启动。

### 为什么固定 `AGENT_AUTH_REQUIRED=true`

`auth_required` 的默认值是 `false`，此时审批端点的角色校验整体不生效。
实测：默认配置下 **employee 账号 alice 可以经 HTTP 批准一次生产变更（200）**。
而 Showcase 的 RBAC 卡片声称「审批按钮会禁用，服务端也会独立拒绝（403）」——
在那个配置下这句话是**假的**：界面描述的是一个被禁用的按钮，不是一个安全边界。

`demo.bat` 因此把它固定为 `true`（`app/config.py::_load_env_file` 只在环境里没有该键时才写入，脚本里的值优先）。
改完后实测 alice 拿到 `403 Manager or admin role is required.`，卡片里的每一句都成立。
**这只修了演示路径**：直接 `uvicorn app.main:app` 起服务仍会回到不校验角色的状态，属既有部署默认值问题，未在本次改动。

---

## 10. Demo 场景覆盖（原则十二 A / B / C）

场景清单由后端从 `sample_data/eval/it_incident_eval.jsonl` **投影**而来（原则 D1），
所以页面上的「期望」与评测 harness 的「期望」是同一个值，不会各自漂移。
本机实际分布：`approval_approved 16 · approval_rejected 3 · auto_execute 1 · risk_deny 1`（共 21，其中 20 条可现场运行）。

| 形态 | 实测证据 |
|---|---|
| **A 只读 / 低风险 → 自动执行** | `it-vpn-gateway-down`：`decision=auto_execute`、`rule_id=read_only_action`、`approval=null`、`execution.executed=true`、工单 `resolved`，且变更类工具的 `mcp.tool_call` 计数恰好 +1 |
| **B production + critical + 有副作用 → require_approval** | `it-redis-prod-down`：`rule_id=action_class_requires_approval:service_restart`；审批前变更类工具调用数**恰好不变**；admin 批准后 `resolved` 且恰好 +1 |
| **C 待审批 → 人工拒绝 → 完整 HITL** | 本机 `/showcase` 实跑「内网 DNS 解析失败，服务无法访问」：`waiting_approval` → 点击「拒绝」→ 工单 `rejected`、`execution=not_executed（审批被拒绝：approval_denied）`、审计链末尾是 `it.action_not_executed` 且**全程没有 `it.action_executed`** |
| **RBAC** | alice（employee）登录后：审批按钮 `disabled=true`，页面文案为「该角色不可审批：审批按钮会禁用，服务端也会独立拒绝（403）—— 禁用界面不是安全边界，服务端的拒绝才是。」；调用链里如实记录 `GET /api/approvals?status=pending → HTTP 403` |
| **幂等** | 同一账号对同一句话再次提交：`POST .../resolve` 返回 **409**，页面不把它当成功，而是显示「这张工单是复用来的…换一个场景，或换一个演示账号再运行」，并仍然加载那张工单的真实链路 |

`risk_deny` 那条（`it-risk-deny-injected`）**被刻意标为不可运行**：
它由评测 harness 注入 `action_type=data_delete` 驱动，而产品 API 没有这个参数。
页面照原样给出这条场景的期望与历史结论，并写明「无法现场复现」，**不为它开一个注入后门**（原则 D4）。

---

## 11. 测试结果（原则十一的 13 项）

| # | 要求 | 结果 |
|---|---|---|
| 1 | 前端 build | ✅ `npm run build` 成功（545ms）；产物 `index-MvZqG46J.js` / `index-V5JMN3Zp.css` |
| 2 | `git diff --check` | ✅ 未暂存与已暂存**都是 exit 0**，无空白错误 |
| 3 | 现有 42 个 smoke test | **38 通过 / 4 失败**，4 个失败**全部是环境依赖，且已在未改动的 HEAD 工作树上复现**（见下） |
| 4 | 新增 Demo 测试 | ✅ `scripts/it_showcase_smoke_test.py` 17 项全通过 |
| 5 | 启动 demo.bat | ✅ `start` → `[demo] Ready. Showing the showcase page.` |
| 6 | `/api/health` | ✅ 200，`tool_mode=mock`、`rag=not_configured`、`auth_required=true` |
| 7 | `/showcase` | ✅ 200 且返回真实 HTML；浏览器实测无 JS 报错 |
| 8 | 实际运行 ≥3 个不同类型 IT 场景 | ✅ A / B / C 各至少一条（见 §10） |
| 9 | auto_execute / require_approval / 人工拒绝 | ✅ 三种都在真实 UI 上跑通（见 §10） |
| 10 | 验证 chain | ✅ 9 步全部由 `GET .../chain` 的真实响应渲染 |
| 11 | 验证 audit | ✅ 审计链显示真实事件序列，拒绝路径以 `it.action_not_executed` 收尾 |
| 12 | 验证 stop | ✅ `stop` → `[demo] Stopped.`；pid 4196 / 9244 两次都确认**进程已消失**、pid 文件被删、`status` 报 not running、`/api/health` 不可达 |
| 13 | `git status` | ✅ 见 §13；无孤儿产物，`data/` 未被跟踪 |

### 4 个失败的 smoke test（**非 Phase 6 回归**）

用一个**未包含任何 Phase 6 改动的 HEAD 工作树**（`git worktree add --detach /tmp/p6base HEAD` → `e67dc21`，
`app/services/it/demo_scenarios.py`、`demo.bat`、`scripts/it_showcase_smoke_test.py` 在那里都不存在）
对照复现，四个**全部同样失败**：

| 脚本 | 本机现象 | 在 pristine HEAD 上 |
|---|---|---|
| `docker_smoke_test.py` | 401 Unauthorized | `RuntimeError: Service did not become healthy: HTTP Error 502` |
| `docker_oidc_smoke_test.py` | `OIDC authentication is not enabled`（等待 77s 后） | `Agent readiness timed out … 502 Bad Gateway` |
| `docker_scim_smoke_test.py` | `SCIM is disabled` | `AGENT_SCIM_TOKEN is required. Run scripts\bootstrap_docker_env.py first` |
| `ticket_http_outbox_smoke_test.py` | `cleanup()` 里 `unlink` → `PermissionError WinError 32` | **逐字相同**的 `WinError 32` |

三个 docker 脚本需要真实容器（本机没有；它们本来就不在 CI 里，见已知限制第 13 条）。
第四个**全部断言都通过**，失败发生在收尾删库时，是 Windows 上未关闭的 SQLite 连接持有文件 —— 既有问题，未在本阶段修。

### 新测试自己抓到的一个 bug

`it_showcase_smoke_test.py` 第一次全量跑时**失败**了，报 `Ticket status 'resolved' cannot be resolved`。
原因是我新加的 `_the_risk_gate_block_is_backed_by_the_payload` 与前面的审批流程用了同一个演示账号 + 同一个场景目标，
于是平台的**真实幂等键**让它们拿到了同一张工单。修法不是绕开幂等（也不是重置数据库 —— Windows 不放文件），
而是给新流程它自己的账号 `manager`。这条 bug 恰好证明了幂等在真实链路上是生效的。

---

## 12. 已知问题

全部已写进 `docs/KNOWN_LIMITATIONS.md` 第一部分第 15–20 条，此处只列标题：

1. **`npm run build` 每次都会把 `app/static/react/index.html` 重新写成 CRLF**（本次实测 433 → 445 字节、12 个 CR），
   `git diff --check` 因此报错 —— **每次构建后都要处理一次**，根因未修。
2. Showcase 的「知识检索」面板是**独立的实时调用**，与执行链里那份证据不是同一次（因为 `evidence[]` 没有 `category`）。
3. 评测数字是**代码里的常量**，不读 `data/eval_reports/`；重跑评测后需手工同步两个地方。
4. `demo.bat` 固定 `AGENT_AUTH_REQUIRED=true`；**直接用 uvicorn 起服务仍是不校验角色的默认配置**。
5. 本机 4 个 smoke 脚本失败（3 个需要 Docker、1 个 Windows SQLite 文件锁），与 Phase 6 无关，已用 pristine HEAD 复现。
6. `git config user.name` / `user.email` **在本仓库与全局都没有值**。提交前需要先显式设置。

---

## 13. 三处需要你知晓的偏差

### 偏差 1（最重要）：D1 的数据源用了完整评测集，不是 smoke 子集

**批准的方案**：以 `sample_data/eval/it_incident_eval_smoke.jsonl`（9 条）作为 Demo 场景来源。
**实际做法**：以 `it_incident_eval.jsonl`（21 条）为来源，smoke 文件**只贡献它那 9 个 id**，页面据此给场景打一个「CI 冒烟子集」徽章。

**为什么必须这样改**：`it_incident_eval_smoke.jsonl` 里**没有任何一条 `auto_execute` 场景**。
它的 9 条期望是 8 条 `require_approval` + 1 条 `deny`（那条 deny 还是靠注入 `data_delete` 驱动的，产品 API 无法复现）。
也就是说，只用 smoke 子集，**原则十二 A「只读/低风险→可自动执行」永远无法被演示** ——
而那恰恰是最能说明「不是所有事都进人工审批」的一条。

**代价**：Demo 与「CI 冒烟子集」不再是同一个集合。页面对此有明确标注（21 条中 9 条属于 CI 子集），
`_the_list_is_projected_from_the_committed_suite` 与 `_the_smoke_subset_is_read_from_the_smoke_file`
两条测试分别守住「场景确实来自提交的评测集」和「徽章确实来自 smoke 文件」。
**如果这不是你想要的，请告诉我**，改回只用 smoke 子集是一处小改动，代价是失去 A 类演示。

### 偏差 2：`index.html` 的行尾归一化用了「删 CR」而不是你给的那条命令

你给的命令是：

```python
p.write_bytes(p.read_bytes().replace(b'\r\n', b'\n').replace(b'\r', b'\n'))
```

构建产物里的行尾是 `\r\r\n`（一个 CR 后面再跟 CRLF）。这条命令的第二步会把那个**多余的 CR 换成 LF**，
结果是 `\n\n` —— **凭空插入一个空行**。HEAD 里已提交的版本是 433 字节的纯 LF、且没有那个空行，
可以反证 Phase 5 当时用的也不是这个写法。

本次实际执行的是：

```python
p.write_bytes(p.read_bytes().replace(b'\r\n', b'\n').replace(b'\r', b''))
```

**只对 `app/static/react/index.html` 这一个文件**，其他文件的行尾一律没碰。结果是 433 字节、0 个 CR，与 HEAD 逐字节一致。

### 偏差 3：`resolution.mode` 报的是真实值，不是简报里写的 `deterministic_keyword`

简报原则六写「历史工单单独标 `source = historical_ticket_index`、`mode = deterministic_keyword`」。
实测该字段的真实取值是 `deterministic`（检索到正式知识时）与 `deterministic_no_knowledge`（没有知识时），
**不存在 `deterministic_keyword` 这个值**。页面改为直接打印真实字段值，`source` 则与简报一致。

另有两处按事实澄清，不是偏差：`source=local_policy_db` 是正式知识证据的真实 `source`；
`llm_confidence_used=false` 是真实值（Risk Gate 的任何分支都不读模型置信度）。

---

## 14. 建议的 commit message（**尚未执行**）

```
feat(phase6): capability-categorised showcase with a one-command demo

Adds /showcase: 21 IT scenarios projected from the committed evaluation
suite, grouped into the seven capability categories, each runnable
against the real HTTP API. The page keeps no success state of its own --
every row of the call trace is written only after the call it describes
has actually been made.

- app/services/it/demo_scenarios.py: read-only projection of the eval
  suite plus the capability and highlight indexes
- GET /showcase and GET /api/it/demo-scenarios
- frontend: ShowcasePage, ITChain and the shared ui helpers lifted out
  of App.jsx (App.jsx shrinks by 293 lines)
- Risk Gate step now lists the gate's own inputs, read from the response
- demo.bat: start/stop/status, pinned to AGENT_AUTH_REQUIRED=true and
  AGENT_TOOL_MODE=mock; stop kills only the pid it recorded
- scripts/it_showcase_smoke_test.py: 17 tests

No change to risk_gate, triage, execution, intake, history, rbac or the
evaluation suite.
```

---

**结论**：Phase 6 的交付物已全部就位并通过验证，**没有 commit、没有 push**。
等你确认后再决定是否提交 —— 提交前请先设置 `git config user.name` / `user.email`。
