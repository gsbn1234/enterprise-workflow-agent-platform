// Phase 6 showcase.
//
// This page runs the real platform. Every scenario below drives the same HTTP
// chain the IT panel drives -- login, submit, resolve, read the chain, decide
// the approval -- and every value it renders comes out of a response body. It
// computes no outcomes locally and has no success state of its own: if a call
// fails, the failure is what gets shown.
//
// The scenario list is not defined here either. It is projected by the backend
// from the committed evaluation suite (see app/services/it/demo_scenarios.py),
// so a scenario's stated expectation and the harness's expectation are one
// value rather than two that can drift.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Check,
  FlaskConical,
  ListChecks,
  LogIn,
  Play,
  SearchCheck,
  ShieldCheck,
  Wrench,
} from "lucide-react";

import { api, login as loginApi } from "./api.js";
import { ITChain } from "./ITChain.jsx";
import { Card, IconButton, PanelHeader, Status } from "./ui.jsx";

const CLASS_LABEL = {
  auto_execute: "A · 只读 / 低风险，可自动执行",
  approval_approved: "B · 生产 + 关键资产，必须人工审批",
  approval_rejected: "C · 待审批后由人拒绝",
  risk_deny: "风险门禁拒绝（已录制，不可现场运行）",
};

const FILTERS = [
  ["all", "全部"],
  ["runnable", "可现场运行"],
  ["auto_execute", "A 自动执行"],
  ["approval_approved", "B 审批后执行"],
  ["approval_rejected", "C 审批后拒绝"],
  ["smoke", "CI 冒烟子集"],
];

const DEMO_ACCOUNTS = [
  ["admin", "AdminPass123", "admin · 可审批"],
  ["manager", "ManagerPass123", "manager · 可审批"],
  ["alice", "AlicePass123", "employee · 不可审批（RBAC）"],
];

function capabilityIcon(id) {
  if (id === "triage") return ListChecks;
  if (id === "retrieval") return SearchCheck;
  if (id === "risk_gate") return ShieldCheck;
  if (id === "approval") return Check;
  if (id === "tool_execution") return Wrench;
  if (id === "audit") return Activity;
  return FlaskConical;
}

function formatMetric(metric) {
  if (metric.unit === "percent") return `${(Number(metric.value) * 100).toFixed(2)}%`;
  return String(metric.value);
}

export function ShowcasePage({ user, canDecide, onLoggedIn, onMessage }) {
  const [data, setData] = useState(null);
  const [health, setHealth] = useState(null);
  const [loadError, setLoadError] = useState("");
  const [evalReports, setEvalReports] = useState(null);
  const [evalError, setEvalError] = useState("");

  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");

  const [kbQuery, setKbQuery] = useState("");
  const [kbResults, setKbResults] = useState(null);
  const [kbError, setKbError] = useState("");
  const [kbBusy, setKbBusy] = useState(false);

  const [busy, setBusy] = useState("");
  const [trace, setTrace] = useState([]);
  const [note, setNote] = useState(null);
  const [ticketId, setTicketId] = useState("");
  const [chain, setChain] = useState(null);
  const [activeScenario, setActiveScenario] = useState(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const payload = await api("/api/it/demo-scenarios");
        if (!cancelled) setData(payload);
      } catch (err) {
        if (!cancelled) setLoadError(err.message || "无法读取 Demo 场景");
      }
      try {
        const status = await api("/api/health");
        if (!cancelled) setHealth(status);
      } catch (err) {
        // Health is decoration here; a failure to read it must not blank the page.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Separate effect, keyed on the session: ``/api/eval-reports`` is behind a
  // token, so the first read on an anonymous page is expected to 401. Re-running
  // it after login is what turns that 401 into the real answer instead of
  // leaving a stale failure on screen.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const reports = await api("/api/eval-reports?limit=5");
        if (cancelled) return;
        setEvalReports(Array.isArray(reports) ? reports : []);
        setEvalError("");
      } catch (err) {
        if (cancelled) return;
        setEvalReports(null);
        setEvalError(
          err.status === 401
            ? "该接口需要登录（HTTP 401）：在上面登录任一演示账号后会自动重新读取。"
            : err.status === 403
              ? "该接口需要 admin 角色（HTTP 403）：换 admin 演示账号登录后会自动重新读取。"
              : err.message || "无法读取评测记录",
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [user]);

  const scenarios = data?.scenarios || [];
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return scenarios.filter((scenario) => {
      const matchesFilter =
        filter === "all"
          ? true
          : filter === "runnable"
            ? scenario.runnable
            : filter === "smoke"
              ? scenario.smoke_subset
              : scenario.demo_class === filter;
      if (!matchesFilter) return false;
      if (!needle) return true;
      return (
        scenario.objective.toLowerCase().includes(needle) ||
        scenario.id.toLowerCase().includes(needle) ||
        String(scenario.category || "").toLowerCase().includes(needle)
      );
    });
  }, [scenarios, filter, query]);

  const record = useCallback((call, result) => {
    setTrace((prev) => [...prev, { call, result }]);
  }, []);

  const loadChain = useCallback(async (id) => {
    const next = await api(`/api/it/requests/${id}/chain`);
    setChain(next);
    return next;
  }, []);

  const handleAuthError = useCallback(
    (err) => {
      if (err.status === 401) {
        setNote({ status: "failed", detail: "该接口需要登录：请先在上方用演示账号登录，再重新运行。" });
        return true;
      }
      return false;
    },
    [],
  );

  const runScenario = async (scenario) => {
    if (busy) return;
    setBusy("run");
    setNote(null);
    setTrace([]);
    setChain(null);
    setTicketId("");
    setActiveScenario(scenario);
    // Point the retrieval section at the text that was actually submitted, so
    // the knowledge a reader sees there is the knowledge this run retrieved.
    setKbQuery(scenario.objective);
    setKbResults(null);
    setKbError("");
    try {
      if (!scenario.runnable) {
        setNote({ status: "skipped", detail: scenario.not_runnable_reason });
        return;
      }
      const intake = await api("/api/it/requests", {
        method: "POST",
        body: JSON.stringify({ objective: scenario.objective }),
      });
      record("POST /api/it/requests", `201 · ticket_id=${intake.ticket_id}`);
      setTicketId(intake.ticket_id);

      // Tickets are idempotent: the platform keys them on title + description +
      // requester, so asking the same question twice returns the *same* ticket
      // rather than filing a second one. That is a real property of the platform
      // and worth showing, but it means re-running a scenario as the same
      // account lands on an already-handled ticket, which resolve refuses. Say
      // so, instead of printing a bare 409 and leaving the reader to guess.
      let resolved;
      try {
        resolved = await api(`/api/it/requests/${intake.ticket_id}/resolve`, {
          method: "POST",
          body: JSON.stringify({}),
        });
      } catch (err) {
        if (err.status === 409) {
          record(`POST /api/it/requests/${intake.ticket_id}/resolve`, `HTTP 409 · ${err.message}`);
          await loadChain(intake.ticket_id);
          setNote({
            status: "skipped",
            detail:
              "这张工单是复用来的：同一个账号对同一句话再次提交，平台按幂等键返回上次那张工单，" +
              "而它已经处理完了，所以不会重复执行。换一个场景，或换一个演示账号再运行。",
          });
          return;
        }
        throw err;
      }
      record(`POST /api/it/requests/${intake.ticket_id}/resolve`, `status=${resolved.status}`);

      const next = await loadChain(intake.ticket_id);
      record(`GET /api/it/requests/${intake.ticket_id}/chain`, `decision=${next?.risk_decision?.decision || "-"} · ticket=${next?.ticket?.status || "-"}`);

      const approval = next?.approval;
      if (approval && approval.status === "pending") {
        // Actually make this call rather than writing the line and hoping: the
        // trace is a claim about what ran, so every row in it has to have run.
        // It doubles as the honest answer to "is there anything for me to
        // decide?" -- an employee gets a 403 here, which is RBAC observed
        // rather than RBAC asserted.
        try {
          const pending = await api("/api/approvals?status=pending");
          const queued = (pending || []).some((item) => item.id === approval.id);
          record(
            "GET /api/approvals?status=pending",
            queued ? `待审批队列含 ${approval.id}` : `待审批队列未含 ${approval.id}`,
          );
        } catch (err) {
          record("GET /api/approvals?status=pending", `HTTP ${err.status || "?"} · ${err.message}`);
        }
      }
      setNote({
        status: resolved.status,
        detail:
          resolved.status === "waiting_approval"
            ? "已停在人工审批：这是设计上的停顿，不是失败。请在下方由人决定批准或拒绝。"
            : resolved.status === "cancelled"
              ? "风险门禁拒绝了本次动作，未执行任何工具。"
              : "链路已跑完。",
      });
    } catch (err) {
      if (!handleAuthError(err)) {
        setNote({ status: "failed", detail: err.message || "运行失败" });
      }
      onMessage?.(err.message || "运行失败", "error");
    } finally {
      setBusy("");
    }
  };

  const decide = async (approvalId, approved) => {
    if (busy) return;
    setBusy(`decide:${approvalId}`);
    try {
      const result = await api(`/api/approvals/${approvalId}/decide`, {
        method: "POST",
        body: JSON.stringify({
          approved,
          decided_by: user?.id,
          reason: approved ? "Showcase 批准执行" : "Showcase 拒绝执行",
        }),
      });
      record(`POST /api/approvals/${approvalId}/decide`, `approved=${approved} · run=${result?.status || "-"}`);
      if (ticketId) {
        const next = await loadChain(ticketId);
        record(`GET /api/it/requests/${ticketId}/chain`, `终态 ticket=${next?.ticket?.status || "-"} · executed=${String(Boolean(next?.execution?.executed))}`);
      }
      setNote({
        status: approved ? "approved" : "denied",
        detail: approved ? "审批通过，动作已执行。" : "审批被拒绝，动作未执行。",
      });
      onMessage?.(approved ? "审批已通过" : "审批已拒绝");
    } catch (err) {
      if (!handleAuthError(err)) {
        setNote({ status: "failed", detail: err.message || "审批失败" });
      }
    } finally {
      setBusy("");
    }
  };

  // Capability ② on its own, so the retrieval channel can be shown without
  // running a whole ticket. This endpoint is anonymous, and it returns the one
  // field the chain's copy of the evidence does not carry: ``category``.
  const searchKnowledge = async (rawQuery) => {
    const q = String(rawQuery ?? kbQuery).trim();
    if (!q || kbBusy) return;
    setKbBusy(true);
    setKbError("");
    try {
      const payload = await api(`/api/knowledge/search?q=${encodeURIComponent(q)}&limit=3`);
      setKbResults(Array.isArray(payload?.results) ? payload.results : []);
    } catch (err) {
      setKbResults(null);
      setKbError(err.message || "检索失败");
    } finally {
      setKbBusy(false);
    }
  };

  const doLogin = async (userId, password) => {
    try {
      const nextUser = await loginApi(userId, password);
      onLoggedIn?.(nextUser);
      onMessage?.(`已登录 ${nextUser.id} · ${nextUser.role}`);
    } catch (err) {
      setNote({ status: "failed", detail: err.message || "登录失败" });
    }
  };

  const toolMode = health?.tool_mode || "unknown";
  const history = data?.evaluation_history;
  // Read straight off the chain, so the counts in the prose are the counts that
  // came back rather than ones this page added up itself.
  const knowledgeCount = (chain?.resolution?.evidence || []).length;
  const historyCount = (chain?.resolution?.historical_evidence || []).length;

  return (
    <main className="showcase">
      <section className="showcase-hero">
        <h1>Enterprise IT Service Agent · Showcase</h1>
        <p>
          下面每一个场景都是<strong>真实调用</strong>平台 HTTP 接口跑出来的，页面不预置任何成功状态。
          场景清单由后端从已提交的评测集投影，因此这里的「期望」与评测 harness 的「期望」是同一个值。
        </p>
        <div className="showcase-meta">
          {data?.source ? (
            <span className="it-badge muted">
              场景来源 {data.source} · {data.count} 条（可现场运行 {data.runnable_count} 条 · CI 冒烟子集 {data.smoke_count} 条）
            </span>
          ) : null}
          <span className={`it-badge ${toolMode === "mock" ? "warn" : "muted"}`}>tool_mode={toolMode}</span>
          <span className="it-badge muted">rag={health?.rag || "-"}</span>
          <span className={`it-badge ${health?.auth_required ? "muted" : "warn"}`}>
            auth_required={String(Boolean(health?.auth_required))}
          </span>
        </div>
        {loadError ? <div className="it-note failed">{loadError}</div> : null}
      </section>

      <section className="showcase-session">
        <Card>
          <PanelHeader icon={LogIn} title="会话" kicker={user ? `${user.id} · ${user.role}` : "未登录"} />
          {user ? (
            <p className="muted">
              已登录 <code>{user.id}</code>（{user.role}）。
              {canDecide
                ? " 该角色可以审批。"
                : " 该角色不可审批：审批按钮会禁用，服务端也会独立拒绝（403）—— 禁用界面不是安全边界，服务端的拒绝才是。"}
            </p>
          ) : (
            <>
              <p className="muted">
                8 个 IT 接口里 6 个要求 token，匿名调用会返回 401；
                <code>GET /api/it/triage/preview</code> 与 <code>GET /api/it/demo-scenarios</code> 是匿名的
                （后者正是本页加载场景列表用的那个）。请选择一个演示账号登录后再运行场景。
              </p>
              <div className="showcase-accounts">
                {DEMO_ACCOUNTS.map(([id, password, label]) => (
                  <button className="scenario-button" type="button" key={id} onClick={() => doLogin(id, password)}>
                    <LogIn size={16} />
                    <span>{id}</span>
                    <small>{label}</small>
                  </button>
                ))}
              </div>
            </>
          )}
        </Card>
      </section>

      <section className="showcase-section">
        <h2>差异化能力</h2>
        <div className="showcase-highlights">
          {(data?.highlights || []).map((item) => (
            <div className="showcase-highlight" key={item.title}>
              <strong>{item.title}</strong>
              <p>{item.detail}</p>
              <code>{item.where}</code>
            </div>
          ))}
        </div>
      </section>

      <section className="showcase-section">
        <h2>功能分类</h2>
        <div className="showcase-capabilities">
          {(data?.capabilities || []).map((capability) => {
            const Icon = capabilityIcon(capability.id);
            return (
              <article className="showcase-capability" key={capability.id}>
                <header>
                  <Icon size={16} />
                  <strong>{capability.title}</strong>
                  {capability.mocked ? <span className="it-badge warn">Mock</span> : null}
                </header>
                <p>{capability.what}</p>
                <p className="muted">{capability.how}</p>
                <div className="showcase-api">
                  {capability.backend.map((entry) => (
                    <code key={entry}>{entry}</code>
                  ))}
                </div>
                <code className="showcase-module">{capability.module}</code>
                <span className="muted">Demo 中出现在：{capability.shown_as}</span>
              </article>
            );
          })}
        </div>
      </section>

      <section className="showcase-section">
        <h2>知识检索（实时）</h2>
        <p className="muted">
          本地策略库 · 确定性关键词检索 —— <strong>不是向量 RAG</strong>。每次查询都真的调用{" "}
          <code>GET /api/knowledge/search</code>，下面每行的 article_id / title / category / score / source /
          snippet 都是响应原文，页面不做加工。这一路（正式知识）是执行依据；历史工单是另一条独立通道，只作参考。
        </p>
        <p className="muted">
          该接口在 <code>auth_required=true</code> 下需要登录（<code>_optional_auth_context</code> 会按
          <code>settings.auth_required</code> 收紧），未登录时会如实返回 401，页面不代为隐藏。
        </p>
        <div className="showcase-search-row">
          <input
            className="showcase-search"
            value={kbQuery}
            onChange={(event) => setKbQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") searchKnowledge();
            }}
            placeholder="例如：Redis 生产故障"
          />
          <IconButton
            icon={SearchCheck}
            label={kbBusy ? "检索中" : "检索"}
            disabled={kbBusy || !kbQuery.trim()}
            onClick={() => searchKnowledge()}
          />
        </div>
        {kbError ? <div className="it-note failed">{kbError}</div> : null}
        {kbResults === null ? (
          <p className="muted">尚未检索。运行场景时这里会自动填入该场景的原始描述，点「检索」即可。</p>
        ) : kbResults.length ? (
          <ul className="it-evidence knowledge">
            {kbResults.map((item) => (
              <li key={item.article_id}>
                <strong>{item.title}</strong>
                <span className="muted">
                  <code>{item.article_id}</code> · category={item.category || "-"} · score={item.score ?? "-"} ·
                  source={item.source || "-"}
                </span>
                <p>{item.snippet}</p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">没有命中任何策略条目。</p>
        )}
      </section>

      <section className="showcase-section">
        <h2>一键场景</h2>
        <p className="muted">
          点「运行」= 提交工单 + 跑完整条 Agent 链路 + 读回真实链路。若停在人工审批，需要你再点一次批准或拒绝 ——
          这一停就是 Human-in-the-loop，不自动批准。
        </p>
        <p className="muted">
          工单是幂等的：同一个账号对同一句话再次提交，会拿到上次那张工单而不是新开一张。因此想完整重跑同一场景时，
          换一个演示账号即可。
        </p>
        <div className="showcase-filters">
          {FILTERS.map(([id, label]) => (
            <button
              className={`filter-chip ${filter === id ? "active" : ""}`}
              type="button"
              key={id}
              onClick={() => setFilter(id)}
            >
              {label}
            </button>
          ))}
          <input
            className="showcase-search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索场景 / 分类 / id"
          />
        </div>

        <div className="showcase-scenarios">
          {filtered.map((scenario) => (
            <article
              className={`showcase-scenario ${activeScenario?.id === scenario.id ? "active" : ""}`}
              key={scenario.id}
            >
              <header>
                <strong>{scenario.objective}</strong>
                {scenario.smoke_subset ? <span className="it-badge muted">CI 冒烟子集</span> : null}
                {!scenario.runnable ? <span className="it-badge warn">不可现场运行</span> : null}
              </header>
              <p className="muted">{CLASS_LABEL[scenario.demo_class] || scenario.demo_class}</p>
              <dl className="showcase-expect">
                <div>
                  <dt>期望分流</dt>
                  <dd>
                    {scenario.intent} / {scenario.category}
                  </dd>
                </div>
                <div>
                  <dt>期望动作</dt>
                  <dd>{scenario.expected_action}</dd>
                </div>
                <div>
                  <dt>期望风险</dt>
                  <dd>{scenario.expected_risk}</dd>
                </div>
                <div>
                  <dt>审批 / 执行</dt>
                  <dd>
                    {String(scenario.expected_approval)} / {String(scenario.expect_executed)}
                  </dd>
                </div>
              </dl>
              <div className="showcase-api">
                {scenario.backend_entry.map((entry) => (
                  <code key={entry}>{entry}</code>
                ))}
              </div>
              <IconButton
                icon={scenario.runnable ? Play : AlertTriangle}
                label={busy === "run" && activeScenario?.id === scenario.id ? "运行中" : "运行"}
                disabled={Boolean(busy) || !scenario.runnable}
                onClick={() => runScenario(scenario)}
              />
            </article>
          ))}
          {!filtered.length ? <p className="muted">没有匹配的场景。</p> : null}
        </div>
      </section>

      {(trace.length || note || chain) && (
        <section className="showcase-section">
          <h2>运行结果</h2>
          {activeScenario ? (
            <p className="muted">
              场景 <code>{activeScenario.id}</code> · {activeScenario.objective}
            </p>
          ) : null}
          {note ? (
            <div className={`it-note ${note.status}`}>
              <Status value={note.status} />
              <span>{note.detail}</span>
            </div>
          ) : null}
          {trace.length ? (
            <ol className="showcase-trace">
              {trace.map((item, index) => (
                <li key={`${item.call}-${index}`}>
                  <code>{item.call}</code>
                  <span className="muted">{item.result}</span>
                </li>
              ))}
            </ol>
          ) : null}
          {chain ? (
            <ITChain
              chain={chain}
              canDecide={Boolean(canDecide)}
              role={user?.role || "-"}
              busy={busy}
              onDecide={decide}
              onRefresh={() => ticketId && loadChain(ticketId)}
            />
          ) : null}
          {chain ? (
            <div className="showcase-provenance">
              <p className="muted">
                检索出处：正式知识 <code>{knowledgeCount} 条 · source=local_policy_db</code> · 历史工单{" "}
                <code>{historyCount} 条 · source=historical_ticket_index</code> · 决议{" "}
                <code>mode={chain.resolution?.mode || "-"}</code>
              </p>
              <p className="muted">
                正式知识（政策 / Runbook）是执行依据；历史工单只是过去的处理经验，仅作参考，不构成政策，也不会被用来选择动作。
              </p>
              <p className="muted">
                注意：工具执行一节显示的是 <strong>Mock 工具</strong>被调用，表示平台记录了这次操作，不代表任何真实基础设施被改动。
                这里不会出现「已重启生产 Redis」这类说法，因为没有任何真实基础设施被操作过。
              </p>
            </div>
          ) : null}
        </section>
      )}

      <section className="showcase-section">
        <h2>评测</h2>
        <article className="showcase-eval">
          <header>
            <FlaskConical size={16} />
            <strong>{history?.label || "Phase 4 Evaluation Result · 历史结果，非实时"}</strong>
          </header>
          <p className="muted">{history?.disclaimer}</p>
          <div className="showcase-metrics">
            {(history?.metrics || []).map((metric) => (
              <div className="showcase-metric" key={metric.key}>
                <span>{metric.label}</span>
                <strong>{formatMetric(metric)}</strong>
                <code>{metric.key}</code>
              </div>
            ))}
          </div>
          {history?.headline ? <p>{history.headline}</p> : null}
          {history?.unchanged ? <p className="muted">{history.unchanged}</p> : null}
          <dl className="showcase-eval-meta">
            <div>
              <dt>记录时间</dt>
              <dd>{history?.recorded_at || "-"}</dd>
            </div>
            <div>
              <dt>记录 id</dt>
              <dd>{history?.record_id || "-"}</dd>
            </div>
            <div>
              <dt>来源文档</dt>
              <dd>{history?.source_doc || "-"}</dd>
            </div>
            <div>
              <dt>产物</dt>
              <dd>{history?.artifact || "-"}</dd>
            </div>
          </dl>
          <p className="muted">{history?.artifact_note}</p>
          <p>
            复现命令：<code>{history?.reproduce}</code>
          </p>
          <div className="showcase-live-eval">
            <strong>当下 GET /api/eval-reports 的真实返回</strong>
            {evalError ? (
              <p className="muted">读取失败：{evalError}</p>
            ) : evalReports === null ? (
              <p className="muted">读取中…</p>
            ) : evalReports.length ? (
              <ul className="it-list">
                {evalReports.map((report) => (
                  <li key={report.id}>
                    <code>{report.id}</code> · {report.total_count} 例 · pass_rate {report.pass_rate}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">
                0 条。评测 harness 跑在独立的评测库上，产物写在被忽略的 data/eval_reports/ ——
                因此这里没有记录是正常的，不代为编造。
              </p>
            )}
          </div>
        </article>
      </section>
    </main>
  );
}
