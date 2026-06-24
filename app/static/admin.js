const state = {
  activeTab: "tickets",
  user: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const token = localStorage.getItem("agent_access_token");
  const headers = new Headers(options.headers || {});
  if (!headers.has("Content-Type") && options.body) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(path, { ...options, headers });
  const isJson = (response.headers.get("content-type") || "").includes("application/json");
  const data = isJson ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof data === "object" ? data.detail || JSON.stringify(data) : data;
    throw new Error(detail || response.statusText);
  }
  return data;
}

async function logoutCurrentUser() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (_error) {
    // Local fallback: the token may already be expired or revoked.
  } finally {
    localStorage.removeItem("agent_access_token");
  }
}

function showMessage(text, tone = "info") {
  const el = $("message");
  el.textContent = text || "";
  el.className = `message ${tone}`;
}

async function withButton(button, label, task) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try {
    return await task();
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function short(value, limit = 180) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 3)}...` : text;
}

function emptyState(text) {
  return `<div class="empty-state">${escapeHtml(text)}</div>`;
}

function statusBadge(value) {
  return `<span class="status ${escapeHtml(value || "")}">${escapeHtml(value || "unknown")}</span>`;
}

function safeJson(value, limit = 540) {
  try {
    return escapeHtml(short(JSON.stringify(value ?? {}, null, 2), limit));
  } catch {
    return escapeHtml(short(String(value), limit));
  }
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    $("health").textContent = `${health.status} · ${health.app}`;
  } catch (error) {
    $("health").textContent = error.message;
  }
}

async function loadMe() {
  const token = localStorage.getItem("agent_access_token");
  if (!token) {
    state.user = null;
    $("me").textContent = "未登录";
    return null;
  }
  try {
    state.user = await api("/api/auth/me");
    $("me").textContent = `${state.user.id} · ${state.user.role}`;
    return state.user;
  } catch {
    localStorage.removeItem("agent_access_token");
    state.user = null;
    $("me").textContent = "未登录";
    return null;
  }
}

function clearAuthenticatedViews() {
  ["metrics", "approvals", "multiAgentRuns", "runs", "jobs", "users", "artifacts"].forEach((id) => {
    $(id).innerHTML = emptyState("请先登录后台账号");
  });
}

async function refreshAll() {
  await loadHealth();
  const user = await loadMe();
  if (!user) {
    clearAuthenticatedViews();
    return;
  }
  await Promise.allSettled([
    loadMetrics(),
    loadApprovals(),
    loadMultiAgentRuns(),
    loadWorkflowRuns(),
    loadJobs(),
    loadUsers(),
    loadArtifacts(),
  ]);
}

async function loadMetrics() {
  try {
    const metrics = await api("/api/metrics/summary");
    const totals = metrics.totals || {};
    const items = [
      ["工作流", totals.runs],
      ["异步任务", totals.jobs],
      ["待审批", totals.pending_approvals],
      ["工单", totals.tickets],
      ["邮件", totals.emails],
      ["平均耗时", `${Number(totals.avg_latency_ms || 0).toFixed(0)} ms`],
      ["估算成本", Number(totals.estimated_cost || 0).toFixed(3)],
    ];
    const runCounts = (metrics.run_counts || []).map((item) => `${item.status}: ${item.count}`).join(" · ") || "暂无工作流";
    const jobCounts = (metrics.job_counts || []).map((item) => `${item.status}: ${item.count}`).join(" · ") || "暂无任务";
    $("metrics").innerHTML = `
      ${items.map(([label, value]) => `<div class="metric"><strong>${escapeHtml(value ?? 0)}</strong><span>${escapeHtml(label)}</span></div>`).join("")}
      <div class="metric metric-wide"><strong>${escapeHtml(runCounts)}</strong><span>工作流状态</span></div>
      <div class="metric metric-wide"><strong>${escapeHtml(jobCounts)}</strong><span>队列状态</span></div>
    `;
  } catch (error) {
    $("metrics").innerHTML = emptyState(error.message);
  }
}

async function loadApprovals() {
  try {
    const approvals = await api("/api/approvals?limit=20");
    $("approvals").innerHTML = approvals.map(renderApproval).join("") || emptyState("暂无审批项");
    bindApprovalButtons();
  } catch (error) {
    $("approvals").innerHTML = emptyState(error.message);
  }
}

function renderApproval(approval) {
  return `
    <article class="item">
      <div class="item-title">
        <strong>${escapeHtml(approval.tool_name)}</strong>
        ${statusBadge(approval.status)}
      </div>
      <div class="muted">${escapeHtml(approval.id)} · ${escapeHtml(approval.action_type || "-")}</div>
      <pre>${safeJson(approval.payload, 420)}</pre>
      ${
        approval.status === "pending"
          ? `<div class="button-row">
              <button data-approve="${escapeHtml(approval.id)}" type="button">通过</button>
              <button class="danger" data-deny="${escapeHtml(approval.id)}" type="button">拒绝</button>
            </div>`
          : ""
      }
    </article>
  `;
}

function bindApprovalButtons() {
  document.querySelectorAll("[data-approve]").forEach((button) => {
    button.addEventListener("click", () => decideApproval(button, button.dataset.approve, true));
  });
  document.querySelectorAll("[data-deny]").forEach((button) => {
    button.addEventListener("click", () => decideApproval(button, button.dataset.deny, false));
  });
}

async function decideApproval(button, approvalId, approved) {
  await withButton(button, "处理中...", async () => {
    await api(`/api/approvals/${approvalId}/decide`, {
      method: "POST",
      body: JSON.stringify({
        approved,
        decided_by: state.user?.id || "admin",
        reason: approved ? "后台审批通过" : "后台审批拒绝",
      }),
    });
    showMessage(approved ? "审批已通过" : "审批已拒绝", "ok");
    await refreshAll();
  }).catch((error) => showMessage(error.message, "bad"));
}

async function loadMultiAgentRuns() {
  try {
    const runs = await api("/api/multi-agent/runs?limit=8");
    const details = await Promise.all(runs.slice(0, 5).map((run) => api(`/api/multi-agent/runs/${run.id}`)));
    $("multiAgentRuns").innerHTML = details.map(renderMultiAgentRun).join("") || emptyState("暂无多智能体轨迹");
    bindTraceButtons();
  } catch (error) {
    $("multiAgentRuns").innerHTML = emptyState(error.message);
  }
}

function renderMultiAgentRun(run) {
  const messages = (run.messages || []).map(renderAgentMessage).join("");
  const findings = renderCriticFindings(run.critic_report);
  return `
    <article class="item trace-item">
      <div class="item-title">
        <strong>${escapeHtml(short(run.objective, 150))}</strong>
        ${statusBadge(run.status)}
      </div>
      <div class="muted">
        ${escapeHtml(run.id)} · workflow=${escapeHtml(run.workflow_run_id || "-")} · critic=${Number(run.critic_score || 0).toFixed(0)} · ${escapeHtml(run.latency_ms || 0)}ms
      </div>
      <p class="summary-text">${escapeHtml(run.final_summary || "等待最终总结")}</p>
      <div class="agent-track">${messages || emptyState("暂无 agent 消息")}</div>
      <div class="critic-box">${findings}</div>
      <div class="button-row">
        <button class="secondary" data-golden="${escapeHtml(run.id)}" type="button">保存 Golden</button>
        <button class="secondary" data-replay="${escapeHtml(run.id)}" type="button">Replay</button>
      </div>
    </article>
  `;
}

function renderAgentMessage(message) {
  return `
    <div class="agent-step">
      <div class="agent-step-head">
        <code>${escapeHtml(message.agent_name)}</code>
        ${statusBadge(message.status)}
      </div>
      <span>${escapeHtml(message.role || "-")} · ${escapeHtml(message.latency_ms || 0)}ms</span>
      <p>${escapeHtml(describeAgentContent(message.content))}</p>
    </div>
  `;
}

function describeAgentContent(content) {
  if (!content || typeof content !== "object") {
    return short(content || "无内容", 160);
  }
  if (content.plan) {
    return short(`规划：${content.plan.category || ""} ${content.plan.summary || content.reasoning || ""}`, 180);
  }
  if (content.risk_level || content.decision) {
    return short(`风险：${content.risk_level || "-"}，决策：${content.decision || "-"}`, 180);
  }
  if (content.final_answer) {
    return short(content.final_answer, 180);
  }
  if (content.answer) {
    return short(content.answer, 180);
  }
  if (content.error) {
    return short(content.error, 180);
  }
  return short(JSON.stringify(content), 180);
}

function renderCriticFindings(report) {
  if (!report || Object.keys(report).length === 0) {
    return `<span class="muted">暂无 critic 报告</span>`;
  }
  const findings = report.findings || report.issues || [];
  if (Array.isArray(findings) && findings.length) {
    return findings
      .slice(0, 4)
      .map((item) => `<div class="finding">${escapeHtml(typeof item === "string" ? item : JSON.stringify(item))}</div>`)
      .join("");
  }
  return `<pre>${safeJson(report, 420)}</pre>`;
}

function bindTraceButtons() {
  document.querySelectorAll("[data-golden]").forEach((button) => {
    button.addEventListener("click", () => saveGoldenTrace(button, button.dataset.golden));
  });
  document.querySelectorAll("[data-replay]").forEach((button) => {
    button.addEventListener("click", () => replayTrace(button, button.dataset.replay));
  });
}

async function saveGoldenTrace(button, runId) {
  const name = window.prompt("Golden Trace 名称", `golden-${runId.slice(-6)}`);
  if (!name) {
    return;
  }
  await withButton(button, "保存中...", async () => {
    await api("/api/golden-traces", {
      method: "POST",
      body: JSON.stringify({ run_id: runId, name }),
    });
    showMessage("Golden Trace 已保存", "ok");
  }).catch((error) => showMessage(error.message, "bad"));
}

async function replayTrace(button, runId) {
  await withButton(button, "Replay 中...", async () => {
    const replay = await api("/api/trace-replay", {
      method: "POST",
      body: JSON.stringify({ source_run_id: runId }),
    });
    showMessage(`Replay 完成：${replay.replay_run_id || replay.id || "已生成"}`, "ok");
    await refreshAll();
  }).catch((error) => showMessage(error.message, "bad"));
}

async function loadWorkflowRuns() {
  try {
    const runs = await api("/api/runs?limit=12");
    const details = await Promise.all(runs.slice(0, 6).map((run) => api(`/api/runs/${run.id}`)));
    $("runs").innerHTML = details.map(renderWorkflowRun).join("") || emptyState("暂无工作流记录");
  } catch (error) {
    $("runs").innerHTML = emptyState(error.message);
  }
}

function renderWorkflowRun(run) {
  const steps = (run.steps || []).map(renderWorkflowStep).join("");
  return `
    <article class="item trace-item">
      <div class="item-title">
        <strong>${escapeHtml(short(run.objective, 150))}</strong>
        ${statusBadge(run.status)}
      </div>
      <div class="muted">
        ${escapeHtml(run.id)} · category=${escapeHtml(run.category || "-")} · risk=${escapeHtml(run.risk_level || "-")} · ${escapeHtml(run.latency_ms || 0)}ms
      </div>
      <p class="summary-text">${escapeHtml(run.final_answer || run.refusal_reason || "暂无最终输出")}</p>
      <div class="steps">${steps || emptyState("暂无步骤")}</div>
    </article>
  `;
}

function renderWorkflowStep(step) {
  return `
    <div class="step">
      <span>${escapeHtml(step.step_index)}</span>
      <code>${escapeHtml(step.node_name)}</code>
      <div>
        <div>${escapeHtml(step.action_type)} · ${escapeHtml(step.tool_name || "internal")} · ${statusBadge(step.status)}</div>
        <p>${escapeHtml(short(step.reasoning_summary, 180))}</p>
      </div>
    </div>
  `;
}

async function loadJobs() {
  try {
    const jobs = await api("/api/jobs?limit=12");
    $("jobs").innerHTML = jobs.map(renderJob).join("") || emptyState("暂无异步任务");
    bindJobButtons();
  } catch (error) {
    $("jobs").innerHTML = emptyState(error.message);
  }
}

function renderJob(job) {
  const canRetry = ["failed", "queued"].includes(job.status);
  return `
    <article class="item">
      <div class="item-title">
        <strong>${escapeHtml(short(job.objective, 90))}</strong>
        ${statusBadge(job.status)}
      </div>
      <div class="muted">${escapeHtml(job.id)} · attempts=${escapeHtml(job.attempts)}/${escapeHtml(job.max_attempts)}</div>
      ${job.error_message ? `<p class="summary-text bad-text">${escapeHtml(short(job.error_message, 220))}</p>` : ""}
      ${
        canRetry
          ? `<div class="button-row">
              <button class="secondary" data-retry-job="${escapeHtml(job.id)}" type="button">重试</button>
              <button class="secondary" data-run-next="1" type="button">运行队列</button>
            </div>`
          : ""
      }
    </article>
  `;
}

function bindJobButtons() {
  document.querySelectorAll("[data-retry-job]").forEach((button) => {
    button.addEventListener("click", () => retryJob(button, button.dataset.retryJob));
  });
  document.querySelectorAll("[data-run-next]").forEach((button) => {
    button.addEventListener("click", () => runNextJob(button));
  });
}

async function retryJob(button, jobId) {
  await withButton(button, "重试中...", async () => {
    await api(`/api/jobs/${jobId}/retry`, { method: "POST" });
    showMessage("任务已重新入队", "ok");
    await refreshAll();
  }).catch((error) => showMessage(error.message, "bad"));
}

async function runNextJob(button) {
  await withButton(button, "运行中...", async () => {
    const result = await api("/api/jobs/run-next", { method: "POST" });
    showMessage(result.message || `任务处理完成：${result.id || result.run_id || ""}`, "ok");
    await refreshAll();
  }).catch((error) => showMessage(error.message, "bad"));
}

async function loadUsers() {
  try {
    const users = await api("/api/users?limit=50");
    $("users").innerHTML = users.map(renderUser).join("") || emptyState("暂无用户");
  } catch (error) {
    $("users").innerHTML = emptyState(error.message);
  }
}

function renderUser(user) {
  return `
    <article class="item">
      <div class="item-title">
        <strong>${escapeHtml(user.display_name || user.id)}</strong>
        ${statusBadge(user.role)}
      </div>
      <div class="muted">${escapeHtml(user.id)} · ${escapeHtml(user.department || "-")}</div>
    </article>
  `;
}

async function loadArtifacts() {
  try {
    const endpoint = {
      tickets: "/api/tickets?limit=20",
      emails: "/api/emails?limit=20",
      knowledge: "/api/knowledge?limit=20",
      audit: "/api/audit-logs?limit=50",
    }[state.activeTab];
    const items = await api(endpoint);
    $("artifacts").innerHTML = items.map(renderArtifact).join("") || emptyState("暂无数据");
  } catch (error) {
    $("artifacts").innerHTML = emptyState(error.message);
  }
}

function renderArtifact(item) {
  if (state.activeTab === "tickets") {
    return `<article class="item"><div class="item-title"><strong>${escapeHtml(item.title)}</strong>${statusBadge(item.status)}</div><div class="muted">${escapeHtml(item.id)} · ${escapeHtml(item.owner_department || "-")}</div><p class="summary-text">${escapeHtml(short(item.description, 360))}</p></article>`;
  }
  if (state.activeTab === "emails") {
    return `<article class="item"><div class="item-title"><strong>${escapeHtml(item.subject)}</strong>${statusBadge(item.status)}</div><div class="muted">${escapeHtml(item.to_address)}</div><p class="summary-text">${escapeHtml(short(item.body, 360))}</p></article>`;
  }
  if (state.activeTab === "knowledge") {
    return `<article class="item"><div class="item-title"><strong>${escapeHtml(item.title)}</strong><span class="status">${escapeHtml(item.category)}</span></div><div class="muted">${escapeHtml(item.tags || "-")}</div><p class="summary-text">${escapeHtml(short(item.content, 360))}</p></article>`;
  }
  return `<article class="item"><div class="item-title"><strong>${escapeHtml(item.action || item.event_type || "audit")}</strong><span class="status">${escapeHtml(item.target_type || "audit")}</span></div><div class="muted">${escapeHtml(item.actor || "-")} · ${escapeHtml(item.target_id || "-")} · ${escapeHtml(item.created_at || "")}</div><pre>${safeJson(item.payload || item, 460)}</pre></article>`;
}

async function checkRag(button) {
  await withButton(button, "检查中...", async () => {
    const response = await api("/api/mcp/call", {
      method: "POST",
      body: JSON.stringify({
        tool_name: "query_enterprise_rag",
        arguments: {
          question: "远程办公申请资格是什么？",
          top_k: 3,
          user_id: "alice",
          user_department: "Customer Success",
          user_role: "employee",
        },
      }),
    });
    const result = response.result || {};
    const citations = result.citations || [];
    if (result.available === false) {
      showMessage(`RAG 未可用：${result.answer || "请检查服务"}`, "warn");
      return;
    }
    showMessage(`RAG 可用，返回 citations=${citations.length}`, "ok");
    state.activeTab = "audit";
    setActiveTab("audit");
    await loadArtifacts();
  }).catch((error) => showMessage(error.message, "bad"));
}

async function clearHistory(button) {
  if (!window.confirm("确认清理多智能体轨迹、工作流轨迹、审批、工单、邮件、审计和回放记录？用户和知识库会保留。")) {
    return;
  }
  await withButton(button, "清理中...", async () => {
    const result = await api("/api/admin/clear-history", { method: "POST" });
    const deleted = Object.entries(result.deleted || {})
      .map(([key, value]) => `${key}=${value}`)
      .join("，");
    showMessage(`轨迹已清理${deleted ? `：${deleted}` : ""}`, "ok");
    await refreshAll();
  }).catch((error) => showMessage(error.message, "bad"));
}

async function seedData(button) {
  if (!window.confirm("确认重置演示数据？这会清空当前数据库并重新生成样例用户、客户和知识。")) {
    return;
  }
  await withButton(button, "重置中...", async () => {
    await api("/api/admin/seed?reset=true", { method: "POST" });
    showMessage("演示数据已重置", "ok");
    await refreshAll();
  }).catch((error) => showMessage(error.message, "bad"));
}

async function login(button) {
  await withButton(button, "登录中...", async () => {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        user_id: $("loginUser").value.trim(),
        password: $("loginPassword").value,
      }),
      headers: {},
    });
    localStorage.setItem("agent_access_token", result.access_token);
    showMessage("登录成功", "ok");
    await refreshAll();
  }).catch((error) => showMessage(error.message, "bad"));
}

function setActiveTab(tabName) {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.tab === tabName);
  });
}

$("loginBtn").addEventListener("click", (event) => login(event.currentTarget));
$("logoutBtn").addEventListener("click", async () => {
  await logoutCurrentUser();
  state.user = null;
  $("me").textContent = "未登录";
  showMessage("已退出", "ok");
  clearAuthenticatedViews();
});
$("refreshBtn").addEventListener("click", (event) => withButton(event.currentTarget, "刷新中...", refreshAll));
$("ragCheckBtn").addEventListener("click", (event) => checkRag(event.currentTarget));
$("clearHistoryBtn").addEventListener("click", (event) => clearHistory(event.currentTarget));
$("seedBtn").addEventListener("click", (event) => seedData(event.currentTarget));

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", async () => {
    state.activeTab = button.dataset.tab;
    setActiveTab(state.activeTab);
    await loadArtifacts();
  });
});

refreshAll();
