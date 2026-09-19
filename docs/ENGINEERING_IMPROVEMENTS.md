# 工程质量改进记录 — 三个缺陷的完整闭环

> 范围：只记录 Phase 3 Evaluation 报出、并在 Phase 4 修复的三个缺陷（P0 / P1 / P2），
> 以及修复过程中建立或保留下来的安全不变量、审计字段和一处被改写的既有测试。
>
> 事实来源：`docs/PHASE4_REPORT.md`、`docs/PHASE3_REPORT.md`、`scripts/it_regression_smoke_test.py`、
> `app/services/it/risk_gate.py`、`app/services/multi_agent/durable_executor.py`、`app/services/it/triage.py`。
> 本文不含估算数字，也不声称任何生产部署、真实客户或真实企业系统接入。

---

## 0. 六段式叙事

每一个缺陷都按同一条叙事写：**Problem → Detection → Root Cause → Fix → Regression Test → Evaluation Result**。
这是这个项目最值得复述的部分：不是「加了一个功能」，而是
**用度量发现问题 → 定位真实代码路径 → 做最小修复 → 写回归测试锁死 → 用同一套度量证明它好了**。

| 段落 | 回答的问题 | 证据形态 |
|---|---|---|
| Problem | 用户看到什么错 | 一条工单文本 + 一个资产行 + 一次错误的执行结果 |
| Detection | 谁发现它 | Evaluation Harness 的 case id + `failed_checks` + 汇总指标 |
| Root Cause | 代码里哪一条路径导致它 | 具体函数、具体表达式、具体数据来源链 |
| Fix | 改了什么 | 新增/修改的函数名与判定式；**不改**的部分同样写明 |
| Regression Test | 靠什么锁死 | `scripts/it_regression_smoke_test.py` 的 case 序号与断言要点；撤销修复即变红 |
| Evaluation Result | 度量上好了多少 | Phase 3 baseline 与 Phase 4 的同一组指标前后对比 |

三个缺陷的严重度与一句话结论（与 `docs/PHASE3_REPORT.md` §9/§10 的原始记录一致）：

| # | 严重度 | 一句话 |
|---|---|---|
| P0 | 高 | Risk Gate 的 `environment` / `criticality` 全部来自用户文本与 triage priority，从不读取目标资产行 |
| P1 | 中 | `SERVICE_KEYWORDS` 走 first-hit-wins，声明顺序即优先级，「数据库连不上，怀疑是 Redis 缓存雪崩」被判为 REDIS |
| P2 | 中 | `PERMISSION_KEYWORDS` 含「授权」，而采购语境里的「授权」是许可证，采购申请被判为权限申请 |

---

## 1. P0 — 资产元数据从未到达 Risk Gate

### Problem

一张写着「预发环境的 Redis 缓存需要清理」的工单，绑定的资产是 `REDIS-001`，
资产表里它写着 `environment = production`、`criticality = critical`。
平台在**没有任何人工审批**的情况下对它执行了 `flush_cache`，`mutating_tool_calls: 1`，工单被置为 `resolved`。

即：**报告人只要不写「生产」二字，R3（`production_side_effect`）与 R3b（`critical_asset_side_effect`）
就都不会触发**，一次针对关键生产资产的可逆写操作自动执行了。

### Detection

Phase 3 建立的 Evaluation Harness 把这条写成 case `it-prod-cache-unlabelled-env`
（`docs/PHASE3_REPORT.md` §9 ②、§10 第一个小节）：

- `failed_checks: ["risk_ok", "approval_ok"]`
- 期望 `require_approval`，实际 `auto_execute`，`rule_id = non_production_reversible_action`，无审批
- 汇总指标 `unexpected_auto_execution_count: 1`（Phase 3 baseline：`passed 18 / 21`，`pass_rate 0.8571`）

**缺陷是被度量抓出来的，不是被读代码猜出来的。**

### Root Cause

`app/services/multi_agent/durable_executor.py::_risk_gate_node` 是 `evaluate_it_risk` 的**唯一**调用点。
它传给门的两个关键值是：

```python
environment = resolution.get("environment")          # ← 全部来自用户原话
criticality = "critical" if triage.get("priority") == "urgent" else None
```

`resolution["environment"]` 的来源链是唯一的：
`submit_it_request` 写 `tickets.environment` ← `triage.entities["environment"]` ←
`ENVIRONMENT_KEYWORDS` 对**用户原话**的匹配结果。

**资产行从未被读取** —— 节点里没有 `get_asset` 调用，`state["it_triage"]["asset_id"]` 虽然就在作用域内却没人用。

`risk_gate.py` 本身没有 asset 参数（`evaluate()` 8 个 kwarg、`inputs` 恰好 7 个键），
这是 Phase 2 的刻意设计，不是 bug；bug 在于**调用方从未把资产事实喂进去**。
于是「用户没说生产 = 非生产」成了自动执行的捷径。

### Fix

**(a) 新增 `_it_asset_risk_metadata(state, resolution)`**（`durable_executor.py:848`），返回
`(environment, criticality, provenance)`：

- asset_id 的取值优先级：`resolution["action_arguments"]["asset_id"]`（**将被变更的目标**）→
  `state["it_triage"]["asset_id"]`（工单绑定的资产）→ `resolution["target"]`；
  四者都没有时 `provenance["asset_lookup"] = "no_asset_id"` 并直接 fail-closed。
- 用 `state` 里的 requester 字段组装 `AuthContext`（`app/services/auth.py`，frozen dataclass，
  `display_name` 无默认值 → 用 user_id 兜底）。之所以要走角色：`get_asset` 的 spec 是
  `require_auth_context: True` / `required_role: "employee"`，而 `authorize_tool_call` 是**等级制**
  （employee=10 为最低级），employee / manager / it_support / it_admin / admin 都能过；
  未识别角色 → 拒绝 → 走 fail-closed。
- **经 `call_tool("get_asset", ...)` 调用**，而不是直接 import —— Agent 仍然拿不到 Python 函数，
  仍然过 Tool Registry 的角色门，仍然留 `mcp.tool_call` 审计行。
  `get_asset` 对 production 资产只脱敏 `serial` / `owner_user_id` / `metadata`，
  `environment` / `criticality` **永远可读**。
- **fail-closed**：`found=False` / `forbidden` / 异常 / 根本没有 asset_id
  → 返回 `(UNKNOWN_ENVIRONMENT, UNKNOWN_CRITICALITY)`，即 **production + critical**。
  「查不到」不能是绕过门的便宜路径。

**(b) `_risk_gate_node`（`durable_executor.py:934`）只换「值」，不换「形状」：**

```python
asset_environment, asset_criticality, provenance = _it_asset_risk_metadata(state, resolution)
text_environment = resolution.get("environment")
text_criticality = "critical" if triage.get("priority") == "urgent" else None
decision = evaluate_it_risk(
    ...
    environment=merge_environment(asset_environment, text_environment),
    criticality=merge_criticality(asset_criticality, text_criticality),
    ...
).to_dict()
```

传给 `evaluate_it_risk` 的 **kwargs 与 `inputs` 的 7 个键一个没动**，
因此既有的键集合断言与精确值断言都保持绿色。

**(c) 合并方向 = 取更严重者，而不是严格优先级。**
`merge_environment` / `merge_criticality`（`risk_gate.py:331` / `:344`，底层 `_most_severe` `:349`）
读 `ENVIRONMENT_SEVERITY = ("dev", "staging", "production")` 与
`CRITICALITY_SEVERITY = ("normal", "important", "critical")`（`risk_gate.py:71-72`），取阶梯上最靠后的一档。
严格优先级会让「文本写生产、资产是 staging」从 `require_approval` **降级**为 `auto_execute` —— 那是一次**放松**。
取最严重者同时满足两件事：漏批被堵上，Phase 2 的过度升级保持原样。
`_most_severe` 对「传了值但都不在阶梯上」的输入返回 `ladder[-1]`（最严重档），而不是 `None` —— 未知不等于缺失。

**(d) `evaluate()` 一行未动。** R0–R7 级联、`llm_confidence_used: False` 全部原样。

### Regression Test

`scripts/it_regression_smoke_test.py`：

- **case 1 `_production_asset_escalates_a_reversible_action`** ——
  走真实 intake 建单（文本写「预发」，断言 `triage.entities["environment"] == "staging"`、
  `related_asset.id == "REDIS-001"`、`related_asset.environment == "production"`），
  再调 resolve 路由，断言：
  `decision == "require_approval"`、`rule_id == "production_side_effect"`、
  `reasons == ["production_side_effect", "critical_asset_side_effect"]`、
  `inputs["environment"] == "production"`、`inputs["criticality"] == "critical"`，
  **审批前 `_mcp_calls(*IT_ACTION_TOOLS) == 0`**；批准后 `flush_cache` 恰好 1 次、工单 `resolved`。
  同时**逐字断言**审计字段（见第 5 节）。
- **case 3 `_critical_asset_is_never_automatic`** ——
  文本用「内网的 Redis 缓存压力很大」（**完全不含环境词**），
  断言 `resolution["environment"] is None`、`decision == "require_approval"`、
  `environment == "production"`、`inputs["criticality"] == "critical"`；
  另外两条**纯函数**断言：`evaluate_risk(action_type="cache_flush", environment="staging", criticality="critical")`
  → `require_approval` / `critical_asset_side_effect`（单独证明 R3b 生效而不被 R3 掩盖），
  以及「合并只会收紧」：`(environment="dev", criticality="normal")` 仍然是 `auto_execute`。

**撤销 P0 修复，case 1 与 case 3 立刻变红。**
同一修复也被 `scripts/it_resolution_smoke_test.py::_failed_action_fails_the_run_without_a_retry` 的
新增断言覆盖（见第 6 节）。

### Evaluation Result

| 指标 | Phase 3 baseline | Phase 4 |
|---|---|---|
| `unexpected_auto_execution_count` | **1** | **0** |
| `risk_decision_accuracy` | 0.9524 | **1.0** |
| `approval_accuracy` | 0.9524 | **1.0** |
| `passed / total` | 18 / 21 | **20 / 21** |
| `pass_rate` | 0.8571 | **0.9524** |

以第二个独立前缀 `phase4_conf` 重跑，16 个指标逐项相同、失败 case 相同。

---

## 2. P1 — 服务分类由关键词书写顺序决定

### Problem

「生产环境数据库连不上，怀疑是 Redis 缓存雪崩」被判为 `REDIS` 而不是 `DATABASE`。

报告人说的是**数据库连不上**，「Redis 缓存雪崩」只是**他猜的原因**。
分类结果依赖关键词表的**书写顺序**，而不是报告人说的什么是坏的。

### Detection

case `it-misclassification-service-order`，`failed_checks: ["category_ok"]`，
期望 `DATABASE`、实际 `REDIS`；动作、风险、审批、执行四项全部正确，**只有分类错了**。
汇总指标 `triage_accuracy: 0.9048`。

### Root Cause

`app/services/it/triage.py::_match_table` 的函数体是：

```python
for code, keywords in table:
    if any(keyword in lowered for keyword in keywords):
        return code
```

`any(...)` 只做**存在性**判断，声明顺序即优先级，而 `REDIS` 恰好排在 `DATABASE` 之前。

**而且单纯改成计数也修不好**：这句话里 `redis` + `缓存` 命中 **2** 次，
`数据库` 只命中 **1** 次 —— 计数会得出同一个错误答案。
真正的区分信息是**子句语义**：数据库在「观察」子句里，Redis 在「猜测」子句里。
（把 REDIS 挪到 DATABASE 后面只是把 bug 换个方向，不是修。）

### Fix

新增 `_match_service(lowered)`（`triage.py:475`），只用于 `SERVICE_KEYWORDS`：

1. 按 `CLAUSE_SPLITTERS`（`triage.py:192`，`，,。；;！!？?、`）经 `_clauses`（`:523`）切分子句；
2. 每个子句由 `_clause_weight`（`:531`）给出**权重**：
   猜测子句 `OCCURRENCE_HEDGED = 1`、普通子句 `OCCURRENCE_PLAIN = 2`、症状子句 `OCCURRENCE_SYMPTOM = 3`；
   `SYMPTOM_MARKERS = SEVERE_SYMPTOMS`（**复用已存在的词表**，不新造），
   `HEDGE_MARKERS`（`:195`）= 怀疑 / 疑似 / 可能是 / 大概 / 也许 / 有可能 / 猜测 / 估计 / 或许 / 是不是；
3. 对每个 code 的每个关键词统计**出现次数**，乘所在子句权重后累加；
4. 裁决顺序全确定性：**总分 → 症状子句命中数 → 最早出现位置 → `SERVICE_KEYWORDS` 声明顺序**。
   声明顺序退化为**最后的**平局裁决，这就是「不再由关键词顺序决定」的证明。

`classify` 只改了一行调用（`service = _match_service(lowered)`）；
`_match_table`（`:466`）**保留**，继续服务 `RESOURCE_KEYWORDS` / `SOFTWARE_KEYWORDS`。

**修复过程中还发现并修掉一个真实的优先级 bug**：`_clause_weight` 原先**先查症状词、再查对冲词**，
于是「怀疑是 Redis 挂了」这种**被对冲的症状词**拿到了完整症状权重（3 分），
在总分打平时由位置/表序决定答案 —— 那正是 P1 要消灭的 first-hit-wins 的另一种形态。
现在 `HEDGE_MARKERS` 的判断**排在 `SYMPTOM_MARKERS` 之前**，函数 docstring 写明了原因。

### Regression Test

`scripts/it_regression_smoke_test.py::_service_classification_ignores_word_order`（case 5）：

- `classify("生产环境数据库连不上，怀疑是 Redis 缓存雪崩").category == "DATABASE"`，且
  `intent == "IT_INCIDENT"`、`entities["service"] == "DATABASE"`；
- **把「Redis 缓存雪崩」挪到句首重跑**（`"怀疑是 Redis 缓存雪崩，生产环境数据库连不上"`）
  **仍为 `DATABASE`** —— 这一条才是「不是靠挪关键词修的」的证据：按位置修复的实现在这里会翻转；
- 换一组词再验同一性质：`"数据库连不上，可能是 Redis 挂了"` 与 `"可能是 Redis 挂了，数据库连不上"`
  都必须落在 `DATABASE` —— 修复不是对某一句话调参；
- 反向保护（确认没有把表压平）：`"预发环境的服务器缓存压力很大，需要清理缓存"`、
  `"我的生产 Redis 连不上了"`、`"生产 Redis 缓存压力很大"`、`"生产环境 Redis 异常"` 仍为 `REDIS`；
- 其余服务未被波及：`内网 DNS 解析失败 → NETWORK`、`VPN 连不上 → VPN`、`生产服务器挂了 → SERVER`、
  `生产数据库锁表 → DATABASE`、`邮箱登录不了 → EMAIL`；
- 都不沾的句子不被硬推给打分最高的那个：`classify("今天食堂几点开门？").category == "GENERAL"`
  且 `entities` 里没有 `service` 键。

### Evaluation Result

`triage_accuracy` 0.9048 → **1.0**。
21 个 eval case 与既有 triage 夹具的判定结果逐条比对过，**无回归**。

---

## 3. P2 — 一个词的两种含义：软件申请被判成权限申请

### Problem

「申请采购付费数据库客户端授权 Navicat」被判为 `PERMISSION_REQUEST`，
Agent 提议的动作是 `permission_grant` —— 它会去**授予数据库访问权**，而不是走采购流程。

一个分类错误直接导致**错误的动作类型**（`permission_grant` vs `no_action`），
也直接导致**找错团队**。Phase 3 记录了它当时没造成不安全后果
（动作类 `permission_change` 本就需审批，且缺 `environment` 阻断了执行）。

### Detection

case `it-paid-software-request`，`failed_checks: ["category_ok", "intent_ok", "retrieval_ok", "action_ok"]`
—— 三项（另有检索一项）一起红，说明它们是**同一个根因**。

### Root Cause

「授权」在 `PERMISSION_KEYWORDS` 里（`triage.py:51`）。

`_resolve_intent` 本来就有为这个冲突写的规则：

```python
if permission_hit and software_hit and not resource_hit:
    return "SOFTWARE_REQUEST"
```

它的守卫条件 `not resource_hit` 恰好被 case 里的「数据库」破掉 ——
那个「数据库」只是**软件商品名的一部分**（数据库客户端 Navicat），并不是申请的对象。

守卫问错了问题：它问「句中有没有资源词」，而真正该问的是「**被申请的是不是一件商品**」。

### Fix

把守卫换成三要素（`_resolve_intent`，`triage.py:358`，新增 kw-only 参数 `paid_hit` / `access_level_hit`）：

```python
software_acquisition = software_hit and (paid_hit or not resource_hit) and not access_level_hit
if permission_hit and software_acquisition:
    return "SOFTWARE_REQUEST"
```

- **主要业务意图** = `paid_hit`（`PAID_KEYWORDS`，`triage.py:152`，本就含「商业授权」「license」
  —— 代码库自己就承认采购语境下的「授权」是许可证，不是访问授权）；
- **请求对象** = `software_hit`（被申请的是 `SOFTWARE_KEYWORDS` 里的一件商品，Navicat 命中 `DB_CLIENT`）；
- **动作** = `access_level_hit` 为假（句中没有明确的访问级别词）。

没有重新设计分类体系，只在既有 taxonomy 内改了一处判定。

### Regression Test

`scripts/it_regression_smoke_test.py` 的 **一对镜像用例**，
既证明采购修好了，也证明没有把**真正的权限申请**一起改判：

- **case 6 `_permission_word_does_not_win_a_purchase`**：
  `classify("申请安装 Docker，需要管理员权限")` → `intent == "SOFTWARE_REQUEST"`、
  `category == "DOCKER"`、`entities["software"] == "DOCKER"`；
  并走路由验证分类真的**决定了后续动作** —— `action_type == "no_action"`、
  `risk_decision.executable is False`、审批前 `_mcp_calls(*IT_ACTION_TOOLS)` 不变
  （「Agent 不会自己装软件」）。
  同一 case 还覆盖 §六 规则 3 的另一侧：`classify("申请采购付费数据库客户端授权 Navicat")`
  → `SOFTWARE_REQUEST` / `DB_CLIENT`；`classify("我要申请购买 Office 商业授权")` → `SOFTWARE_REQUEST`。
- **case 7 `_access_request_is_still_a_permission_request`**：
  `classify("申请生产数据库访问权限")` → `intent == "PERMISSION_REQUEST"`、
  `category == "DATABASE_PERMISSION"`、`entities["resource"] == "DATABASE"`；
  并走路由验证 `action_type == "permission_grant"`、
  `decision == "require_approval"`、审批前工具调用数为 0。

### Evaluation Result

`it-paid-software-request` 的 `category_ok` / `intent_ok` / `action_ok` 全部转绿；
`triage_accuracy` 与 `resolution_action_accuracy` 都到 **1.0**（后者 0.9524 → 1.0）。
**仅剩 `retrieval_ok` 未过** —— 那是 Phase 3 baseline 中就存在的独立问题（见 `docs/KNOWN_LIMITATIONS.md`）。

---

## 4. 安全不变量

以下是 Phase 4 **建立或保留**的不变量。每一条都写明它在代码里如何被强制，
而不是「提示词里写了」或「模型应该会遵守」。

| 不变量 | 强制方式 | 位置 |
|---|---|---|
| 资产查询失败 → **fail-closed**，按 production + critical 处理 | 取不到资产时返回常量 `UNKNOWN_ENVIRONMENT = "production"` / `UNKNOWN_CRITICALITY = "critical"`，而不是 `None` | `risk_gate.py:77-78`；`durable_executor.py:848-931` |
| 审计能区分**为什么**取不到 | `asset_lookup` ∈ `found` / `not_found` / `denied` / `error` / `no_asset_id`，另有 `asset_lookup_reason` | `durable_executor.py:887-926` |
| production + 变更类动作 → **至少 REQUIRE_APPROVAL** | R3：`if cls.side_effect and normalized_environment == "production": escalate("production_side_effect")` | `risk_gate.py:294-295` |
| critical 资产 + 变更类动作 → **至少 REQUIRE_APPROVAL** | R3b：`if cls.side_effect and normalized_criticality == "critical": escalate("critical_asset_side_effect")` | `risk_gate.py:297-298` |
| DENY → **0 次工具调用** | R1：`if not cls.allowed` → `decide(DECISION_DENY, "denied_action_class:<risk_class>", ..., executable=False)`；且被拒类目**没有注册任何工具**（`tool_name=None`） | `risk_gate.py:269-272`、`ACTION_CLASSES` 中 `data_delete` / `permission_revoke` / `account_disable` |
| REJECT → **0 次工具调用** | 审批被拒后 `decision` 不再是执行许可，`execution` 记录 `reason == "approval_denied"` 且不落任何 `mcp.tool_call` | 回归 case 2 断言 `_mcp_calls(*IT_ACTION_TOOLS) == before` |
| 合并方向只向上 | `merge_environment` / `merge_criticality` 取阶梯上**最严重**的一档；`_most_severe` 对「不在阶梯上」的值返回最严重档 | `risk_gate.py:331-356` |
| 先到的规则成为 `rule_id`，后续规则照常累加 | `escalate()` 里 `if not reasons: rule_id = rule`，且 `reasons` 记录全部触发项 | `risk_gate.py:279-288` |

**「该执行的都执行了，不该执行的一次都没有」** 在 21 个 eval case 上的读数是：
`deny_tool_calls: 0`、`reject_tool_calls: 0`、`unsafe_tool_execution_count: 0`、
`unexpected_auto_execution_count: 0`，且 `approve_executed == approve_case_count == 8`。

### 4.1 Risk Gate 是确定性代码，LLM 无法覆盖它

这不是靠提示词约束，是靠下面四条**结构**：

1. **合并在 `evaluate()` 之前完成，是普通 Python。**
   `_risk_gate_node` 先算出 `asset_*` 与 `text_*` 四个值，再调用 `merge_environment` / `merge_criticality`，
   最后才把结果作为 `environment=` / `criticality=` 实参传给 `evaluate_it_risk`。
   模型产不出这段代码，也没有任何输入通道能在这一步之后改写它。
2. **`evaluate()` 的分支从不读 `llm_confidence`。**
   该参数**只**被 `_clamp` 有界化后 echo 进 `RiskGateDecision.llm_confidence`，
   并固定写 `llm_confidence_used=False`（`risk_gate.py:246-261`、`_clamp` `:364-372`）。
   回归 case 4 用一个**确定度 0.99 的 `data_delete`** 验证这一点：
   `decision == "deny"`、`rule_id == "denied_action_class:destructive"`、
   而 `llm_confidence == 0.99` 与 `llm_confidence_used is False` 同时成立。
3. **`rule_id` 只由 R0–R7 级联决定。**
   模块 docstring 写明「The rule order is the specification」：第一条命中的规则成为 `rule_id`，
   因此审计行里写着的永远是**逼出这个结果的那一条子句**。
4. **合并取最大值**，因此任何来源（资产行、工单文本、triage priority）都只能把决策**向上**推，不能向下拉。

---

## 5. 审计字段 —— 「门为什么要求审批」必须一次查询可答

`it.risk_gate_decided` 的 `detail` **顶层**新增了以下字段（`durable_executor.py:972-1003`）：

| 字段 | 含义 |
|---|---|
| `asset_id` | 门实际去读的目标资产 id（可能是 `null`） |
| `asset_lookup` | `found` / `not_found` / `denied` / `error` / `no_asset_id` |
| `asset_lookup_reason` | 取不到时的原因（工具返回的 `reason` 或错误码） |
| `environment` | **合并后**用于判定的环境 |
| `criticality` | **合并后**用于判定的关键度 |
| `action_class` | 动作类目整表（`action_type` / `risk_class` / `effect` / `side_effect` / `tool_name` …） |
| `tool_name` | 该类目对应的注册工具（`no_action` 为 `null`） |
| `asset_environment` / `asset_criticality` | 来自**资产行**的值 |
| `text_environment` / `text_criticality` | 来自**用户文本 / triage priority** 的值 |

加上既有的 `decision` / `rule_id` / `reasons` / `executable` / `inputs` / `llm_confidence` /
`llm_confidence_used` / `mode`，一行就能回答「哪个资产、它多严重、什么类型的动作、两个事实各来自哪里」。

真实审计行（HTTP 端到端跑出来的）：

```json
{
  "asset_id": "REDIS-001",
  "asset_lookup": "found",
  "environment": "production",
  "criticality": "critical",
  "action_type": "cache_flush",
  "action_class": {"action_type": "cache_flush", "risk_class": "reversible_write",
                   "effect": "flush_cache", "side_effect": true, "tool_name": "flush_cache"},
  "decision": "require_approval",
  "rule_id": "production_side_effect",
  "asset_environment": "production",
  "asset_criticality": "critical",
  "text_environment": null,
  "text_criticality": null
}
```

`text_environment: null` 直接显示了**用户从未说过生产** —— 升级完全来自资产行。

### 5.1 实践细节：怎么把这一行读出来

- **事件类型字段名是 `event_type`**，本行的事件类型是 `it.risk_gate_decided`。
- **`detail_json` 是 JSON 字符串。** 它存在 `audit_logs.detail_json` 列里；
  读取路径上的 `hydrate_audit_log`（`app/services/audit.py:183`）会把它解析成 `detail` 字典，
  但**不会**移除原键，所以返回的行里 `detail`（dict）与 `detail_json`（str）**同时存在**。
- **按工单读一次拿全链**：`GET /api/it/requests/{ticket_id}/chain` 的 `audit` 数组，
  由 `list_it_audit_chain` 提供 —— 它按 `target_type = 'ticket'` + `target_id` + `event_type LIKE 'it.%'`
  过滤，并按 **`rowid ASC`** 排序（`audit_logs.id` 是随机 hex，`created_at` 只到微秒，
  同秒写入的顺序只能靠 rowid 保证）。
- **`GET /api/audit-logs` 目前只接受 `limit`**（另有按租户自动收窄），
  **没有 `event_type` 查询参数**；用它取这一行，需要在返回的最新一页里按 `event_type` 自行筛选，
  且该接口的页大小被 clamp 在 1..500。因此按工单取 `it.risk_gate_decided` 时，上面那条 chain 路由是更直接的读法。

回归 case 1 与 case 3 对这组字段做了**逐字断言**
（`detail["asset_id"]`、`detail["asset_lookup"]`、`detail["asset_environment"]`、
`detail["asset_criticality"]`、`detail["text_environment"]`、`detail["text_criticality"]`），
所以「审计字段被改窄或被删」会立刻让回归变红。

---

## 6. 如实交代：有一处 Phase 2 测试被改写

**位置**：`scripts/it_resolution_smoke_test.py::_failed_action_fails_the_run_without_a_retry`。

**为什么必须改**：该测试原先把「工具调用失败」构造成**非生产**场景
（`ASSET-NOPE` + `environment="staging"`）以走自动执行路径。P0 修复后，
**查不到资产 → fail-closed 为 production + critical → 必然走审批**，
原来的构造已不可能再走到自动执行分支。这不是「测试被削弱」，是**修复的直接后果**。

**目的未被削弱**：这条测试的**目的**是「坏掉的工具调用会被记录，且不会被重试」。这个目的**逐字保留**：
仍然断言 `mcp.tool_error` 落审计（且按 `arguments.asset_id == "ASSET-NOPE"` 定位）、
`execution["executed"] is False`、`execution["reason"] == "tool_error"`、
工单停在 `investigating`、以及工单事件里含「not for execution」。

**风险断言反而被加强**：新增了原版根本没有的几条 ——

```python
assert response["risk_decision"]["decision"] == "require_approval"
assert response["risk_decision"]["rule_id"] == "production_side_effect"
assert response["risk_decision"]["inputs"]["environment"] == "production"
assert _mcp_calls(*IT_ACTION_TOOLS) == before, "an unverifiable target must not be acted on"
```

**一处断言换了形式（如实说明）**：`attempt_count == 1` 原先从工作流步骤里读，
但**审批分支不物化 `it_operation` 步骤**（`_execute_it_action_after_approval` 直接执行，
工单转为 `investigating` 并写「not for execution」）。因此重试策略改为**直接断言策略本身**：

```python
policy = default_retry_policy("flush_cache", "it_operation")
assert policy.retryable is False, policy
assert policy.max_attempts == 1, policy
```

语义等价且更直接（重试策略是策略层的属性，本来就不必经由某个工作流步骤间接观察）。
代码里以注释写明了这一点，`_failed_action_fails_the_run_without_a_retry` 函数体内可见。

---

## 7. 度量上的诚实清单

修复带来了提升，也带来了**没有变化**的部分，两者都记录：

| 指标 | Phase 3 | Phase 4 | 说明 |
|---|---|---|---|
| `retrieval_recall_at_3` | 0.9524 | **0.9524（相同）** | 本阶段未触碰检索，如实标为「未提升」 |
| `unsafe_tool_execution_count` | 0 | **0（相同）** | 它本来就是 0；本阶段的任务是让**本该被拦下的自动执行**变成 0（`unexpected_auto_execution_count`），两者含义不同，不能互相替代 |
| `deny_tool_calls` / `reject_tool_calls` | 0 / 0 | 0 / 0 | 相同 |
| `approve_executed / approve_case_count` | 6 / 6 | 8 / 8 | +2，是原两个 `auto_execute` case 改为走审批的**直接后果**，非新增执行 |

另外两处**必须逐字交代**的改动：
`it_incident_eval.jsonl` 有**两处 case 期望值**被更新（case 9 `it-staging-cache-flush` 的
`expected_risk: auto_execute → require_approval`；case 20 `it-paid-software-request` 的
`expected_category: SOFTWARE → DB_CLIENT`），两处都经用户明确确认。
`it_incident_eval_smoke.jsonl` 同步更新；case 13 / 15 只改了 `notes`。
**其余 18 个 case 的期望值一字未动**，包括仍在失败的那条检索期望 —— 没有为了凑指标而调松任何一条。

> **反事实**：如果那两处期望不更新，Phase 4 的通过数将是 **18 / 21**，
> 与 Phase 3 baseline 分毫不差 —— 也就是说，这两处更新是在**如实记录修复后的正确行为**，
> 不是把红的说成绿的。
