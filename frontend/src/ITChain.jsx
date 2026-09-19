// Lifted out of App.jsx for Phase 6, so the showcase can render the same chain
// view the IT panel renders: same steps, same four-way reading of tool
// execution, same approval buttons.
//
// One block is new. The Risk Gate step now lists the gate's own inputs
// (action_type / environment / criticality / evidence_count / actor_role /
// triage_needs_approval / llm_confidence / missing_information) next to its
// decision, because "why did this need a human?" is the question the showcase
// exists to answer and the response already carries the answer. Every value is
// read off `risk_decision`; anything the server did not send renders as "-"
// rather than being inferred here.

import { Check, RefreshCw, X } from "lucide-react";

import { IconButton, Status, short } from "./ui.jsx";


export function ITChain({ chain, canDecide, role, busy, onDecide, onRefresh }) {
  const ticket = chain.ticket || {};
  const triage = chain.triage || {};
  const resolution = chain.resolution || {};
  const risk = chain.risk_decision || {};
  const execution = chain.execution || {};
  const approval = chain.approval || null;
  const history = resolution.historical_evidence || [];
  const knowledge = resolution.evidence || [];

  const steps = [
    {
      key: "ticket",
      title: "Ticket 受理",
      state: "done",
      detail: (
        <>
          <code>{ticket.id}</code> · <Status value={ticket.status} /> · {ticket.priority || "-"} ·{" "}
          {ticket.asset_id || "无关联资产"}
        </>
      ),
    },
    {
      key: "triage",
      title: "Triage 分类",
      state: triage.category ? "done" : "todo",
      detail: (
        <>
          {triage.intent || "-"} · {triage.category || "-"} · {triage.priority || "-"} · 环境{" "}
          {triage.entities?.environment || "未标注"}
          {triage.missing_information?.length ? ` · 缺失信息 ${triage.missing_information.join("、")}` : ""}
        </>
      ),
    },
    {
      key: "knowledge",
      title: "Knowledge 检索（正式知识 / 政策 / Runbook）",
      state: knowledge.length ? "done" : "todo",
      detail: (
        <ITEvidenceList
          tone="knowledge"
          items={knowledge.map((item) => ({
            key: item.article_id || item.title,
            title: item.title,
            meta: `${item.source || "-"} · score ${item.score ?? "-"}`,
            body: item.snippet,
          }))}
          empty="未检索到可引用的正式知识"
        />
      ),
    },
    {
      key: "history",
      title: "Historical Ticket 检索（历史工单，仅供参考）",
      state: history.length ? "done" : "todo",
      detail: (
        <>
          <div className="it-badges">
            <span className={`it-badge ${resolution.historical_reference ? "warn" : "muted"}`}>
              historical_reference={String(Boolean(resolution.historical_reference))}
            </span>
            <span className={`it-badge ${resolution.historical_divergence ? "warn" : "muted"}`}>
              historical_divergence={String(Boolean(resolution.historical_divergence))}
            </span>
          </div>
          <ITEvidenceList
            tone="historical"
            items={history.map((item) => ({
              key: item.ticket_id,
              title: `${item.ticket_id} · ${item.title}`,
              meta: `${item.category || "-"} · ${item.environment || "-"} · similarity ${item.similarity ?? "-"} · 当时的动作 ${item.resolution_action || "-"}`,
              body: item.snippet,
            }))}
            empty="未检索到相似历史工单"
          />
          {resolution.historical_note ? <p className="muted">{resolution.historical_note}</p> : null}
        </>
      ),
    },
    {
      key: "resolution",
      title: "Resolution 决议",
      state: resolution.status ? "done" : "todo",
      detail: (
        <>
          <Status value={resolution.status} /> · {resolution.action_type || "-"} · 目标{" "}
          {resolution.target || "-"} · 环境 {resolution.environment || "-"}
          <p>{short(resolution.diagnosis, 220) || "没有可用证据，因此没有给出诊断。"}</p>
        </>
      ),
    },
    {
      key: "risk",
      title: "Risk Gate 风险门禁",
      state: risk.decision ? "done" : "todo",
      detail: (
        <>
          <Status value={risk.decision} /> · {risk.rule_id || "-"} · executable=
          {String(Boolean(risk.executable))} · 工具 {risk.tool_name || "-"}
          {/* The gate's inputs, taken from the response as-is. `rule_id` is
              whichever rule the cascade chose -- the source's R0..R7 labels are
              comments, not values, so none of them appear here. */}
          <dl className="it-risk-facts">
            <dt>action_type</dt>
            <dd>{risk.action_type || "-"}</dd>
            <dt>environment</dt>
            <dd>{risk.inputs?.environment || risk.environment || "-"}</dd>
            <dt>criticality</dt>
            <dd>{risk.inputs?.criticality || "-"}</dd>
            <dt>evidence_count</dt>
            <dd>{risk.inputs?.evidence_count ?? "-"}</dd>
            <dt>actor_role</dt>
            <dd>{risk.inputs?.actor_role || "-"}</dd>
            <dt>triage_needs_approval</dt>
            <dd>{String(Boolean(risk.inputs?.triage_needs_approval))}</dd>
            <dt>llm_confidence</dt>
            <dd>
              {risk.llm_confidence ?? "-"} · used={String(Boolean(risk.llm_confidence_used))}
            </dd>
            <dt>missing_information</dt>
            <dd>{(risk.inputs?.missing_information || []).join("、") || "-"}</dd>
            <dt>risk_class</dt>
            <dd>{risk.risk_class || "-"}</dd>
            <dt>mode</dt>
            <dd>{risk.mode || "-"}</dd>
          </dl>
          <ul className="it-list">
            {(risk.reasons || []).map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </>
      ),
    },
    {
      key: "approval",
      title: "Approval 人工审批",
      // Three states, not two: no approval needed, one waiting, one decided.
      state: approval ? (approval.status === "pending" ? "waiting" : "done") : "done",
      detail: approval ? (
        <>
          <Status value={approval.status} /> · {approval.action_type} · 工具 {approval.tool_name || "-"}
          {approval.decided_by ? ` · 决策人 ${approval.decided_by}` : ""}
          {approval.reason ? <p className="muted">{approval.reason}</p> : null}
          {approval.status === "pending" ? (
            <div className="approval-actions">
              <IconButton
                icon={Check}
                label={busy === `decide:${approval.id}` ? "提交中" : "批准执行"}
                disabled={!canDecide || Boolean(busy)}
                onClick={() => onDecide(approval.id, true)}
              />
              <IconButton
                icon={X}
                label="拒绝"
                variant="danger"
                disabled={!canDecide || Boolean(busy)}
                onClick={() => onDecide(approval.id, false)}
              />
            </div>
          ) : null}
          {approval.status === "pending" && !canDecide ? (
            <p className="muted">当前账号角色为 {role}，无权审批；请以 manager / admin 身份登录。</p>
          ) : null}
        </>
      ) : (
        <p className="muted">风控判定为自动执行，无需人工审批。</p>
      ),
    },
    {
      key: "tool",
      title: "Tool Execution 工具执行",
      state: execution?.executed ? "done" : approval?.status === "pending" ? "waiting" : "todo",
      // Four distinct answers, not two. "Nothing ran because a human has not
      // decided yet" and "nothing ran because there was nothing runnable" look
      // identical if you only test ``executed``, and they mean opposite things
      // to whoever is reading the ticket.
      detail: execution?.executed ? (
        <>
          <Status value="executed" /> · {execution.tool_name} · {execution.arguments?.asset_id || execution.target || "-"} ·
          执行账号 {execution.executed_by || "-"} · 请求人 {execution.requested_by || "-"}
        </>
      ) : approval?.status === "pending" ? (
        <>
          <Status value="not_yet" />
          <p>等待人工审批，尚未执行任何工具。</p>
        </>
      ) : approval?.status === "denied" ? (
        <>
          <Status value="not_executed" />
          <p>审批被拒绝：{execution?.reason || "approval_denied"}，未执行任何工具。</p>
        </>
      ) : risk.decision === "deny" ? (
        <>
          <Status value="not_executed" />
          <p>风险门禁拒绝：{execution?.reason || "risk_deny"}，未执行任何工具。</p>
        </>
      ) : (
        <>
          <Status value="not_executed" />
          <p>未执行：{execution?.reason || "no_action"}（没有可执行的工具动作，不编造解决方案）。</p>
        </>
      ),
    },
    {
      key: "audit",
      title: "Final Status 与审计链",
      state: ["resolved", "rejected", "closed"].includes(ticket.status) ? "done" : "waiting",
      detail: (
        <>
          <Status value={ticket.status} />
          <ol className="it-audit">
            {(chain.audit || []).map((row) => (
              <li key={row.id}>
                <code>{row.event_type}</code>
                <span className="muted">
                  {row.actor} · {(row.created_at || "").slice(11, 19)}
                </span>
              </li>
            ))}
          </ol>
          <IconButton icon={RefreshCw} label="刷新链路" variant="ghost" onClick={onRefresh} />
        </>
      ),
    },
  ];

  // The same ``.progress-step`` markup and styling as ``ProgressTimeline``, but
  // rendered here rather than through it: that component wraps each detail in a
  // ``<p>``, and these details contain lists and blocks. A ``<div>`` inside a
  // ``<p>`` is invalid HTML that the browser silently restructures, which would
  // have looked like a styling bug rather than the markup error it is.
  return (
    <div className="progress-timeline it-timeline">
      {steps.map((step, index) => (
        <div className={`progress-step ${step.state}`} key={step.key}>
          <span className="progress-index">{index + 1}</span>
          <div className="it-step">
            <strong>{step.title}</strong>
            <div className="it-step-detail">{step.detail}</div>
          </div>
        </div>
      ))}
      <ITEvidenceNote />
    </div>
  );
}

function ITEvidenceList({ tone, items, empty }) {
  if (!items.length) return <p className="muted">{empty}</p>;
  return (
    <ul className={`it-evidence ${tone}`}>
      {items.map((item) => (
        <li key={item.key}>
          <strong>{item.title}</strong>
          <span className="muted">{item.meta}</span>
          {item.body ? <p>{short(item.body, 200)}</p> : null}
        </li>
      ))}
    </ul>
  );
}

function ITEvidenceNote() {
  // §六's separation, stated where a reader of the UI will actually meet it.
  return (
    <p className="it-evidence-note">
      正式知识（政策 / Runbook）是执行依据；历史工单只是过去的处理经验，仅作参考，不构成政策，也不会被用来选择动作。
    </p>
  );
}
