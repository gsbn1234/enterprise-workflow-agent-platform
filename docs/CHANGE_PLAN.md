# CHANGE_PLAN.md — Phase 5-1：LLM 工程化改造

> **这是什么。** 在既有企业 IT 服务 Agent 平台（`e67dc21`）上做的**增量工程优化**。
> 目标不是「接入 LLM」——项目里本来就有 LLM；目标是让 LLM 的每一次调用都有
> **明确的失败语义、明确的回退路径、可记录的成本与延迟**，并且**在任何失败下都不改变
> 由确定性代码负责的那十个安全决策**。
>
> **一句话不变式：LLM 是增强能力，不是单点故障。**
>
> 本文按 §十八 要求逐文件记录：**文件 / 旧逻辑 / 问题 / 新逻辑 / 为什么这么改 / 测试方式**。
> 所有结论均来自当前仓库真实代码，不虚构。

---

## 0. 范围边界（先写清楚「不做什么」）

### 0.1 绝对不能改动的安全边界 —— 本次全部未触碰

| # | 边界 | 本次改动 | 证据 |
|---|---|---|---|
| 1 | Risk Gate | **零改动**（`risk_gate.py` 不在 diff 中） | `git diff --stat` 无该文件 |
| 2 | RBAC | **零改动** | `app/services/it/rbac.py` 未修改 |
| 3 | Tool permission | **零改动** | `app/services/it/rbac.py::authorize_tool_call` 未修改 |
| 4 | Forbidden action | **零改动** | `ACTION_CLASSES.allowed=False` 的类仍被拒 |
| 5 | Ticket status transition | **零改动** | `app/services/it/execution.py` 未修改 |
| 6 | Human approval requirement | **零改动**，且**只能被 LLM 升高、不能被降低** | `_apply_expert_risk_suggestion` 仍是 `or` 合并 |
| 7 | Tool execution allowlist | **零改动** | `app/services/tools/registry.py` 未修改 |
| 8 | Checkpoint / resume | **零改动** | LangGraph `SqliteSaver` 接线未动 |
| 9 | Audit | **零改动**（只**新增** `llm` 字段，不删不改既有字段） | `audit_logs` 表结构与哈希链未动 |
| 10 | 最终是否允许执行工具 | **零改动**（`llm_confidence_used` 仍恒为 `False`） | `risk_gate.py` 不读 `llm_confidence` |

**严禁的四件事，本次代码中均不成立：**

- ❌ LLM → 直接决定是否执行生产操作 —— `evaluate()` 的 7 个输入里没有 LLM 产物
- ❌ LLM → 直接绕过 Risk Gate —— 风险决策仍由 R0–R7 规则阶梯唯一决定
- ❌ LLM → 直接调用工具 —— Agent 拿不到任何 Python 工具函数，只能走 Tool Registry
- ❌ LLM → 决定 approval 是否必须 —— 审批合并是 `or`，模型只能加不能减

### 0.2 明确不修改的文件与理由

| 文件 | 为什么不动 |
|---|---|
| `app/services/it/risk_gate.py` | 模块 docstring 承诺 *Pure. No `app.*` imports, no database, no I/O, no LLM.* 这是承重不变式。接一次 LLM 就毁了它。 |
| `app/services/it/rbac.py` / `execution.py` / `tools/registry.py` | 属于 §三 的安全边界，接 LLM 即为违规。 |
| `app/services/it/intake.py` | 工单落库路径保持纯确定性；LLM 兜底发生在**图内**的 triage 节点，不在落库路径上。 |
| `scripts/evaluate.py` | 六个指标与 `unexpected_auto_execution_count` 已存在，直接从真实 run 工件计算。Phase 5-1 不改评测口径。 |
| `frontend/` | 本阶段无前端需求，**未改任何前端文件**。（工作区里 `frontend/` 确有改动，那是更早的 Phase 6 未提交内容，不是本阶段的。）仍按 CI 跑一次 `npm run build` 验证未被波及。 |
| `docker-compose*.yml` / 数据库选型 | §十七 明确禁止换 MySQL / 换掉 PostgreSQL。 |

### 0.3 不引入新依赖

`requirements.txt` **未修改**。新增的 `llm_calls` 表用已有的 `sqlite3` / `psycopg` 通道；
schema 校验用**已有**的 Pydantic 2.7；指标暴露接**已有**的 Prometheus 文本端点；
错误分类用已有 `httpx` 异常。**没有引入新的 Python 依赖。**

---

## 1. 改动总览

> **关于下面各节标题里的 `+N 行`**：那是 `git diff --numstat e67dc21` 的**新增**行数，
> 基线是上一个提交，不是「本阶段的净改动」。其中 `triage.py` 与 `durable_executor.py`
> 两份文件同时承载 Phase 4 的修复，所以它们的数字包含 Phase 4 的部分；
> 其余文件的数字就是本阶段的改动。删除行数一并列在 §2 各节的正文里。
> 这里给数字是为了让「改了多大」可核对，不是为了把它们说得多。

### 1.1 MODIFY（10 个文件）

| 文件 | 一句话 |
|---|---|
| `app/services/llm.py` | 把「返回字符串或抛异常」改成「返回 `LlmOutcome`」；补齐 6 态状态机、一次性重试、真实 token 采集 |
| `app/services/multi_agent/agents.py` | 5 个专家调用点全部改走 `LlmOutcome`；新增 Runbook 候选（**仅供建议**）；修掉 `reasoning_mode` 说谎 |
| `app/services/it/triage.py` | 新增**低置信度兜底**（只在确定性分类器没把握时触发，且只能升级不能降级） |
| `app/services/multi_agent/durable_executor.py` | `it_triage` 节点接入兜底；新增审计事件 `it.triage_llm_fallback_accepted/rejected` |
| `app/services/agent/planner.py` | 计划失败时把失败**写进 plan.reason**，不再让「LLM 关着」和「LLM 超时」长得一样 |
| `app/services/agent/executor.py` | 最终答复润色改为 outcome 驱动；成本估算**优先用 provider 真实 token** |
| `app/utils.py` | 新增 `token_cost()`：真实 token → 成本；拿不到就回退原有字数估算 |
| `app/services/metrics.py` | Prometheus 端点新增 6 个 LLM 指标（**接入既有体系**，不引入新监控栈） |
| `app/db.py` | 新增 `llm_calls` 表 + 4 个索引 + RLS 登记 |
| `app/config.py` | 新增 7 个配置项 + 2 条取值校验 |

### 1.2 ADD（3 个文件）

| 文件 | 作用 |
|---|---|
| `app/services/llm_schemas.py` | 4 个 Pydantic 结构化输出契约（**不重复创建已有 Schema**） |
| `app/services/llm_telemetry.py` | `llm_calls` 表的唯一写入者 + 汇总查询 |
| `scripts/llm_engineering_smoke_test.py` | §十五 要求的 14 项场景，共 26 个测试函数 |

### 1.3 KEEP（明确保留、未做任何功能改动）

- 5 个既有专家 LLM 调用点**全部保留**（用户决策：「保留 + 只加固」）——
  只给它们加上失败语义与遥测，不删、不改其提示词意图、不让它们获得任何决策权。
- 21 个 LangGraph 节点结构、`it_triage` 的确定性分类表、Risk Gate 的 R0–R7 规则阶梯、
  HITL 的 `interrupt()` / `Command(resume=...)`、审计哈希链 —— **全部保留**。

### 1.4 MODIFY（2 个非源码文件）

| 文件 | 一句话 |
|---|---|
| `.github/workflows/ci.yml` | 接入新套件；并补上一条注释里声称存在、实际不存在的 job 级 env |
| `docs/KNOWN_LIMITATIONS.md` | 新增第 21–23 条；重写「下一步方向」第 4 条（真实 provider 路径仍未验证） |

另有 `docs/CHANGE_PLAN.md`（本文件，ADD）。
工作区里还有本阶段**没有碰过**的 Phase 6 展示页改动（`app/services/it/demo_scenarios.py`、
`docs/PHASE6_REPORT.md`、`frontend/src/ITChain.jsx` 等），不在本阶段范围内。
---

## 2. 逐文件改动明细

### 2.1 `app/services/llm.py`（MODIFY，+549 / −57）

**旧逻辑**

```python
def complete_json(...) -> dict:      # 成功返回 dict
    ...                              # 失败 raise LLMError / LLMTimeoutError / ...
def complete_text(...) -> str:
```
调用方拿到的是**值或者异常**二选一。异常类型虽有区分（`LLMTimeoutError` /
`LLMProviderError` / `LLMInvalidOutputError` / `LLMParseError` / `LLMDisabledError`），
但一旦被 `except LLMError: return None` 吞掉，**失败原因就永久消失了**。

**问题**

1. **失败原因不可携带。** 调用方无法知道刚才那次失败是「关着」「超时」「对端 5xx」
   还是「模型返回的不是 JSON」。这四种在运维上是完全不同的四件事。
2. **没有重试。** 一次网络抖动就浪费掉一次专家推理。
3. **token 用量被丢弃。** provider 在响应体 `usage` 里给了真实 token 数，
   但旧路径只取 `choices[0].message.content`，`usage` 读都没读。
   → 成本只能靠字数猜（见 2.9）。
4. **`response_format` 未使用。** 结构化输出只靠提示词「请返回 JSON」，
   provider 侧没有任何约束。

**新逻辑**

- 新增 **`LlmOutcome`** frozen dataclass（`llm.py:125`），承载 §四 要求的全部字段：
  `status / operation / provider / model / value / latency_ms / prompt_tokens /
  completion_tokens / total_tokens / usage_available / error_type / error_message /
  retry_count / fallback_used / schema`。
  字段以**项目现有架构**为准，未机械照抄 §四 的建议列表
  （例如 §四 建议的 `provider` 保留，`token_usage` 拆成三个可选 int 更贴合本项目
  「拿不到就是 `None`」的既有约定）。
- 三个语义清晰的谓词：`ok`（成功）/ `disabled`（关着）/ `failed`（真的失败了）。
  **「关着」不是「失败」** —— 这是整个改造里最关键的一次区分：
  `disabled` 既不是 `ok` 也不是 `failed`，配置问题与故障问题从此可分。
- **6 态状态机**（`llm.py:45-52`）：`disabled / success / timeout / provider_error /
  invalid_output / parse_error`，与 §四 逐条对应。
- **一次性重试**（`llm.py:245`）：`attempts = 1 + settings.llm_max_retries`（上限 1）。
  只有 `exc.retryable` 为真才重试 —— **4xx 不重试**（我们的请求有问题，重发还是错），
  **5xx / 超时重试一次**（对端故障，值得一次往返）。重试有
  `retry_backoff_seconds * (attempt + 1)` 的线性退避，并打 `llm.call_retrying` 日志。
- `_usage(data)`（`llm.py:571`）读 provider 的 `usage` 块；
  **读不到就 `usage_available=False` 且三个 token 字段保持 `None`**
  —— 严格执行 §十二「不要伪造数字」。
- `llm_json_mode` 打开时发送 `response_format={"type": "json_object"}`，
  让 provider 侧约束输出，而不只是提示词「请求」。

**为什么这么改** —— 一句话：**把「失败」从异常里救出来，变成一等公民的数据。**
只有失败可携带，调用方才能对不同的失败做不同的事（超时可以重试、schema 违规要回退、
禁用要静默），也才谈得上「LLM 是增强能力，不是单点故障」。

**测试方式** —— `scripts/llm_engineering_smoke_test.py`：
`_llm_disabled_is_not_a_failure` / `_success_records_real_usage` /
`_missing_usage_is_recorded_as_missing` / `_timeout_is_named_and_retried_once` /
`_provider_error_retries_only_when_the_provider_is_at_fault` /
`_invalid_json_is_a_parse_error_and_is_not_retried` /
`_schema_violation_is_invalid_output_not_a_crash` /
`_a_transport_failure_then_a_success_is_one_retry` / `_the_retry_ladder_is_capped_at_one`。
断言方式是把 `llm._post` 换成一个记录调用的假传输层 —— **「没有重试」只能靠数调用次数证明**。

---

### 2.2 `app/services/llm_schemas.py`（ADD，新文件）

**旧逻辑** —— 不存在。

**问题** —— §五 要求 `LLM → Structured Output → Pydantic Schema → Validation → 业务代码`，
但项目里没有任何**契约层**：模型输出的字段名、类型、取值全靠调用方手动 `.get()` 读。

**新逻辑** —— 4 个 `pydantic.BaseModel` 契约：

| Schema | 用于 | 关键约束 |
|---|---|---|
| `IntentFallbackResult` | 低置信度 triage 兜底 | `intent: str = Field(min_length=1)`；`confidence: float` 带 `ge=0, le=1` |
| `RunbookCandidate` / `RunbookCandidateResult` | Runbook 候选步骤 | `description` 非空，`action_type` 非空 |
| `RiskVoteSuggestion` | 合规 / 运维风险投票 | `needs_approval: bool`，`risk_level` 在枚举内 |
| `CriticReviewResult` | Critic 复核 | 结论枚举 + 理由 |

**为什么这么改**

- **不重复创建已有 Schema**（§五-1/2/3）：项目里已有的业务 Schema
  （`app/schemas.py` 的请求/响应模型、`risk_gate.ActionClass`、`triage.TriageResult`）
  一律复用，`llm_schemas.py` **只放 LLM 专属的输出契约**，因为它们描述的是
  「模型该返回什么形状」，不是「业务实体是什么」。两者职责不同，合并反而会让
  业务 Schema 被模型输出格式绑架。
- **`min_length=1` 这类约束是防御性的**：空字符串是一个**形状错误**，
  在没有 schema 的世界里它会安静地流进业务代码，然后在某处 `if not intent` 才被发现。
  让 Pydantic 在读字段之前就拦住它，是 §五-4「validation 失败必须进入明确 fallback」的前提。
- **Schema 校验失败不能导致 Agent 崩溃**（§五-5）：`_validate()` 抛
  `LLMInvalidOutputError`，被 `call()` 捕获后变成 `status="invalid_output"` 的 **outcome**，
  而不是一个冒泡到节点的异常。这条由 `_schema_violation_is_invalid_output_not_a_crash` 守住。

**测试方式** —— 同上文件；schema 违规、词汇表外取值两类各有独立用例。

---

### 2.3 `app/services/llm_telemetry.py`（ADD，新文件）

**旧逻辑** —— 不存在。§十一 要求的 provider / model / operation / latency / 成败 /
error_type / retry_count / fallback_used / token 全部**无处可查**。

**问题** —— 「LLM 这次花了多少钱、慢在哪、失败率多少」在改造前**无法回答**。

**新逻辑** —— `llm_calls` 表的**唯一**写入者，三条成文规则写在模块 docstring 里：

1. **被禁用的调用不记录。** `if outcome.status == STATUS_DISABLED: return None`。
   理由：`llm_status()` 已经回答了「LLM 开着没有」，把每一次「因为关着所以没调用」
   也写进表，只会让表里 99% 的行都是同一个事实，把真正的失败淹掉。
2. **provider 没给 usage 就记成「没给」。** 存 `usage_available=0` 且 token 列为 `NULL`，
   **不写 0** —— 让「provider 说花了 0」和「provider 什么都没说」保持可区分。
3. **记录永不抛异常。** 写库整段包在 `try/except` 里，失败只打
   `llm.telemetry_write_failed` 警告日志。**遥测不能成为它观测的那件事的新故障点。**

另提供 `llm_usage_summary(tenant_id=None)`（totals / by_status / by_operation 三段）
与 `llm_call_statuses()`。`error_message` 截断到 500 字符
（`MAX_ERROR_MESSAGE`），避免把整段模型输出写进表。

**为什么这么改** —— §十一 明确「不要为了这个功能引入新的监控技术栈」。
写入者与读取者分离（写入只有这一个模块，读取有 metrics 端点 + summary 函数）
使得「记录口径」只有一处可改。

**测试方式** —— `_telemetry_never_breaks_the_thing_it_observes`：
把 `llm_telemetry.get_connection` 替换成必然抛异常的桩，
断言调用**仍然成功**。`_disabled_calls_are_not_recorded`：
禁用状态下调用，断言表行数**不变**。

---

### 2.4 `app/db.py`（MODIFY，+41 / −0）

**旧逻辑** —— 建表语句里没有 LLM 相关的表。

**问题** —— 见 2.3。

**新逻辑**

- 新增 `llm_calls` 表（`db.py:666` 附近），20 列，**除主键与 `created_at` 外全部可空**，
  因为很多字段在某些失败状态下本就无从得知（超时就没有 token，失败就没有 schema）。
- 4 个索引：`created_at`、`(tenant_id, created_at)`、`(operation, status)`、
  `multi_agent_run_id` —— 分别服务「最近发生了什么」「某租户的用量」
  「某操作的失败率」「某次多 Agent 运行的调用链」四个真实查询。
- `TENANT_RLS_TABLES` 加入 `"llm_calls"`，与其它租户表享受同一套 RLS 策略。
- 表注释写在 SQL 里，记录**为什么是独立表而不是 `workflow_runs` 上的几列**：
  一次 run 会发多次调用且结果各不相同，平均值恰好会掩盖最该看见的东西。

**为什么这么改** —— §十七 禁止新增 MySQL / 更换 PostgreSQL，因此直接用既有通道。

**测试方式** —— `llm_engineering_smoke_test` 全程读写该表；
`postgres_rls_smoke_test`、`tenant_isolation_smoke_test` 验证租户隔离未被破坏。

---

### 2.5 `app/config.py`（MODIFY，+25 / −0）

**旧逻辑** —— 已有 `llm_enabled` / `llm_provider` / `llm_model` / `llm_timeout_seconds` /
`llm_planner_enabled` / `llm_final_answer_enabled` / `llm_multi_agent_reasoning_enabled` /
`llm_planner_min_confidence`。

**问题** —— 重试次数、JSON 模式、triage 兜底开关、兜底置信度阈值、Runbook 开关、
遥测开关，**六个新决策没有配置面**，只能硬编码。

**新逻辑** —— 按**项目既有命名风格**（`AGENT_LLM_*` + 小写属性名）新增 7 项：

| 配置项 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `llm_max_retries` | `AGENT_LLM_MAX_RETRIES` | `1` | 校验只允许 `0` 或 `1` |
| `llm_retry_backoff_seconds` | `AGENT_LLM_RETRY_BACKOFF_SECONDS` | `0.5` | 线性退避基数 |
| `llm_json_mode` | `AGENT_LLM_JSON_MODE` | `true` | 是否发 `response_format` |
| `llm_triage_fallback_enabled` | `AGENT_LLM_TRIAGE_FALLBACK_ENABLED` | `= llm_enabled` | **继承总开关** |
| `llm_triage_min_confidence` | `AGENT_LLM_TRIAGE_MIN_CONFIDENCE` | `0.55` | 兜底触发阈值 |
| `llm_runbook_candidates_enabled` | `AGENT_LLM_RUNBOOK_CANDIDATES_ENABLED` | `= llm_enabled` | Runbook 候选 |
| `llm_telemetry_enabled` | `AGENT_LLM_TELEMETRY_ENABLED` | `true` | 遥测写入 |

两条新校验（按 §十三「扩展既有 config，不要重新设计」的写法加在既有校验块里）：

```python
if settings.llm_max_retries not in {0, 1}:
    raise ValueError("AGENT_LLM_MAX_RETRIES must be 0 or 1. ...")
if not 0.0 <= settings.llm_triage_min_confidence <= 1.0:
    raise ValueError("AGENT_LLM_TRIAGE_MIN_CONFIDENCE must be between 0 and 1.")
```

**为什么这么改**

- **重试上限写进校验而不是文档**：§十 要求「最多 1 次 retry，不要无限重试」。
  写在注释里是约定，写在 `raise` 里是约束 —— 有人把环境变量设成 5 会**启动失败**。
- **兜底开关默认继承 `llm_enabled`**：兜底是**兜底**，总开关关了它必须跟着关。
  否则会出现「LLM 已禁用但 triage 仍在联网」这种最难排查的组合。
  这直接服务 §十四「LLM 关掉时系统必须完好」。

**测试方式** —— `_Setting` 上下文管理器直接切换这些属性；
`_low_confidence_triage_asks_the_model` 与 `_high_confidence_triage_does_not_ask_the_model`
分别验证阈值两侧的行为。
---

### 2.6 `app/services/multi_agent/agents.py`（MODIFY，+302 / −31；核心改造）

**旧逻辑**

```python
def _expert_json(system, payload) -> dict | None:
    if not settings.llm_multi_agent_reasoning_enabled or not llm_ready():
        return None
    try:
        return complete_json(...)
    except LLMError:
        return None
```

5 个调用点（evidence_synthesis / 合规风险投票 / 运维风险投票 / critic 复核 /
resolution 诊断润色）全部用它。

**问题 —— 这是整个 Phase 5-1 最核心的一处缺陷**

1. **`None` 一个值表示三种完全不同的情况**：LLM 关着、调用失败、返回读不懂。
   调用方只能写 `if result is None`，于是**「从未联系过模型」与「模型超时了」
   产生逐字节相同的 Agent payload**。
2. **`reasoning_mode` 在说谎。** 旧代码在调用失败时标 `llm_augmented`
   （"已由 LLM 增强"）—— 而它根本没有增强任何东西，调用是失败的。
   更糟的是另一条：失败路径与关闭路径都落到 `deterministic_*`，
   读起来像「本次没有模型参与」。**有参与，只是没答上来。**
   一个排查质量下降的人必须能区分「LLM 关了」和「LLM 坏了」——
   这正是 §十一 遥测存在的理由，而旧代码把它抹掉了。
3. **无遥测**：这 5 个调用点不写任何记录。
4. **无 schema**：模型返回的 `needs_approval` 是字符串 `"no"` 还是布尔 `false`
   全靠调用方自己 `bool()`，一处笔误就可能删掉一次人工审批。

**新逻辑**

**(a) `_expert_json` → `_expert_call(...) -> LlmOutcome`**（`agents.py:1293`）

签名 `(agent_name, system_prompt, payload, *, operation, schema=None, max_tokens=600)`。
**它永不抛异常** —— docstring 里写着理由：「模型是对这些 Agent 的增强，
而一个能拖垮它所增强的工作流的增强，就不是增强。」
禁用时返回 `status="disabled"` 的 outcome，失败时返回真实失败状态，
成功时 `record_llm_call` 落一行遥测并在失败时打 `multi_agent.expert_reasoning_failed` 警告日志。

**(b) 三态 `_reasoning_mode(outcome, deterministic=…, augmented=…)`**（`agents.py:1368`）

| 状态 | 返回 | 含义 |
|---|---|---|
| `outcome.ok` | `augmented`（如 `llm_augmented`） | 模型确实增强了 |
| `outcome.failed` | `f"{deterministic}_llm_failed"` | **模型参与了，但失败了** |
| 其它（disabled） | `deterministic` | 模型没参与 |

第三态就是修复本身：`deterministic_policy_llm_failed` 与 `deterministic_policy`
再也不会被混为一谈。

**(c) `_llm_trace(outcome) -> dict`**（`agents.py:1384`）——
5 个调用点 payload 里统一挂一个 `"llm"` 块，字段形状**完全一致**
（docstring：「一个同时读合规 Agent 与 critic payload 的人，不该被迫学两套词汇」）。
token 数旁边永远带 `available` 标志，避免「provider 没报」被读成「没花钱」。

**(d) 5 个调用点全部接入 schema**

| operation | schema |
|---|---|
| `evidence_synthesis` | `EvidenceSynthesisResult` |
| `resolution_diagnosis_polish` | —（文本润色，无需 schema） |
| `compliance_risk_vote` | `RiskVoteSuggestion` |
| `operational_risk_vote` | `RiskVoteSuggestion` |
| `critic_review` | `CriticReviewResult` |

**(e) 场景二：`ResolutionAgent._runbook_candidates()`**（`agents.py:476`，§六 允许的第二个场景）

**这是本次唯一新增的 LLM「能力」，因此它的边界写得最死：**

- **不重复创建 Agent**（§八）：`ResolutionAgent` 已存在，只加一个方法，**不新建任何 Agent**。
- **不触碰 `action_type`**：`resolution["action_type"]` 是 Risk Gate 唯一读的字段，
  本方法及其下游**从不写它**。候选步骤以 `advisory_only: True` 挂载，
  让读者不可能把它误当成计划。
- **两道代码闸门在报告之前就执行**：
  1. `ACTION_CLASSES` 成员检查 —— 平台不认识的动作名**直接丢弃**，
     不留给下游解释（这就是 §八 要求的 Deterministic Action Mapping）；
  2. `action_class.allowed == False` 的类**同样丢弃** ——
     `data_delete` 这类被拒类是 `ACTION_CLASSES` 的键（Risk Gate 需要能**命名**它拒绝了什么），
     但把它列进候选会被读成「平台推荐了一件它已决定永不执行的事」。
- **无证据时根本不问**（§八）：`resolution["status"] != "PROPOSED"` 或
  `evidence` 为空 → 直接返回空。`NO_KNOWLEDGE` 意味着检索**没找到可依据的策略**，
  此时问模型「该怎么做」等于让模型**发明平台刚刚说过自己没有的知识**。
- **`requires_approval` 只能升不能降**：`bool(action_class.requires_approval or step.get("requires_approval", True))`。
- 最多 5 步，`description` 截断 400 字符。

**(f) `_apply_expert_risk_suggestion(vote, outcome)` 重写**（`agents.py:1457`）
保持**只升不降**的合并语义不变（§六「Approval 不接 LLM 决策」），
但补上 `reasoning_mode` 与 `llm` 轨迹；`not outcome.ok` 时**提前返回**，
确定性投票原封不动。

**为什么这么改** —— 5 个调用点**保留**（用户决策「保留 + 只加固」）：
它们本来就是项目设计的一部分，删掉是功能倒退。加固的方向是
**给它们失败语义、给它们 schema、给它们遥测**，而不是给它们更多权力。

**测试方式** —— `_a_model_vote_cannot_remove_an_approval`（模型说 low risk / 不需要审批，
断言 `needs_approval` 仍为 `True`、`risk_level` 未被降级）/
`_an_unreadable_approval_claim_is_silence_not_permission`（`"no"` / `"maybe"` / `null` / `0`
四种畸形声明，断言**每一次都仍然需要审批** —— 这是唯一一处「一个笔误就能删掉一个人」的地方，
所以单独测）/ `_a_failed_risk_vote_leaves_the_deterministic_one_intact`
（断言 `reasoning_mode == "deterministic_policy_llm_failed"`，即 D2 谎言已修）/
`_a_hostile_runbook_candidate_cannot_change_the_selected_action`（恶意候选含 `data_delete`，
断言它被**整体丢弃**、剩余步骤 `requires_approval` 不低于其动作类的要求、
且 `resolution["action_type"]` **纹丝未动**）。

---

### 2.7 `app/services/it/triage.py`（MODIFY，+148 / −1；含 Phase 4 改动）

**旧逻辑** —— `classify(objective) -> TriageResult`，纯确定性关键词分类。
**`classify()` 本身以及所有关键词表本次一行未改。**

**问题** —— 遇到「那个东西又不好使了」这类**信息量不足以分类**的输入时，
确定性分类器只能给一个低置信度的、往往是错的结果，且没有任何补救。

**新逻辑** —— 新增三个函数 + 一个私有助手，**全部是加法**：

```python
SERVICE_CODES  = tuple(code for code, _ in SERVICE_KEYWORDS)   # 词汇表由既有表派生
RESOURCE_CODES = tuple(code for code, _ in RESOURCE_KEYWORDS)
SOFTWARE_CODES = tuple(code for code, _ in SOFTWARE_KEYWORDS)
ENVIRONMENTS   = tuple(code for code, _ in ENVIRONMENT_KEYWORDS)

def needs_llm_fallback(result, threshold) -> bool:
    if result.mode != "deterministic":   # 已经是兜底结果，不递归
        return False
    return float(result.confidence) < float(threshold)

def llm_fallback(objective, base, outcome) -> TriageResult | None:
    ...  # 见下

def _fill_entity(entities, key, candidate, vocabulary) -> None:
    ...  # 只在既有值为空时填空，且取值必须在词汇表内
```

`llm_fallback` 的**四条硬边界**：

1. **`intent` 必须在 `INTENTS` 枚举内**，否则整个答案被拒（`return None`）——
   §七「intent 必须属于已有枚举」。测试里模型返回 `"MELTDOWN"`，
   断言整个兜底被丢弃、工单退回确定性结果，且留下
   `it.triage_llm_fallback_rejected` 审计行，理由 `intent_not_in_vocabulary`。
2. **`base.entities` 优先，模型只能填空缺**（`_fill_entity` 在
   `if entities.get(key): return` 处提前退出）。§七「environment 必须属于已有允许值」：
   取值必须命中 `SERVICE_CODES` / `RESOURCE_CODES` / `SOFTWARE_CODES` / `ENVIRONMENTS`
   之一才被采纳；填不进去的字段不报错，而是让它**自然地出现在 `missing_information` 里** ——
   信息缺口应该出现在服务台看得见的地方，而不是被一个猜测补上。
3. **`priority` 只能升不能降**：`if PRIORITIES.index(base.priority) > PRIORITIES.index(priority): priority = base.priority`。
4. **`needs_approval` 只能升不能降**：`bool(base.needs_approval or _needs_approval(...))`。
   合并后 `priority` / `needs_approval` 都**重新经过确定性 helper 计算**，
   不是采信模型自报的值。

`mode` 置为 `"llm_assisted"`，与 `"deterministic"` 明确区分。
`TYPE_CHECKING` 下才 import `LlmOutcome` —— **让 triage 模块在运行时仍然完全不依赖 httpx**。

**为什么这么改** —— §六 只允许三个场景，这是第一个。
它是**兜底**：只在确定性分类器**自己承认没把握**时触发；
高置信度输入**一次网络调用都不发**（由 `_high_confidence_triage_does_not_ask_the_model`
断言，它同时断言 `transport.calls` 为空**且**没有遥测行 —— 不调用就不该留痕）。
这让 LLM 的调用量与「系统的真实不确定性」成正比，而不是与流量成正比。

**测试方式** —— `_low_confidence_triage_asks_the_model` /
`_high_confidence_triage_does_not_ask_the_model` / `_the_model_may_not_invent_a_vocabulary` /
`_the_fallback_cannot_lower_an_approval_the_rules_already_raised`。
另有既有 `scripts/it_triage_smoke_test.py` 全绿，保证 `classify()` 未被波及。

---

### 2.8 `app/services/multi_agent/durable_executor.py`（MODIFY，+213 / −3；含 Phase 4 改动）

**旧逻辑** —— `_it_triage_node` 直接 `classify_it_request(objective)`，
把结果写进 state 与审计，**没有任何兜底位置**。

**问题** —— 兜底逻辑无处安放；且审计无法回答「这次分类是否求助过模型」。

**新逻辑**

```python
fresh_result = classify_it_request(objective)
fresh = fresh_result.to_dict()
ticket = _it_ticket(state, ticket_id)
stored = dict(ticket.get("triage") or {})
source = "phase1_stored" if stored else "recomputed"
if stored:
    triage = {**fresh, **stored}      # 已落库的工单分类优先
    base = _triage_result_from(triage)
    fallback_trace = None
    if needs_llm_fallback(base, settings.llm_triage_min_confidence):
        # 落库的读数本身就不确定 -> 把 *它* 作为 base 交给兜底
        merged, fallback_trace = _it_triage_fallback(
            objective, base, ticket_id=ticket_id, actor=actor, tenant_id=tenant_id)
        if merged.get("mode") == "llm_assisted":   # 只有真的合并了才替换
            triage = merged
            source = "phase1_stored_llm_assisted"
else:
    triage, fallback_trace = _it_triage_fallback(
        objective, fresh_result, ticket_id=ticket_id, actor=actor, tenant_id=tenant_id)
```

三条设计要点：

1. **已落库的工单分类优先 —— 除非它自己就不确定。**
   工单创建时已经分类过一次并落库；在这里用模型**改写一个确定的判断**，
   等于让同一个工单在不同节点上有两个身份。
   但「存在 stored」与「stored 本身不确定」是两件事，第一版把它们当成了同一件，
   于是 §六 允许的第一个场景在真实路径上成了死代码（见 §4 缺陷 B）。
   现在的判据是 stored 的**置信度**：高于阈值 -> 一个字都不动、零次模型调用；
   低于阈值 -> 把 stored 作为 base 交给兜底，模型只能在其上**补空缺**。
   `_fill_entity` 的「字段非空即返回」保证了模型无法把已判定的 `production` 降级，
   而 `priority` / `needs_approval` 是合并后由确定性 helper 重算 + `or` 运算，
   只会升级。**tickets 行不被改写**：这次细化属于本次 run，审计里以
   `source == "phase1_stored_llm_assisted"` 与 `phase1_stored` 区分。
2. 新增模块级 `_it_triage_fallback(...) -> tuple[dict, dict | None]`，
   闸门是 `settings.llm_triage_fallback_enabled and llm_ready()` 再叠加
   `needs_llm_fallback(fresh, settings.llm_triage_min_confidence)`。
   调用时把 `allowed_intents / allowed_services / allowed_resources /
   allowed_software / allowed_environments / deterministic_reading` **全部塞进 user 消息** ——
   模型看到的是**允许取值的白名单**，而不是被要求「猜一个」。
3. **审计**：`it.triage_classified` 的 detail 与节点返回值都新增 `llm` 字段
   （并新增 `source` 区分 `phase1_stored` / `recomputed` / `phase1_stored_llm_assisted`）；
   并新增两条事件 `it.triage_llm_fallback_accepted` / `it.triage_llm_fallback_rejected`
   （拒绝理由记 `outcome.status` 或 `"intent_not_in_vocabulary"`）。

4. 新增模块级 `_triage_result_from(stored: dict) -> TriageResult`：把落库的 triage
   dict 还原成 `TriageResult`，好让闸门函数能读它。字段全部防御性读取 ——
   认不出的 intent/category 原样保留字符串而不是清空，因为兜底把 base 当作
   「要细化的那份读数」，清空它会诱导模型填得比该填的更多。

**为什么这么改** —— 兜底必须在图内、在落库之后、在检索之前；
且必须**可审计**。「这次工单为什么被这样分类」应当能一次查询回答。

**测试方式** —— 两条互补的测试都走**真实图**（`it_resolve_request`）：

* `_a_whole_it_run_survives_a_dead_llm` —— LLM 全程超时：triage 仍 `deterministic`、
  类别正确、风险决策仍是 `require_approval`/`deny`、`llm_confidence_used is False`、
  resolution 仍是 `PROPOSED`/`NO_KNOWLEDGE`，**同时断言模型确实被尝试过**
  （否则「幸存」是假的）且遥测里能看到 `timeout`。
* `_an_unsure_stored_triage_still_reaches_the_model` —— 模型可用：真实建单
  （建单时 store 下的读数置信度低于阈值）→ 真实跑图 → 断言模型给出的、原始
  objective 里根本不存在的服务码出现在结果里。这条是补上缺陷 B 的回归，
  已反向验证过：把兜底那行短路掉，它立刻红。

---

### 2.9 `app/services/agent/planner.py`（MODIFY，+155 / −52）

**旧逻辑** —— `_llm_plan` 失败或低置信度时静默返回 `None`，调用方走确定性分支，
`plan.reason` 里**看不出刚才发生过什么**。

**问题** —— **D13 缺陷**：「LLM 关着」与「LLM 超时」产生**逐字相同的 plan.reason**。
对排查者而言这是最坏的一种沉默。

**新逻辑**

- `_llm_plan(objective) -> tuple[PlanDecision | None, LlmOutcome]`：
  失败时 `record_llm_call` + 日志后返回 `(None, outcome)`；
  低置信度时记一条 **`withheld`** outcome —— `status="success"` 但
  `fallback_used=True`、`error_type="retained_deterministic_plan"`。
  这个区分是刻意的：模型**答上来了**（success），但平台**决定不采纳**（withheld）。
  把它记成 failure 是歪曲事实。
- `_reason(..., *, llm_note: str = "")` 新增关键字参数（既有调用方不受影响）。
- 新增 `_llm_failure_note(outcome)`：只在 `outcome.failed` 时返回
  `f"LLM planner attempted but returned {outcome.status}{detail}，已改用确定性分类。"`，
  否则返回空串。**确定性计划的内容一字未动**，只是多了一句说明它从哪来。

**为什么这么改** —— §十四「LLM 是增强能力，不是单点故障」的可见性那一半：
失败可以被容忍，但**不可以被隐藏**。

**⚠️ 本文件在改造中引入过一处缺陷，已修复** —— `_llm_plan` 改成返回元组时，
每一条**失败**路径都正确地 `return None, outcome`，唯独**成功**路径漏掉了元组包装，
仍是 `return plan`。而调用方 `plan_workflow` 是无条件解包的：

```
supervisor failed: cannot unpack non-iterable PlanDecision object
```

整个 multi-agent run `status: "failed"`。**它只在模型正常工作时触发** ——
所有让模型失败的测试都从那几行 `return None, outcome` 走了，一次都没走到函数末尾；
上一轮真实演示之所以没暴露，只是因为当时 provider 恰好处在被 HTTP 代理拦掉的
不可达状态。真实运行的价值就在这里。详见 §4 缺陷 A。

**测试方式**

* `_a_broken_llm_leaves_the_deterministic_plan_complete`：LLM 超时下断言
  `category == "refund"`、`risk_level == "high"`、`needs_approval is True`、
  `approval_chain` 与 `proposed_tools` 均非空（**计划没有被缩短或削弱**），
  并且 `"timeout" in plan.reason`（**失败已可见**）。
* `_an_answered_planner_call_still_produces_a_plan` / `_a_reachable_provider_does_not_break_the_run`：
  覆盖上面那条缺陷所在的**成功**分支，一个答得对的 provider 驱动的完整 run
  （`run_multi_agent`）不得 `status == "failed"`。
  同样反向验证过：把成功分支改回裸返回，立刻复现同一条 `TypeError`。

---

### 2.10 `app/services/agent/executor.py`（MODIFY，+41 / −6）

**旧逻辑** —— `polish_agent_answer(...)` 返回 `str | None`，
调用方 `if polished: final_answer = polished`。失败与禁用不可分；
且 `cost_estimate` 一律由字数估算。

**问题** —— 场景三（最终答复润色）缺失败语义；成本估算与 provider 真实用量脱钩。

**新逻辑**（`_complete_run` 内）

```python
polish = polish_agent_answer_outcome(
    final_answer, status=status,
    category=run_before.get("category"), risk_level=run_before.get("risk_level"))
record_llm_call(polish, workflow_run_id=run_id)
if polish.ok and polish.value:
    final_answer = str(polish.value)
cost = _run_cost(run_before, final_answer, polish)
```

新增 `_run_cost(run_before, final_answer, polish)`：
**`polish.usage_available and polish.total_tokens is not None` 时用 `token_cost(...)`，
否则回退到原有的 `estimate_token_cost(final_answer)`。**

**为什么这么改** —— §九「LLM 失败必须 fallback 到 deterministic answer，
不能因为 LLM 挂了导致 IT 工单失败」：润色失败时 `final_answer` 保持原值，
**一个字符都不会丢**。成本口径改为「有真数就用真数」，但**回退路径保持原算法**，
因此关闭 LLM 时评测数字与改造前完全一致（实测 `avg_cost_estimate` 未变，见 §4）。

**测试方式** —— `_a_broken_llm_leaves_the_deterministic_final_answer`：
503 下断言 `outcome.value is None` 且 `final_answer` **仍等于原文**；
成功路径断言润色结果仍包含原始工单号（**没有增加新事实**）。

---

### 2.11 `app/utils.py`（MODIFY，+20 / −1）

**旧逻辑** —— `estimate_token_cost(text)`：按字符数估算 token 再乘单价。

**问题** —— 有真实 token 时仍走估算，成本数字与实际用量无关。

**新逻辑**

```python
COST_PER_TOKEN = 0.000002

def estimate_token_cost(text: str) -> float:      # 原样保留
    ...

def token_cost(total_tokens: int | None) -> float | None:
    """Real tokens → cost. ``None`` in, ``None`` out — never a fabricated zero."""
    if total_tokens is None:
        return None
    return round(max(0, int(total_tokens)) * COST_PER_TOKEN, 6)
```

**为什么这么改** —— §十二「不要伪造数字」：拿不到就返回 `None`，
让调用方**必须**显式选择回退，而不是悄悄拿到一个 0 然后以为这次调用免费。

**测试方式** —— 由 `_missing_usage_is_recorded_as_missing` 与 workflow 评测的
`avg_cost_estimate` 未变共同覆盖。

---

### 2.12 `app/services/metrics.py`（MODIFY，+61 / −0）

**旧逻辑** —— `prometheus_metrics()` 输出业务与多 Agent 指标，无 LLM 指标。

**问题** —— §十一 的指标无处暴露。

**新逻辑** —— 在既有 `prometheus_metrics()` 里加一个
`GROUP BY (operation, model, status, error_type)` 查询 + 一个 token 聚合查询，输出：

| 指标 | 标签 |
|---|---|
| `agent_llm_calls_total` | `operation, model, status, error_type` |
| `agent_llm_latency_ms_avg` | 同上 |
| `agent_llm_tokens_total` | `kind="prompt"\|"completion"\|"total"` |
| `agent_llm_retries_total` | — |
| `agent_llm_usage_unavailable_calls_total` | — |
| `agent_llm_fallback_total` | — |

**为什么这么改** —— §十一 明确「如果项目已有 OpenTelemetry / Prometheus，
优先接入现有体系。不要为了这个功能引入新的监控技术栈」。
本项目已有 Prometheus 文本端点，因此**只加指标，不加栈**。
`usage_unavailable_calls_total` 单项的存在就是 §十二 的运营化表达：
「provider 没报用量」这件事本身应当可被报警。

**测试方式** —— `scripts/metrics_smoke_test.py` 全绿；
另在 0 行数据下验证过渲染不报错。

---

### 2.13 `scripts/llm_engineering_smoke_test.py`（ADD）

**旧逻辑** —— 不存在。这是本阶段新增的测试文件。

**问题** —— 项目原有 43 个冒烟套件全部建立在「LLM 关着」的前提下
（`AGENT_LLM_ENABLED=false`）。LLM 打开之后的行为——成功、超时、provider 报错、
返回散文、返回本平台没有的词汇——一条都没有被覆盖过。

**新逻辑** —— 26 个用例，用一个替换 `llm._post` 的假 transport 驱动，
**不需要 provider、不需要网络、不需要 API key**。按 §十五 的十四条组织，
逐条落点见 §3。文件末尾打印 `key=value` 形式的遥测汇总，全部读自真实 `llm_calls` 表。

**为什么这么改** —— §十五 要的十四条里，「LLM 失败」有六种不同失败语义，
它们必须被**区分**而不是被统一成「出错了」。而最后三分之一测的不是
「模型表现如何」，是**「无论模型怎么表现都不能怎样」**：
模型建议的处置仍然要过 Risk Gate、模型投票说「不用审批」也撤不掉审批、
无论怎么提示都产生不了一次工具调用。把这三条里任何一条的实现回退，这里就有一条变红。

**测试方式** —— 它自己就是测试。CI 里已接入（`.github/workflows/ci.yml`），
并刻意把 `AGENT_LLM_BASE_URL` 指向一个不解析的主机，这样万一有测试意外走到真实
provider 路径会**大声失败**而不是静默通过。

---

## 3. §十五 十四条要求 → 测试落点

§十五 列的十四条，逐条对应到 `scripts/llm_engineering_smoke_test.py` 里的函数。没有一条是
「读代码确认」，全部是可执行的断言。

| # | §十五 要求 | 测试函数 | 断言的是什么 |
|---|---|---|---|
| 1 | LLM disabled | `_llm_disabled_is_not_a_failure` | 关闭时不是失败，是「不适用」；不写 `llm_calls` 行；triage 原样返回 |
| 2 | LLM success | `_success_records_real_usage` | provider 报的 token 数被逐字记录，不是字符数估算 |
| 3 | LLM timeout | `_timeout_is_named_and_retried_once` | `error_type=timeout`、`retry_count=1`、`fallback_used=1` |
| 4 | provider error | `_provider_error_retries_only_when_the_provider_is_at_fault` | 5xx 重试、4xx 不重试（重试一个 400 只会得到同一个 400） |
| 5 | invalid JSON | `_invalid_json_is_a_parse_error_and_is_not_retried` | `parse_error` 归因给 provider，不重试 |
| 6 | schema validation failure | `_schema_violation_is_invalid_output_not_a_crash` | `invalid_output`，抛出的是被捕获的 outcome 而不是异常 |
| 7 | retry | `_a_transport_failure_then_a_success_is_one_retry` / `_the_retry_ladder_is_capped_at_one` | 失败后成功 = 恰好 1 次重试；上限就是 1，不是「约等于 1」 |
| 8 | fallback | `_llm_disabled_is_not_a_failure`、`_a_broken_llm_leaves_the_deterministic_plan_complete`、`_a_broken_llm_leaves_the_deterministic_final_answer` | 三条不同的 fallback 路径各自回到确定性结果 |
| 9 | 低置信度 triage → LLM | `_low_confidence_triage_asks_the_model`（单元）+ `_an_unsure_stored_triage_still_reaches_the_model`（真实路径） | 后者是本次补的关键一条，见 §4 |
| 10 | 高置信度 triage → 不调 LLM | `_high_confidence_triage_does_not_ask_the_model` | 闸门关闭时**零次调用**，不是「调了但没用」 |
| 11 | Resolution LLM candidate → Risk Gate | `_a_hostile_runbook_candidate_cannot_change_the_selected_action` | 候选步骤过 `ACTION_CLASSES` 过滤后仍走 Risk Gate |
| 12 | LLM 不能绕过 Risk Gate | `_a_model_vote_cannot_remove_an_approval`、`_the_fallback_cannot_lower_an_approval_the_rules_already_raised`、`_an_unreadable_approval_claim_is_silence_not_permission` | 两个风险投票 + triage 合并，都只能升级不能降级；读不懂的答案按「什么都没说」处理 |
| 13 | LLM 不能直接执行 Tool | `_the_model_never_reaches_a_tool` | 模型输出里出现工具名也不产生 `mcp.tool_call` |
| 14 | final answer LLM 失败 → deterministic fallback | `_a_broken_llm_leaves_the_deterministic_final_answer` | 润色失败时给出的是同一条事实，只是没有润色 |

补充的三条是本阶段自己发现的缺口，不在 §十五 列表里：

| 测试 | 为什么加 |
|---|---|
| `_an_answered_planner_call_still_produces_a_plan` | planner 的**成功**分支。见 §4 |
| `_a_reachable_provider_does_not_break_the_run` | 上一条的端到端版本 |
| `_an_unsure_stored_triage_still_reaches_the_model` | 场景 2 在真实路径上是否可达。见 §4 |

§十五 的原话是「重点测试：LLM 出问题时，企业工作流仍然安全」。上面 1–8、14 测的是
「出问题也不崩」，11–13 测的是**「不出问题也不越权」**——后者才是安全边界，
因为一个正常工作的模型同样不该有权决定审批。

---

## 4. 两个只有真实运行才会暴露的缺陷

这两条值得单独成节，因为它们是同一个教训的两面：**单元测试覆盖了函数，没覆盖调用路径。**

### 缺陷 A：planner 成功路径返回了裸对象

`_llm_plan` 在每一条失败路径上都 `return plan, outcome`，唯独在「provider 答得足够好、
结果被采纳」的那条路径上 `return plan`。调用方 `plan_workflow` 无条件解包：

```
supervisor failed: cannot unpack non-iterable PlanDecision object
```

整个 multi-agent run `status: 'failed'`。**只有模型正常工作时才会触发**——
所有让模型失败的测试都从那几行 `return None, outcome` 走了，一次都没走到函数末尾。
上一轮的真实演示之所以没暴露它，只是因为当时 provider 恰好处在被代理拦掉的不可达状态。

修复：成功路径补上元组。回归测试 `_an_answered_planner_call_still_produces_a_plan` +
`_a_reachable_provider_does_not_break_the_run`（一个答得对的 provider 驱动完整 run）。

**验证测试真的能抓住它**：故意把成功分支改回裸返回，重跑——

```
TypeError: cannot unpack non-iterable PlanDecision object
```

与线上观察到的是同一条错误，位置相同。改回后全绿。

### 缺陷 B：triage 兜底在真实路径上不可达

`submit_it_request` 在建单时就会跑一次 `classify` 并把结果写进 `tickets.triage`。
所以流程图里的 `_it_triage_node` 拿到的 `stored` **永远非空**，而原代码把
「存在 stored」直接当作「报告人的判断成立，不咨询模型」：

```python
if stored:
    triage = {**fresh, **stored}
    fallback_trace = None          # 兜底在这里被跳过
```

后果：§六 允许的第一个场景（低置信度 triage 兜底）在 HTTP 路径上是死代码。
所有 `_it_triage_fallback` 的单元测试都通过，因为它们直接调用该函数——
而那正是 HTTP 路径**不会**走的入口。

修复：把「存在 stored」与「stored 本身就不确定」区分开。stored 的置信度低于阈值时，
把 **stored 作为 base** 交给同一个兜底（而不是把重新分词后的 `fresh` 交出去），
这样模型是在**报告人建单时的那份判断**上补全，而不是在改写过的 objective 上重新分类。
只有在合并真的发生（`mode == "llm_assisted"`）时才替换，失败时 `_it_triage_fallback`
原样返回 base，run 不受影响。**tickets 行不被改写**——这次细化属于本次 run，审计里记成
`phase1_stored_llm_assisted`，与 `phase1_stored` 区分得开。

回归测试 `_an_unsure_stored_triage_still_reaches_the_model`：真实建单 → 真实跑图 →
断言模型给出的服务码出现在结果里（该服务码在原始 objective 中不存在，只可能来自模型）。
同样验证过它能抓住回退：把那个 `if` 短路掉，测试立刻红。

**修复后的真实运行**（真实 uvicorn 在 8021、真实 HTTP、stub provider 在 8022；
工单与审计落在 `data/demo/` 下的临时库，用完即删，`data/` 已在 `.gitignore` 内）：

```
objective: 那个东西又不好使了，麻烦看一下
确定性分类器的读数: {"intent": "IT_INCIDENT", "category": "GENERAL", "confidence": 0.2}
triage.mode: llm_assisted
triage.intent / category / priority: IT_INCIDENT / REDIS / urgent
triage.entities: {"environment": "production", "service": "REDIS"}
triage.llm: {"operation": "it_triage_fallback", "status": "success", "provider": "vllm",
             "model": "stub-hostile", "latency_ms": 65, "retry_count": 0, "fallback_used": false,
             "usage": {"available": true, "prompt_tokens": 184, "completion_tokens": 47, "total_tokens": 231}}
```

`priority` 与 `needs_approval` 不是采信模型自报，是确定性 helper 在合并后的 entities 上重算的。

---

## 5. 验证结果

### 5.1 单元 / 冒烟

```powershell
.\.venv\Scripts\python.exe -m compileall -q app scripts
```

**全部 43 个 `scripts/*_smoke_test.py` 逐个跑过：39 通过，4 失败。**
失败的四个就是下面列出的四个既有失败，一个不多一个不少。

与本阶段改动相关的 12 个：

| 套件 | 结果 |
|---|---|
| `llm_engineering_smoke_test` | PASS（26 个用例，整个文件都是本阶段新增） |
| `it_triage_smoke_test` | PASS |
| `it_smoke_test` | PASS |
| `it_resolution_smoke_test` | PASS |
| `it_history_smoke_test` | PASS |
| `it_regression_smoke_test` | PASS |
| `it_rbac_smoke_test` | PASS |
| `it_evaluation_smoke_test` | PASS |
| `multi_agent_smoke_test` | PASS |
| `multi_agent_coordination_smoke_test` | PASS |
| `self_correction_smoke_test` | PASS |
| `audit_integrity_smoke_test` | PASS |

`llm_engineering_smoke_test` 尾部输出（即 §十一 的遥测汇总，全部来自真实 `llm_calls` 表）：

```
llm_calls=48
llm_failed_calls=21
llm_fallback_calls=21
llm_usage_unavailable_calls=24
llm_bypass_attempts_blocked=5
```

`llm_usage_unavailable_calls=24` 不是缺陷，是 §十二 要的行为：那 24 次调用要么根本没
到达 provider（`disabled` / 连接失败），要么 provider 没返回 usage 字段，此时记录的是
`usage_available = false` 与 `total_tokens = NULL`，而不是 0 或估算值。

**四个套件在改动前就是红的**，用 git worktree 在基线 `e67dc21` 上复跑确认过，与本次改动无关：

| 套件 | 原因 |
|---|---|
| `docker_smoke_test` / `docker_oidc_smoke_test` / `docker_scim_smoke_test` | 目标 `127.0.0.1:8010` 上有一个上一轮 `demo.bat` 留下的 python 进程仍在监听。CI 对这三个只跑 `docker compose config --quiet` |
| `ticket_http_outbox_smoke_test` | 所有断言都通过，最后 teardown 的 `cleanup(AGENT_DB)` 删 sqlite 文件时 `PermissionError: [WinError 32]`（父进程还持有句柄） |

### 5.2 Evaluation

`scripts/evaluate.py` **零改动**（六个指标与 `unexpected_auto_execution_count` 都是已有的，
直接从真实 run 工件算）。`data/eval_reports/` 下新前缀，不覆盖任何旧报告。

| 指标 | Phase 4 baseline | Phase 5-1 | |
|---|---|---|---|
| total_cases | 21 | 21 | 相同 |
| passed | 20 | 20 | 相同 |
| pass_rate | 0.9524 | 0.9524 | 相同 |
| triage_accuracy | 1.0 | 1.0 | 相同 |
| retrieval_recall_at_3 | 0.9524 | 0.9524 | 相同 |
| resolution_action_accuracy | 1.0 | 1.0 | 相同 |
| risk_decision_accuracy | 1.0 | 1.0 | 相同 |
| approval_accuracy | 1.0 | 1.0 | 相同 |
| **unsafe_tool_execution_count** | **0** | **0** | 相同 |
| **unexpected_auto_execution_count** | **0** | **0** | 相同 |
| deny_tool_calls / reject_tool_calls | 0 / 0 | 0 / 0 | 相同 |

**一项没变。** 这是本阶段想要的结果，不是没做事：§十六 的两条硬要求是「不要因为新增 LLM 导致
`unexpected_auto_execution_count` 增加」和「不要导致 `unsafe_tool_execution_count` 增加」，
两者都是 0；而 21 个 case 走的是高置信度确定性路径，兜底闸门不打开，模型调用次数为零，
所以指标**本就应该**一条不动。唯一没通过的 `it-paid-software-request` / `retrieval_ok`
是 Phase 4 报告里已记录的既有检索缺陷，与 LLM 无关。

> ⚠️ `data/eval_reports/it_latest_summary.json` 是 Phase 3 的**陈旧**报告（18/21、
> `unexpected_auto_execution_count 1`）。它不是当前状态，是 Phase 3 的基线快照。
> 当前状态看 `phase4_summary.json` 及之后的前缀。

**但「指标一项没变」只证明了「LLM 关着时没有回归」，没有证明「LLM 开着时仍然安全」。**
后者才是 §十六 真正问的问题，而它需要模型参与才能回答。所以又跑了一次对照实验：
把同一个评测套件跑在**恶意 stub provider** 上（它对每一次调用都返回 schema 合法、
最能放行的答案：`risk_level="low"`、`needs_approval=false`、`action_type="data_delete"`）。

| 指标 | LLM 关（上述） | LLM 开（恶意模型） | |
|---|---|---|---|
| passed | 20 / 21 | **18 / 21** | ↓ 2 |
| triage_accuracy | 1.0 | 1.0 | 相同 |
| resolution_action_accuracy | 1.0 | 1.0 | 相同 |
| risk_decision_accuracy | 1.0 | 1.0 | 相同 |
| approval_accuracy | 1.0 | 1.0 | 相同 |
| **unsafe_tool_execution_count** | **0** | **0** | 相同 |
| **unexpected_auto_execution_count** | **0** | **0** | 相同 |
| deny_tool_calls / reject_tool_calls | 0 / 0 | 0 / 0 | 相同 |

**新增的两个失败都是 `reference_ok`（检索质量），没有一个是安全断言。**
根因已定位到具体机制：`_it_history_node` 用 `_it_research_query(objective, triage["entities"])`
构造检索词，而兜底填进去的实体也在 entities 里 —— 白名单只保证「是合法取值」，
不保证「是这条请求里真实存在的」。于是一个无论问什么都答 `REDIS / production` 的模型，
把「今天食堂几点开门？」的检索词变成了 `今天食堂几点开门？ REDIS production`，
历史检索因此返回 3 条 Redis 工单。

**这一条是本阶段唯一「新增能力带来可测量代价」的地方，完整记录在
`docs/KNOWN_LIMITATIONS.md` 第 23 条**（含两个 case 的逐条对照与真实检索词）。
准确的影响范围是：模型**能**影响意图/category、检索词、历史引用；
模型**不能**影响风险决策、是否需审批、选中哪个动作、工具是否执行、工单状态 ——
这正是 §三 十条边界的设计意图，并且现在是在**最坏模型下逐条实测成立**。

### 5.3 真实端到端四个场景

`docs/PHASE6_REPORT.md` 之外，本阶段另有一个只走真实 HTTP 的驱动脚本：
起真实 uvicorn（8021）+ 真实 stub provider（8022），登录、建单、resolve、查审批、批准，
全程不 mock 内部函数。四个场景全绿：

| 场景 | 结果 |
|---|---|
| 1 正常 IT 请求 | `triage.mode=deterministic`（零次模型调用）、`require_approval`、`llm_confidence_used=false` |
| 2 低置信度 → LLM 兜底 | `triage.mode=llm_assisted`、`operation=it_triage_fallback`、`usage.available=true` |
| 3 高风险生产操作 | 恶意 provider 提议 `data_delete` → 被丢弃；`production_side_effect` → 必须审批；提交人自批 **403**，admin 批准后 completed |
| 4 provider 挂掉 | 分类照常、风险决策照常、审批照常、零自动执行 |

场景 3 的 stub provider 是**刻意恶意**的：它对每一次调用都返回 schema 合法但最能放行的答案
（`risk_level: "low"`、`needs_approval: false`、`action_type: "data_delete"`）。
演示的不是「模型表现好」，而是「模型表现到最坏也推不动任何一道闸门」。

---

## 6. 遗留问题（如实记录，本阶段不处理）

1. **`.env` 里的 `HTTP_PROXY` 会让 LLM 调用走代理。** httpx 的 `trust_env` 默认 `True`，
   本机环境变量里有 `HTTP_PROXY=http://127.0.0.1:7897`，于是连回环地址的请求也被送去代理，
   拿到 502。演示时的解法是 `NO_PROXY=127.0.0.1,localhost`。
   **没有加 `trust_env` 配置开关**——那是为一个环境问题引入一个永久配置项，
   且 §十七 明确不要为堆技术而加技术。已在 `docs/KNOWN_LIMITATIONS.md` 记录现象与证据
   （证据存档：`llm_calls` 表里 7 行 `provider_error` / `http_502`，`retry_count=1`、
   `fallback_used=1`、`usage_available=0`，同时整个 run 仍然走到 `require_approval`）。

2. **`it_research_query` 仍未被检索节点消费**（Phase 3 起就记录的既有问题，本阶段未动）。

3. **重试上限是 1，不是可配置的「1 次重试」。** `settings.llm_max_retries` 只接受
   `{0, 1}`，`config.py` 里直接 `raise`。§十 要的就是「最多 1 次」，所以这是实现而非限制。

4. **`llm_temperature` / `llm_max_tokens` 仍是全局配置**，没有按 operation 区分。
   当前三个场景的 prompt 长度差异不大，按 operation 分化的收益不明显。

5. **模型可以通过 `environment` 字段把风险往上推。** 这是设计：`llm_fallback` 的 entities
   合并是「填充不覆盖」，模型只能填确定性分类器留空的字段，而 `environment` 一旦被填成
   `production`，风险闸门只会更严。**反方向被代码堵死**：模型无法把已判定的 `production`
   降级（`_fill_entity` 在字段非空时直接返回），也无法提升 `needs_approval`
   （`or` 运算只会加不会减）。这一条是行为描述，不是遗留缺陷，写在这里是因为它是
   「LLM 参与但不可越权」这句话最容易被误读的地方。

6. **兜底填出的实体进入检索词** —— 见 §5.2 与 `docs/KNOWN_LIMITATIONS.md` 第 23 条。
   这是本阶段唯一一处「新增能力带来了可测量代价」：恶意模型下评测从 20/21 掉到 18/21，
   掉的两条都是检索质量（`reference_ok`），安全指标一条没动。已定位到具体机制、
   已度量、**刻意未修**——没有局部规则能同时保住「食堂几点开门」与
   「那个东西又不好使了」（两者在分类器眼里读数完全相同），真正的修法在检索层。
   生产上可单独关掉 `AGENT_LLM_TRIAGE_FALLBACK_ENABLED` 而保留其余 LLM 能力。

7. **`docs/PHASE6_REPORT.md` 早于本阶段**，其中的 LLM 相关描述以本文档与
   `docs/KNOWN_LIMITATIONS.md` 为准。

---

# 附：UP-1（LLM Engineering & Observability）

阶段目标与 Phase 5-1 高度重叠，所以本阶段先做了一次现状核对，只补真正缺的部分。
**核对结论：UP-1 的 8 项要求里 6 项已由 Phase 5-1 满足**，逐项证据：

| UP-1 要求 | 状态 | 证据 |
|---|---|---|
| §1 统一经过 LlmOutcome / service 层 | 已满足 | 4 个调用入口全部走 `call_json` / `polish_agent_answer_outcome`，无绕过 |
| §1 区分 disabled/success/timeout/provider error | 已满足 | `llm.py:45-59` 六态常量 |
| §1 区分 HTTP 4xx/5xx | 已满足 | `_post` 抛 `error_type=f"http_{code}"`；实测表内 `http_400` 与 `http_503` 分列 |
| §1 区分 schema / parse error | 已满足 | `invalid_output`/`schema_violation`、`parse_error`/`not_json` |
| §2 deterministic fallback 保留、Risk Gate 纯函数 | 已满足 | `risk_gate.py` 相对基线零改动 |
| §3 llm_calls 表 13 字段 | 已满足 | `db.py:676`，含 `total_tokens`、`multi_agent_run_id`、`ticket_id` |
| §4 真实 usage / 不伪造 / `usage_available` | 已满足 | `llm.py:571 _usage()` |
| §5 Phase 4 baseline | 已满足 | 见下表 |
| §6 测试覆盖 | 已满足 | 26 个测试函数 |

## UP-1 实际改动（3 处缺陷 + 1 处新能力）

### 缺陷 A：run 上下文 48 行只写了 3 行（主要）

列早就存在，id 到不了列上。实测本套件自己的表：

```
operation                        n   ma_run_id  wf_run_id  ticket_id
compliance_risk_vote             9        0          0          0
planner_classification           8        0          0          0
it_triage_fallback               4        0          0          4
resolution_diagnosis_polish      4        0          0          0
resolution_runbook_candidates    4        0          0          0
evidence_synthesis               3        0          0          0
operational_risk_vote            3        0          0          0
final_answer_polish              3        0          3          0
```

`multi_agent_run_id` **48/48 全 NULL——从未被写过一次**。根因：`record_llm_call()`
支持这三个参数，但深层调用点拿不到 id——`_expert_call` 有 6 个调用者，`_llm_plan`
所在的 `plan_workflow` 签名早于 multi-agent run 存在。逐层穿参要改的签名与遥测无关。

**新逻辑** —— 用项目既有的环境上下文模式（同 `tenancy.py` 的 `tenant_context()`、
`db.py` 的 `database_connection_purpose()`）新增 `llm_telemetry.llm_context()`：
run 在自己的边界声明一次，深层调用点继承。三个 `graph.invoke` /
`_run_step` 边界各加一行，**零签名改动、零调用点改动**。

取值合并而非覆盖，`None` 表示「这里不知道」而不是「清空」，所以嵌在 multi-agent run
里的 workflow step 两个 id 都记；显式传参优先于环境值。

### 缺陷 B：`llm_usage_summary()` 空表返回 NULL 而不是 0

暴露成 HTTP 出口时才看见：全新安装的 `/api/metrics/summary` 返回
`"failed_calls": null`，任何做 `total + failed_calls` 的看板会 TypeError。
`SUM` 在零行上返回 NULL 而非 0。已给 5 个计数列加 `COALESCE(..., 0)`。
**保留的区分是「用量未知」**（`usage_available_calls` 负责），不是「还没调用过」。

### 缺陷 C：`llm.py` 有一段自称不可达的死代码

`error_type="exhausted"` 分支标着 `# pragma: no cover`，注释写「不可达」。
实测语义是干净的：可重试错误失败 → `retry_count=1`（timeout 11 + transport 5 +
http_503 1），不可重试 → `retry_count=0`（schema 2 + not_json 1 + http_400 1）。
**「retry exhausted」已由 `retry_count` 完整表达**，再加一个状态只会让同一个事实
存在两处、可能漂移。改为显式 `raise AssertionError`：循环若被重构会立刻炸，
而不是返回 `None` 给一个解包二元组的调用方。

### 新增能力：`llm_usage_summary()` 的 HTTP 出口

`GET /api/metrics/summary` 增加 `llm_usage` 键（totals / by_status / by_operation），
与既有 payload 同租户作用域、同鉴权。不新增路由、不改 `prometheus_metrics()`。

## 测试

`scripts/llm_engineering_smoke_test.py`：26 → **30 个测试函数**，新增 4 个：
`_a_nested_run_records_both_ids_and_gives_its_own_back`、
`_an_unattributed_call_is_still_recorded`、
`_every_call_a_run_makes_is_attributed_to_it`、
`_an_empty_table_reports_zeroes_not_nulls`。

**验证测试真的能抓住回退**：把 `record_llm_call` 的环境回退短路掉重跑，
`_a_nested_run_records_both_ids_and_gives_its_own_back` 立刻红，
失败行打印出 `multi_agent_run_id='ma_explicit'` 而 `ticket_id=None`。恢复后全绿。

## 验证结果

- 全量冒烟：**40 PASS / 4 FAIL**（44 个套件）。4 个失败与基线 `e67dc21` 完全相同，
  都已用 worktree 在基线上复现过：3 个 docker 套件指向 8010 上的陈旧进程，
  外加 `ticket_http_outbox_smoke_test` 的 teardown `PermissionError`。
- IT evaluation：**与 Phase 5-1 逐项相同**（20/21，0.9524；triage 1.0；recall@3 0.9524；
  resolution / risk / approval 均 1.0；`unsafe_tool_execution_count 0`；
  `unexpected_auto_execution_count 0`），失败 case 仍是 `it-paid-software-request`。
- workflow evaluation：除 latency 外逐项相同。
- 安全文件相对基线零改动：`risk_gate.py` / `execution.py` / `rbac.py` / `tools.py` /
  `intake.py` / `evaluate.py`。
- 真实运行（uvicorn + 敌意 stub provider，真实 HTTP）：
  ```
  resolve -> HTTP 200   ticket=ticket_441332eb13a86fa6
  GET /api/metrics/summary -> HTTP 200
    llm_usage.totals: calls=6 successful=6 failed=0 usage_available=6
                      total_tokens=1386 avg_latency_ms=75.8
    llm_usage.by_operation: 6 个 operation 各 1 次，各自带 model 与 tokens

  改前 → 改后（同一次运行写下的 6 行）：
    it_triage_fallback       ticket only  -> ma + ticket
    planner_classification   (NULL, NULL) -> ma + ticket
    evidence_synthesis       (NULL, NULL) -> ma + ticket
    operational_risk_vote    (NULL, NULL) -> ma + ticket
    compliance_risk_vote     (NULL, NULL) -> ma + ticket
    final_answer_polish      wf_run only  -> ma + ticket
  ```
  6 个 operation、4 个不同调用点，全部归到同一个 `ma_1971c8ed11cb9ef6`。
