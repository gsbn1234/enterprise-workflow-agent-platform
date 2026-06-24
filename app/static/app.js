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

function statusBadge(value) {
  return `<span class="status ${escapeHtml(value || "")}">${escapeHtml(value || "unknown")}</span>`;
}

function short(value, limit = 160) {
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
  $("metrics").innerHTML = emptyState("请先登录");
  $("recentRuns").innerHTML = emptyState("请先登录后查看处理状态");
  $("approvals").innerHTML = emptyState("请先登录后查看审批");
  $("artifacts").innerHTML = emptyState("请先登录后查看业务产物");
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
    loadRecentRuns(),
    loadApprovals(),
    loadArtifacts(),
  ]);
}

async function loadMetrics() {
  try {
    const metrics = await api("/api/metrics/summary");
    const totals = metrics.totals;
    $("metrics").innerHTML = [
      ["运行", totals.runs],
      ["待审批", totals.pending_approvals],
      ["工单", totals.tickets],
      ["邮件", totals.emails],
    ]
      .map(([label, value]) => `<div class="metric"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`)
      .join("");
  } catch (error) {
    $("metrics").innerHTML = emptyState(error.message);
  }
}

async function loadRecentRuns() {
  try {
    const runs = await api("/api/multi-agent/runs?limit=6");
    const details = [];
    for (const run of runs.slice(0, 3)) {
      details.push(await api(`/api/multi-agent/runs/${run.id}`));
    }
    $("recentRuns").innerHTML = details.map(renderRunSummary).join("") || emptyState("暂无处理记录");
  } catch (error) {
    $("recentRuns").innerHTML = emptyState(error.message);
  }
}

function renderRunSummary(run) {
  const agents = (run.messages || []).map((item) => item.agent_name).join(" → ");
  return `
    <article class="item">
      <div class="item-title">
        <strong>${escapeHtml(short(run.objective, 110))}</strong>
        ${statusBadge(run.status)}
      </div>
      <div class="muted">workflow=${escapeHtml(run.workflow_run_id || "-")} · critic=${Number(run.critic_score || 0).toFixed(0)} · ${escapeHtml(run.latency_ms || 0)}ms</div>
      <p class="summary-text">${escapeHtml(run.final_summary || "处理中")}</p>
      <div class="timeline-line">${escapeHtml(agents || "等待调度")}</div>
    </article>
  `;
}

async function loadApprovals() {
  if (!state.user || !["admin", "manager"].includes(state.user.role)) {
    $("approvals").innerHTML = emptyState("当前账号没有审批权限");
    return;
  }
  try {
    const approvals = await api("/api/approvals?limit=20");
    $("approvals").innerHTML = approvals.map(renderApproval).join("") || emptyState("暂无待处理审批");
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
      <div class="muted">${escapeHtml(approval.id)}</div>
      <pre>${escapeHtml(short(JSON.stringify(approval.payload, null, 2), 440))}</pre>
      ${
        approval.status === "pending"
          ? `<div class="button-row">
              <button data-approve="${escapeHtml(approval.id)}">通过</button>
              <button class="danger" data-deny="${escapeHtml(approval.id)}">拒绝</button>
            </div>`
          : ""
      }
    </article>
  `;
}

function bindApprovalButtons() {
  document.querySelectorAll("[data-approve]").forEach((button) => {
    button.addEventListener("click", () => decide(button, button.dataset.approve, true));
  });
  document.querySelectorAll("[data-deny]").forEach((button) => {
    button.addEventListener("click", () => decide(button, button.dataset.deny, false));
  });
}

async function decide(button, approvalId, approved) {
  await withButton(button, "处理中...", async () => {
    await api(`/api/approvals/${approvalId}/decide`, {
      method: "POST",
      body: JSON.stringify({
        approved,
        decided_by: state.user?.id || "manager",
        reason: approved ? "审批通过" : "审批拒绝",
      }),
    });
    showMessage(approved ? "审批已通过" : "审批已拒绝", "ok");
    await refreshAll();
  }).catch((error) => showMessage(error.message, "bad"));
}

async function loadArtifacts() {
  try {
    const endpoint = {
      tickets: "/api/tickets?limit=20",
      emails: "/api/emails?limit=20",
      knowledge: "/api/knowledge?limit=20",
    }[state.activeTab];
    const items = await api(endpoint);
    $("artifacts").innerHTML = items.map(renderArtifact).join("") || emptyState("暂无数据");
  } catch (error) {
    $("artifacts").innerHTML = emptyState(error.message);
  }
}

function renderArtifact(item) {
  if (state.activeTab === "tickets") {
    return `<article class="item"><div class="item-title"><strong>${escapeHtml(item.title)}</strong>${statusBadge(item.status)}</div><div class="muted">${escapeHtml(item.id)} · ${escapeHtml(item.owner_department)}</div><p class="summary-text">${escapeHtml(short(item.description, 320))}</p></article>`;
  }
  if (state.activeTab === "emails") {
    return `<article class="item"><div class="item-title"><strong>${escapeHtml(item.subject)}</strong>${statusBadge(item.status)}</div><div class="muted">${escapeHtml(item.to_address)}</div><p class="summary-text">${escapeHtml(short(item.body, 320))}</p></article>`;
  }
  return `<article class="item"><div class="item-title"><strong>${escapeHtml(item.title)}</strong><span class="status">${escapeHtml(item.category)}</span></div><div class="muted">${escapeHtml(item.tags)}</div><p class="summary-text">${escapeHtml(short(item.content, 320))}</p></article>`;
}

function emptyState(text) {
  return `<div class="empty-state">${escapeHtml(text)}</div>`;
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

async function submitRun(button, mode) {
  const payload = formPayload();
  if (!payload.objective) {
    showMessage("请输入业务请求", "warn");
    return;
  }
  const endpoint = mode === "queue" ? "/api/workflow/jobs" : "/api/multi-agent/run";
  const label = mode === "queue" ? "入队中..." : "处理中...";
  await withButton(button, label, async () => {
    await api(endpoint, { method: "POST", body: JSON.stringify(payload) });
    $("objective").value = "";
    showMessage(mode === "queue" ? "请求已加入异步队列" : "请求已处理完成", "ok");
    await refreshAll();
  }).catch((error) => showMessage(error.message, "bad"));
}

$("runForm").addEventListener("submit", (event) => event.preventDefault());
$("loginBtn").addEventListener("click", (event) => login(event.currentTarget));
$("logoutBtn").addEventListener("click", async () => {
  await logoutCurrentUser();
  state.user = null;
  $("me").textContent = "未登录";
  showMessage("已退出", "ok");
  clearAuthenticatedViews();
});
$("multiAgentBtn").addEventListener("click", (event) => submitRun(event.currentTarget, "run"));
$("enqueueBtn").addEventListener("click", (event) => submitRun(event.currentTarget, "queue"));
$("refreshBtn").addEventListener("click", (event) => withButton(event.currentTarget, "刷新中...", refreshAll));

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", async () => {
    document.querySelectorAll(".tab").forEach((tab) => tab.classList.remove("active"));
    button.classList.add("active");
    state.activeTab = button.dataset.tab;
    await loadArtifacts();
  });
});

refreshAll();
