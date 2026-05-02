const state = {
  activeTab: "tickets",
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const token = localStorage.getItem("agent_access_token");
  const authHeaders = token ? { Authorization: `Bearer ${token}` } : {};
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...authHeaders, ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function statusBadge(value) {
  return `<span class="status ${escapeHtml(value || "")}">${escapeHtml(value || "unknown")}</span>`;
}

function short(value, limit = 120) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 3)}...` : text;
}

function formPayload() {
  return {
    objective: $("objective").value.trim(),
    requester_user_id: $("requester").value.trim() || "demo",
    requester_department: $("department").value.trim() || "Business Ops",
  };
}

async function loadMe() {
  const token = localStorage.getItem("agent_access_token");
  if (!token) {
    $("me").textContent = "";
    return;
  }
  try {
    const me = await api("/api/auth/me");
    $("me").textContent = `${me.id} · ${me.role}`;
  } catch (error) {
    localStorage.removeItem("agent_access_token");
    $("me").textContent = "";
  }
}

async function loadHealth() {
  const health = await api("/api/health");
  $("health").textContent = `${health.status} · ${health.db_path}`;
}

async function loadMetrics() {
  const metrics = await api("/api/metrics/summary");
  const totals = metrics.totals;
  $("metrics").innerHTML = [
    ["Runs", totals.runs],
    ["Pending", totals.pending_approvals],
    ["Tickets", totals.tickets],
    ["Emails", totals.emails],
    ["Jobs", totals.jobs],
    ["Avg ms", Math.round(totals.avg_latency_ms || 0)],
    ["Cost", Number(totals.estimated_cost || 0).toFixed(6)],
  ]
    .map(([label, value]) => `<div class="metric"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`)
    .join("");
}

async function loadMultiAgentRuns() {
  const runs = await api("/api/multi-agent/runs?limit=8");
  const details = [];
  for (const run of runs.slice(0, 4)) {
    details.push(await api(`/api/multi-agent/runs/${run.id}`));
  }
  $("multiAgentRuns").innerHTML =
    details
      .map((run) => {
        const messages = run.messages || [];
        const findings = run.critic_report?.findings || [];
        return `
          <article class="item">
            <div class="item-title">
              <strong>${escapeHtml(short(run.objective, 92))}</strong>
              ${statusBadge(run.status)}
            </div>
            <div class="muted">
              critic=${Number(run.critic_score || 0).toFixed(0)}
              · corrections=${escapeHtml(run.correction_count || 0)}
              · ${escapeHtml(run.executor_type || "executor")}
              · workflow=${escapeHtml(run.workflow_run_id || "n/a")}
              · ${escapeHtml(run.latency_ms || 0)}ms
            </div>
            <pre>${escapeHtml(run.final_summary || "")}</pre>
            <div class="agent-track">
              ${messages
                .map(
                  (message) => `
                    <div class="agent-step">
                      <code>${escapeHtml(message.agent_name)}</code>
                      <span>${escapeHtml(message.role)} · ${escapeHtml(message.status)} · ${escapeHtml(message.latency_ms)}ms</span>
                    </div>
                  `,
                )
                .join("")}
            </div>
            ${
              findings.length
                ? `<pre>${escapeHtml(JSON.stringify(findings, null, 2))}</pre>`
                : `<div class="muted success-line">critic passed</div>`
            }
            <div class="item-actions">
              <button class="secondary" data-save-golden="${escapeHtml(run.id)}">保存 Golden</button>
              <button class="secondary" data-replay-run="${escapeHtml(run.id)}">Replay</button>
            </div>
          </article>
        `;
      })
      .join("") || `<div class="muted">暂无多智能体运行记录</div>`;

  document.querySelectorAll("[data-save-golden]").forEach((button) => {
    button.addEventListener("click", async () => {
      const name = window.prompt("Golden trace name", `golden-${button.dataset.saveGolden}`);
      if (!name) return;
      await api("/api/golden-traces", {
        method: "POST",
        body: JSON.stringify({ run_id: button.dataset.saveGolden, name }),
      });
      await refreshAll();
    });
  });
  document.querySelectorAll("[data-replay-run]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api("/api/trace-replay", {
        method: "POST",
        body: JSON.stringify({ source_run_id: button.dataset.replayRun }),
      });
      await refreshAll();
    });
  });
}

async function loadRuns() {
  const runs = await api("/api/runs?limit=12");
  const details = [];
  for (const run of runs.slice(0, 6)) {
    details.push(await api(`/api/runs/${run.id}`));
  }
  $("runs").innerHTML =
    details
      .map(
        (run) => `
          <article class="item">
            <div class="item-title">
              <strong>${escapeHtml(short(run.objective, 90))}</strong>
              ${statusBadge(run.status)}
            </div>
            <div class="muted">${escapeHtml(run.category || "unclassified")} · ${escapeHtml(run.risk_level || "n/a")} · ${escapeHtml(run.latency_ms)}ms</div>
            <pre>${escapeHtml(run.final_answer || "")}</pre>
            <div class="steps">
              ${run.steps
                .map(
                  (step) => `
                    <div class="step">
                      <code>#${escapeHtml(step.step_index)}</code>
                      <code>${escapeHtml(step.node_name)}</code>
                      <span>${escapeHtml(step.tool_name || step.action_type)} · ${escapeHtml(step.latency_ms)}ms${step.attempt_count > 1 ? ` · attempts ${escapeHtml(step.attempt_count)}/${escapeHtml(step.max_attempts)}` : ""}</span>
                    </div>
                  `,
                )
                .join("")}
            </div>
          </article>
        `,
      )
      .join("") || `<div class="muted">暂无运行记录</div>`;
}

async function loadJobs() {
  const jobs = await api("/api/jobs?limit=12");
  $("jobs").innerHTML =
    jobs
      .map(
        (job) => `
          <article class="item">
            <div class="item-title">
              <strong>${escapeHtml(short(job.objective, 74))}</strong>
              ${statusBadge(job.status)}
            </div>
            <div class="muted">${escapeHtml(job.id)} · attempts ${escapeHtml(job.attempts)}/${escapeHtml(job.max_attempts)}</div>
            ${job.run_id ? `<pre>run_id=${escapeHtml(job.run_id)}</pre>` : ""}
            ${job.error_message ? `<pre>${escapeHtml(job.error_message)}</pre>` : ""}
            ${
              job.status === "failed"
                ? `<div class="item-actions"><button data-retry="${escapeHtml(job.id)}">重试</button></div>`
                : ""
            }
          </article>
        `,
      )
      .join("") || `<div class="muted">暂无异步任务</div>`;

  document.querySelectorAll("[data-retry]").forEach((button) => {
    button.addEventListener("click", async () => {
      await api(`/api/jobs/${button.dataset.retry}/retry`, { method: "POST" });
      await refreshAll();
    });
  });
}

async function loadApprovals() {
  const approvals = await api("/api/approvals?limit=20");
  $("approvals").innerHTML =
    approvals
      .map(
        (approval) => `
          <article class="item">
            <div class="item-title">
              <strong>${escapeHtml(approval.tool_name)}</strong>
              ${statusBadge(approval.status)}
            </div>
            <div class="muted">${escapeHtml(approval.id)}</div>
            <pre>${escapeHtml(short(JSON.stringify(approval.payload, null, 2), 420))}</pre>
            ${
              approval.status === "pending"
                ? `<div class="item-actions">
                    <button data-approve="${escapeHtml(approval.id)}">通过</button>
                    <button class="danger" data-deny="${escapeHtml(approval.id)}">拒绝</button>
                  </div>`
                : ""
            }
          </article>
        `,
      )
      .join("") || `<div class="muted">暂无审批</div>`;

  document.querySelectorAll("[data-approve]").forEach((button) => {
    button.addEventListener("click", () => decide(button.dataset.approve, true));
  });
  document.querySelectorAll("[data-deny]").forEach((button) => {
    button.addEventListener("click", () => decide(button.dataset.deny, false));
  });
}

async function decide(approvalId, approved) {
  await api(`/api/approvals/${approvalId}/decide`, {
    method: "POST",
    body: JSON.stringify({
      approved,
      decided_by: "manager",
      reason: approved ? "演示审批通过" : "演示审批拒绝",
    }),
  });
  await refreshAll();
}

async function loadArtifacts() {
  const endpoint = {
    tickets: "/api/tickets?limit=20",
    emails: "/api/emails?limit=20",
    knowledge: "/api/knowledge?limit=20",
  }[state.activeTab];
  const items = await api(endpoint);
  $("artifacts").innerHTML =
    items
      .map((item) => {
        if (state.activeTab === "tickets") {
          return `<article class="item"><div class="item-title"><strong>${escapeHtml(item.title)}</strong>${statusBadge(item.status)}</div><div class="muted">${escapeHtml(item.id)} · ${escapeHtml(item.owner_department)}</div><pre>${escapeHtml(short(item.description, 320))}</pre></article>`;
        }
        if (state.activeTab === "emails") {
          return `<article class="item"><div class="item-title"><strong>${escapeHtml(item.subject)}</strong>${statusBadge(item.status)}</div><div class="muted">${escapeHtml(item.to_address)}</div><pre>${escapeHtml(short(item.body, 320))}</pre></article>`;
        }
        return `<article class="item"><div class="item-title"><strong>${escapeHtml(item.title)}</strong><span class="status">${escapeHtml(item.category)}</span></div><div class="muted">${escapeHtml(item.tags)}</div><pre>${escapeHtml(short(item.content, 320))}</pre></article>`;
      })
      .join("") || `<div class="muted">暂无数据</div>`;
}

async function refreshAll() {
  await loadMe();
  await Promise.allSettled([
    loadHealth(),
    loadMetrics(),
    loadMultiAgentRuns(),
    loadRuns(),
    loadJobs(),
    loadApprovals(),
    loadArtifacts(),
  ]);
}

$("runForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = formPayload();
  if (!payload.objective) return;
  await api("/api/workflow/run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  $("objective").value = "";
  await refreshAll();
});

$("multiAgentBtn").addEventListener("click", async () => {
  const payload = formPayload();
  if (!payload.objective) return;
  await api("/api/multi-agent/run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  $("objective").value = "";
  await refreshAll();
});

$("enqueueBtn").addEventListener("click", async () => {
  const payload = formPayload();
  if (!payload.objective) return;
  await api("/api/workflow/jobs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  $("objective").value = "";
  await refreshAll();
});

$("refreshBtn").addEventListener("click", refreshAll);
$("loginBtn").addEventListener("click", async () => {
  const result = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      user_id: $("loginUser").value.trim(),
      password: $("loginPassword").value,
    }),
    headers: {},
  });
  localStorage.setItem("agent_access_token", result.access_token);
  await refreshAll();
});

$("logoutBtn").addEventListener("click", async () => {
  localStorage.removeItem("agent_access_token");
  await refreshAll();
});

$("seedBtn").addEventListener("click", async () => {
  await api("/api/admin/seed?reset=true", { method: "POST" });
  await refreshAll();
});

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", async () => {
    document.querySelectorAll(".tab").forEach((tab) => tab.classList.remove("active"));
    button.classList.add("active");
    state.activeTab = button.dataset.tab;
    await loadArtifacts();
  });
});

refreshAll().catch((error) => {
  $("health").textContent = error.message;
});
