# 已知限制与未来可扩展方向

> 本文分两部分：**当前限制**（现在就是这样，不隐藏）与**未来可扩展方向**（尚未实现，逐条标注）。
>
> 事实来源：`docs/PHASE4_REPORT.md`、`docs/PHASE3_REPORT.md`、`scripts/it_regression_smoke_test.py`、
> `sample_data/eval/it_incident_eval.jsonl`、`app/services/it/mock_data.py`、
> `app/services/it/triage.py`、`app/services/multi_agent/durable_executor.py`、
> `app/services/it/risk_gate.py`、`scripts/evaluate.py`、`.github/workflows/ci.yml`、
> `docker-compose.prod.yml`、`scripts/bootstrap_docker_env.py`、`.gitignore`。
>
> 本文不声称任何生产部署、真实客户、真实工单量、真实 QPS 或 SLA。

---

## 第一部分：当前限制

### 1. `it_research_query` 被构建、被审计，但没有任何检索节点消费它

`durable_executor.py` 把 triage 阶段算出的 `research_query` 写入状态键 `it_research_query`
（并纳入 checkpoint 白名单），同时留下一条 `it.research_query_built` 审计行；
但 6 处检索节点读取的都是 `active_objective` / `objective`，不是这个值 ——
`it_history` 节点自己用 `_it_research_query(objective, entities)` **重新算一遍**，而不是读已存下来的那份。

后果：`it.research_query_built` 审计行**声称了一个并非实际使用的检索词**。
这是「审计说了谎」类问题里最轻的一种（它记录的是真的被计算过的值，只是没被用），
但既然如实记录就必须写明。

**Phase 4 刻意不修**，按用户决定继续作为 Known Limitation 保留（`docs/PHASE4_REPORT.md` §13-1）。

### 2. 唯一剩余的评测失败：`it-paid-software-request` / `retrieval_ok`

- 期望检索到：《员工设备与软件申请指引》
- 实际返回：客户退款处理政策 / 采购审批规则 / 生产故障响应 SOP
- **这是 Phase 3 baseline 中就存在的独立问题**（baseline 里同一个 case 失败 4 项，其中就有 `retrieval_ok`），
  与 P0/P1/P2 三个缺陷无关
- 期望值**被刻意保留、未放宽**：`it_evaluation_smoke_test.py` 改为**精确断言这条残余**
  （失败项集合必须恰好是 `["retrieval_ok"]`），而不是删掉或调松它

因此 Phase 4 的读数是 `passed 20 / 21`、`pass_rate 0.9524`、`retrieval_recall_at_3 0.9524`
（与 Phase 3 相同，未提升）。

### 3. IT 数据是 mock / 种子数据

`app/services/it/mock_data.py` 里的部门、员工、资产全部是**虚构行**，
模块 docstring 明确写着「Every row is entirely fictional and stays local: no real company, employee,
asset or external SaaS is contacted」。资产表里的 `REDIS-001`、`SERVER-001`、历史工单
`IT-2025-1041` 等都是种子数据。

**没有接入任何真实企业系统**，因此本项目的任何数字都不能当作真实环境下的实测值。

### 4. 工具执行是本地受控的（默认 mock 工具模式），没有真实 ITSM 集成

- `app/config.py` 的 `tool_mode` 默认取自 `AGENT_TOOL_MODE`，**缺省为 `mock`**。
- 工单与邮件 provider 同样有 mock 实现（回归脚本显式设 `AGENT_TICKET_PROVIDER=mock` / `AGENT_EMAIL_PROVIDER=mock`）。
- **没有真实的 ServiceNow / Jira / ERP 连接器**；IT 动作工具（`flush_cache` / `restart_service` /
  `grant_permission` / `diagnose_service`）都是本地实现。

所以「工具被执行了」在当前代码里意味着「mock 工具被调用了」，而不是「企业系统真的被改了」。

### 5. 过度升级不修：文本说生产、资产是 staging 时仍走人工审批

P0 的合并方向选择的是**取更严重者**（most severe wins），不是严格优先级。
副作用是：一句话写着「生产」而目标资产其实是 staging 时，运行**仍然要求人工审批**。

这是**刻意的选择**（用户确认「只收紧，不动过度升级」）。它不经济，但安全；
作为同一根因的反方向对照保留，也说明「取最大值」这一条不能被局部改成「资产优先」。

### 6. P2 规则里 `access_level_hit` 的已知边界

`software_acquisition = software_hit and (paid_hit or not resource_hit) and not access_level_hit`
—— 当采购句里**同时**出现明确的访问级别词时，`access_level_hit` 为真，
这句仍会被判为权限申请。例如：

> 「申请采购 Navicat 数据库读写权限」

会走 `PERMISSION_REQUEST` 而不是采购。这是**刻意的取舍**（有明确访问级别时，保守地按权限申请处理），
且 **eval 套件未覆盖这一句**，因此它既没有被验证，也没有被否定。

### 7. `_match_table` 仍是 first-hit-wins

`app/services/it/triage.py::_match_table` 的函数体仍是有序表的**存在性**匹配
（命中第一个 code 就返回）。P1 的修复只把 `SERVICE_KEYWORDS` 换成 `_match_service`，
`_match_table` 保留下来，现在只服务 `RESOURCE_KEYWORDS` / `SOFTWARE_KEYWORDS` 两张表。

这两张表**无已知缺陷** —— 它们的语义本就是「句中提到的最具体的那一个」。
`_match_environment` 同为顺序匹配。

### 8. 请求未指明环境时，resolution 与 risk gate 的读数不一致

当工单文本里没有任何环境词时：

- resolution 报 `missing_information: ["environment"]`；
- risk gate 已经从资产行读到了环境 → **R6 `request_more_information` 触发并标记 `runnable=False`**
  （`executable is False`）。

也就是说，**门现在比喂给它的 resolution 知道得更多**，两级读数不一致。

**两种读数都是安全的**（一个更保守地要求补充信息，一个更准确地知道环境；
无论哪种，工单都会走到人面前，且仅凭审批无法解锁一个 `executable=false` 的动作），
因此如实记录而不去「调平」它。`scripts/it_regression_smoke_test.py` case 3 用注释写明了这一点并断言
`decision["executable"] is False`、`reasons == ["production_side_effect", "critical_asset_side_effect", "request_more_information"]`。

### 9. `eval_reports` 表的列语义被复用

IT 套件没有自己的表：`eval_reports` 只有六个指标列，因此 IT 的专项数字放在 `report_json` 里，
两个列被复用为最接近的含义 —— **`tool_accuracy` 存的是 resolution action accuracy**
（`scripts/evaluate.py::save_it_report` 的 docstring 明确标注了这一点）。

**表结构未改**（Phase 4 的约束之一是「不新增大量数据库表」）。
后果：直接查这张表的人必须知道，对 IT 套件而言 `tool_accuracy` 不是「工具准确率」。

### 10. 部署风险（Phase 5 审计发现）：`.env.hr-demo` 缺失时 compose 回落到弱默认口令

`docker-compose.prod.yml` 对敏感变量使用 `${VAR:-默认值}` 形式，
仓库里目前**只有 `.env.hr-demo.example`，没有 `.env.hr-demo`**（后者被 `.gitignore` 排除）。
于是省略 `--env-file` 直接启动时，会静默使用示例级默认值，例如：

- `AGENT_POSTGRES_PASSWORD` / `AGENT_POSTGRES_MIGRATION_PASSWORD` / `..._WORKER_...` / `..._READONLY_...` → `agent_password`
- `RAG_POSTGRES_PASSWORD` → `rag_password`
- `AGENT_AUTH_TOKEN_SECRET` → `replace-this-hr-demo-secret`
- `AGENT_SCIM_TOKEN` → `replace-this-scim-token`
- `RAG_SERVICE_TOKEN` → `replace-this-rag-service-token`
- `TICKET_SERVICE_TOKEN` → `replace-this-ticket-token`
- `RAG_AUTH_TOKEN_SECRET` → `replace-this-rag-demo-secret`
- `AGENT_OIDC_CLIENT_SECRET` → `local-oidc-demo-client-secret`

**没有任何机制拒绝启动**（compose 层没有校验，应用层 `preflight.py` 检查的是运行配置而非 compose 变量）。

**本次只记录，未修改**（不改变部署脚本的行为）。
推荐的正确做法：**先运行 `scripts/bootstrap_docker_env.py`** 生成/补齐 `.env`，
并按 `README.md` / `docs/DOCKER_DEPLOYMENT.md` 的方式显式传 `--env-file` 启动；
该脚本会把 `replace-this-*`、空值与长度不足 32 的值一律判定为弱口令并重新生成。

### 11. 构建产物同步：`app/static/react/` 的 `index.html` 必须与新的 hash 资源一起提交

`app/static/react/` 是 Vite 的 `outDir`（见 `vite.config.js`），其**构建产物按约定纳入版本库** ——
容器和一份全新 checkout 因此无需运行 Node 就能提供 UI。

代价是：`npm run build` 之后必须把重新生成的 **`index.html` 与 `assets/` 下带新 hash 的文件一起提交**。
只提交 `index.html` 会让页面引用仓库里不存在的文件（`.gitignore` 里的注释写明了这一点）。
这是一个人为约定，不是构建系统保证的 —— 忘记同步会得到一个白屏页面。

### 12. 评测是「21 条精选案例 + mock 数据」，不是生产基准

- IT 套件是 `sample_data/eval/it_incident_eval.jsonl`，**21 条人工编写的 case**；
  smoke 子集 `it_incident_eval_smoke.jsonl` 是 9 条。
- 数据全部来自 mock / 种子语料（见第 3 条），指标从 `it.*` 审计行与 `mcp.tool_call` 行**重算**得出，
  因此可复现，但**样本量与场景覆盖度都有限**。
- **LLM 默认关闭**：`AGENT_LLM_ENABLED` 缺省为 `False`（`app/config.py`），
  默认路径是**确定性**的（`resolution.mode == "deterministic"`、`risk_gate.MODE == "deterministic"`）。

所以 `triage_accuracy 1.0` 这类数字的准确含义是「在这 21 条上成立」，不是「在生产上成立」。

### 13. 没有统一的测试运行器

- 仓库里有 50+ 个 `scripts/*.py`，其中大部分是 smoke test；**没有一个统一的 runner**。
- **CI（`.github/workflows/ci.yml`）是唯一的聚合清单**：它以显式步骤逐个运行 smoke 脚本，
  并额外跑 `python -m compileall app scripts external_ticket_service`、迁移与 preflight、`npm run build`、
  `docker compose config --quiet`。
- **有 7 个 smoke 脚本不在 CI 里**，原因是它们需要 Docker 或外部服务/GPU：
  `docker_smoke_test.py`、`docker_oidc_smoke_test.py`、`docker_scim_smoke_test.py`、
  `postgres_smoke_test.py`、`llm_smoke_test.py`、`scim_smoke_test.py`、`async_job_smoke_test.py`
  （`llm_smoke_test.py` 需要一个可用的 LLM provider；`docker-compose.vllm.yml` 里的 vLLM 服务带 `gpus: all`）。

后果：这些脚本只能在**本地手工运行**，CI 变绿不代表它们也变绿。

### 14. 评测套件不构成 CI 门禁

`.github/workflows/ci.yml` 里的 `python scripts/evaluate.py --suite it` 步骤**会执行但不会失败退出** ——
CI 注释写明「It is an evaluation, not a gate, so it exits 0 and the numbers are read」。
因此评测指标下跌不会阻止合并；数字要有人去看。

---

## 第二部分：未来可扩展方向

以下是**尚未实现**的方向，全部标注为「未实现 / 未验证」。
列出它们是为了说明「已知的下一步是什么」，不构成任何已完成或已计划的承诺。

| # | 方向 | 状态 | 说明 |
|---|---|---|---|
| 1 | 让检索节点真正消费 `it_research_query` | **未实现 / 未验证** | 当前 6 处检索节点读 `active_objective` / `objective`，`it_history` 自行重算 query（见第一部分第 1 条）。要做的是把已存下来的那份接进检索路径，并让 `it.research_query_built` 审计行与实际使用值重新对齐 |
| 2 | 改善采购类请求的检索命中 | **未实现 / 未验证** | 直接对应第一部分第 2 条那个唯一剩余的失败（`it-paid-software-request` / `retrieval_ok`）。方向是语料或分词口径，而不是放宽期望 |
| 3 | 真实 ITSM 连接器（ServiceNow / Jira / ERP） | **未实现 / 未验证** | 当前工具是本地 mock 实现（第一部分第 4 条），完全没有对接真实系统；连接器需要同时解决凭据、重试、幂等与审计的形状 |
| 4 | 由真实 LLM 驱动的评测 | **未实现 / 未验证** | 当前 `AGENT_LLM_ENABLED` 缺省为 `False`，默认路径是确定性代码（第一部分第 12 条）。要测「模型参与时是否仍然安全」，需要一套与确定性路径对照的运行方式 |
| 5 | 统一本地测试运行器 | **未实现 / 未验证** | 目前 CI 的 YAML 是唯一聚合清单（第一部分第 13 条）；一个本地 runner 需要能让「需要 Docker / GPU 的脚本」显式跳过而不是静默漏跑 |
| 6 | 把评测接入 CI 作为门禁 | **未实现 / 未验证** | 现状是「执行但不失败退出」（第一部分第 14 条）。变成门禁前需要先确定阈值与允许的失败集合（目前 `it-paid-software-request` / `retrieval_ok` 是已知的、刻意不放宽的失败） |
| 7 | 修正部署时的弱默认口令 | **未实现 / 未验证** | 对应第一部分第 10 条。至少可以是「compose 层拒绝启动」或「preflight 检查 compose 变量」，目前两者都没有做；短期补救仍是先跑 `scripts/bootstrap_docker_env.py` |

上表每一项都**没有代码**，也没有测试覆盖，**不应被读成已完成的工作**。
