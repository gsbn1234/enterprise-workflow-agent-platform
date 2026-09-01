import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  Archive,
  BriefcaseBusiness,
  Check,
  ClipboardList,
  Database,
  FileClock,
  Gauge,
  GitBranch,
  Home,
  KeyRound,
  LogIn,
  LogOut,
  Mail,
  Play,
  RefreshCw,
  RotateCcw,
  SearchCheck,
  Send,
  ShieldCheck,
  Ticket,
  UserRound,
  Users,
  Workflow,
  X,
} from "lucide-react";
import {
  api,
  clearToken,
  getToken,
  login as loginApi,
  logout as logoutApi,
  openEventStream,
  startOidcLogin,
} from "./api.js";

const USER_DEFAULT = { userId: "alice", password: "AlicePass123" };
const ADMIN_DEFAULT = { userId: "admin", password: "AdminPass123" };
const REFRESH_SIGNAL_KEY = "agent_refresh_signal";
const APPROVER_ROLES = new Set(["admin", "manager"]);
const SCENARIO_ICONS = {
  refund: Ticket,
  security: ShieldCheck,
  procurement: Database,
  remote_work: Home,
  remote: Home,
  access: KeyRound,
  access_request: KeyRound,
  incident: Activity,
  ticket_query: SearchCheck,
  ticket_update: ClipboardList,
};
const DEMO_SCENARIOS = [
  {
    id: "refund",
    icon: Ticket,
    title: "退款投诉",
    text: "客户 Orbit Retail 投诉上月服务中断，要求退费 800 元，请创建工单并准备回复 fjsmlfy@gmail.com",
  },
  {
    id: "security",
    icon: ShieldCheck,
    title: "安全事件",
    text: "发现 Acme China 账号疑似异常登录并可能存在权限泄露，请通知安全团队并保留审计，不要直接删除数据",
  },
  {
    id: "procurement",
    icon: Database,
    title: "采购申请",
    text: "市场团队申请采购一套数据分析软件，预算 3600 元，请评估风险、创建采购工单并等待负责人审批",
  },
  {
    id: "remote",
    icon: Home,
    title: "远程办公",
    text: "员工 Alice 申请下周三远程办公一天，请根据企业政策判断是否可自动处理并创建记录",
  },
  {
    id: "access",
    icon: KeyRound,
    title: "权限申请",
    text: "销售同事申请开通客户数据导出权限，用于本周客户复盘，请创建权限申请工单并等待负责人审批",
  },
  {
    id: "incident",
    icon: Activity,
    title: "生产故障",
    text: "核心服务 P1 故障影响企业客户登录，请创建故障工单、查询 SLA 政策并通知值班负责人",
  },
  {
    id: "ticket_query",
    icon: SearchCheck,
    title: "查询工单",
    text: "查询 Customer Success 部门工单",
  },
  {
    id: "ticket_update",
    icon: ClipboardList,
    title: "修改工单",
    text: "把最近一个 Customer Success 工单优先级改成 urgent，并备注：客户已二次催促，升级处理",
  },
];

function useRoute() {
  const [route, setRouteState] = useState(window.location.pathname || "/");

  useEffect(() => {
    const onPop = () => setRouteState(window.location.pathname || "/");
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const setRoute = useCallback((nextRoute) => {
    window.history.pushState({}, "", nextRoute);
    setRouteState(nextRoute);
  }, []);

  return [route, setRoute];
}

function useAutoRefresh(callback, intervalMs = 4000, enabled = true) {
  const callbackRef = useRef(callback);

  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);

  useEffect(() => {
    if (!enabled) return undefined;
    const tick = () => {
      if (document.visibilityState !== "visible") return;
      Promise.resolve(callbackRef.current()).catch(() => {});
    };
    const timer = window.setInterval(tick, intervalMs);
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") tick();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [enabled, intervalMs]);
}

function emitRefreshSignal(reason = "data_changed") {
  const payload = JSON.stringify({ reason, at: Date.now() });
  try {
    localStorage.setItem(REFRESH_SIGNAL_KEY, payload);
  } catch {}
  try {
    const channel = new BroadcastChannel(REFRESH_SIGNAL_KEY);
    channel.postMessage(payload);
    channel.close();
  } catch {}
}

function useRefreshSignal(callback) {
  const callbackRef = useRef(callback);

  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);

  useEffect(() => {
    const refresh = () => Promise.resolve(callbackRef.current()).catch(() => {});
    const onStorage = (event) => {
      if (event.key === REFRESH_SIGNAL_KEY) refresh();
    };
    window.addEventListener("storage", onStorage);
    let channel;
    try {
      channel = new BroadcastChannel(REFRESH_SIGNAL_KEY);
      channel.onmessage = refresh;
    } catch {}
    return () => {
      window.removeEventListener("storage", onStorage);
      if (channel) channel.close();
    };
  }, []);
}

function useEventStream(enabled, onSnapshot) {
  const snapshotRef = useRef(onSnapshot);
  const [streamStatus, setStreamStatus] = useState(enabled ? "connecting" : "disabled");

  useEffect(() => {
    snapshotRef.current = onSnapshot;
  }, [onSnapshot]);

  useEffect(() => {
    if (!enabled) {
      setStreamStatus("disabled");
      return undefined;
    }
    setStreamStatus("connecting");
    const close = openEventStream(
      (snapshot) => {
        setStreamStatus("connected");
        snapshotRef.current(snapshot);
      },
      () => setStreamStatus("reconnecting"),
    );
    return close;
  }, [enabled]);

  return streamStatus;
}

export default function App() {
  const [route, setRoute] = useRoute();
  const [user, setUser] = useState(null);
  const [booting, setBooting] = useState(true);
  const [message, setMessage] = useState("");

  const refreshUser = useCallback(async () => {
    if (!getToken()) {
      setUser(null);
      setBooting(false);
      return null;
    }
    try {
      const me = await api("/api/auth/me");
      setUser(me);
      return me;
    } catch {
      clearToken();
      setUser(null);
      return null;
    } finally {
      setBooting(false);
    }
  }, []);

  useEffect(() => {
    refreshUser();
  }, [refreshUser]);

  const logout = async () => {
    await logoutApi().catch(() => clearToken());
    setUser(null);
    setMessage("已退出");
    setRoute("/login");
  };

  const page = route.startsWith("/admin") ? "admin" : route.startsWith("/login") ? "login" : "workspace";
  const canOpenAdmin = user ? APPROVER_ROLES.has(user.role) : false;
  const visibleUser = page === "admin" && !canOpenAdmin ? null : user;

  return (
    <div className="app">
      <TopNav page={page} user={visibleUser} onNavigate={setRoute} onLogout={logout} message={message} />
      {booting ? (
        <main className="screen-center">
          <Card className="boot-card">
            <RefreshCw className="spin" size={22} />
            <span>连接平台中</span>
          </Card>
        </main>
      ) : page === "login" ? (
        <LoginPage
          mode="user"
          onLoggedIn={(nextUser) => {
            setUser(nextUser);
            setMessage("登录成功");
            setRoute(nextUser.role === "admin" ? "/admin" : "/");
          }}
        />
      ) : page === "admin" ? (
        canOpenAdmin ? (
          <AdminConsole user={user} onMessage={setMessage} onNeedLogin={() => setRoute("/login")} />
        ) : (
          <LoginPage
            mode="admin"
            onLoggedIn={(nextUser) => {
              setUser(nextUser);
              setMessage("登录成功");
              setRoute("/admin");
            }}
          />
        )
      ) : user ? (
        <Workspace user={user} onMessage={setMessage} />
      ) : (
        <LoginPage
          mode="user"
          onLoggedIn={(nextUser) => {
            setUser(nextUser);
            setMessage("登录成功");
            setRoute("/");
          }}
        />
      )}
    </div>
  );
}

function TopNav({ page, user, onNavigate, onLogout, message }) {
  return (
    <header className="top-nav">
      <button className="brand-button" type="button" onClick={() => onNavigate("/")}>
        <SparkIcon />
        <span>
          <strong>企业 Agent 平台</strong>
          <small>{page === "admin" ? "后台控制台" : page === "login" ? "登录" : "业务工作台"}</small>
        </span>
      </button>
      <nav className="nav-actions" aria-label="页面导航">
        <NavButton active={page === "workspace"} icon={Home} label="用户端" onClick={() => onNavigate("/")} />
        <NavButton active={page === "admin"} icon={ShieldCheck} label="后台端" onClick={() => onNavigate("/admin")} />
        {message ? <span className="top-message">{message}</span> : null}
        {user ? (
          <>
            <span className="user-pill">
              <UserRound size={15} />
              {user.id} · {user.role}
            </span>
            <IconButton icon={LogOut} label="退出" variant="ghost" onClick={onLogout} />
          </>
        ) : (
          <IconButton icon={LogIn} label="登录" onClick={() => onNavigate("/login")} />
        )}
      </nav>
    </header>
  );
}

function SparkIcon() {
  return (
    <span className="spark-icon">
      <GitBranch size={18} />
    </span>
  );
}

function LoginPage({ mode, onLoggedIn }) {
  const defaults = mode === "admin" ? ADMIN_DEFAULT : USER_DEFAULT;
  const [userId, setUserId] = useState(defaults.userId);
  const [password, setPassword] = useState(defaults.password);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [oidcReady, setOidcReady] = useState(false);

  useEffect(() => {
    setUserId(defaults.userId);
    setPassword(defaults.password);
  }, [defaults.userId, defaults.password]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const oidcError = params.get("oidc_error");
    if (oidcError) {
      setError(oidcError);
      window.history.replaceState({}, "", window.location.pathname);
    }
    fetch("/api/health")
      .then((response) => response.json())
      .then((health) => {
        const oidc = health.oidc || {};
        setOidcReady(Boolean(oidc.browser_login_enabled && (!oidc.missing || oidc.missing.length === 0)));
      })
      .catch(() => setOidcReady(false));
  }, []);

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const nextUser = await loginApi(userId.trim(), password);
      onLoggedIn(nextUser);
    } catch (err) {
      setError(err.message || "登录失败");
    } finally {
      setBusy(false);
    }
  };

  const fillAccount = (nextUserId, nextPassword) => {
    setUserId(nextUserId);
    setPassword(nextPassword);
    setError("");
  };

  return (
    <main className="login-shell">
      <section className="login-layout">
        <Card className="login-card">
          <div className="section-title">
            <KeyRound size={20} />
            <div>
              <h1>{mode === "admin" ? "后台登录" : "用户登录"}</h1>
              <p>{mode === "admin" ? "admin / AdminPass123" : "alice / AlicePass123"}</p>
            </div>
          </div>
          <form className="form-stack" onSubmit={submit}>
            <label>
              账号
              <input value={userId} onChange={(event) => setUserId(event.target.value)} autoComplete="username" />
            </label>
            <label>
              密码
              <input
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                type="password"
                autoComplete="current-password"
              />
            </label>
            {error ? <div className="alert bad">{error}</div> : null}
            <button className="primary-button" type="submit" disabled={busy}>
              {busy ? <RefreshCw className="spin" size={16} /> : <LogIn size={16} />}
              登录
            </button>
          </form>
          {oidcReady ? (
            <button className="action-button secondary" type="button" onClick={() => startOidcLogin(mode === "admin" ? "/admin" : "/")}>
              <ShieldCheck size={16} />
              SSO
            </button>
          ) : null}
          <div className="quick-accounts">
            <button type="button" onClick={() => fillAccount("alice", "AlicePass123")}>
              员工
            </button>
            <button type="button" onClick={() => fillAccount("manager", "ManagerPass123")}>
              经理
            </button>
            <button type="button" onClick={() => fillAccount("admin", "AdminPass123")}>
              管理员
            </button>
          </div>
        </Card>
        <Card className="login-aside">
          <div className="section-title">
            <Workflow size={20} />
            <div>
              <h2>演示链路</h2>
              <p>Supervisor · RAG · Risk · Tool · Trace</p>
            </div>
          </div>
          <div className="flow-preview">
            {["输入请求", "规划任务", "查询知识", "风险判断", "生成产物"].map((item, index) => (
              <div className="flow-node" key={item}>
                <span>{index + 1}</span>
                <strong>{item}</strong>
              </div>
            ))}
          </div>
        </Card>
      </section>
    </main>
  );
}

function Workspace({ user, onMessage }) {
  const [health, setHealth] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [liveSnapshot, setLiveSnapshot] = useState(null);
  const [demoScenarios, setDemoScenarios] = useState([]);
  const [currentArtifacts, setCurrentArtifacts] = useState({ tickets: [], emails: [], knowledge: [] });
  const [currentDecision, setCurrentDecision] = useState(null);
  const [tab, setTab] = useState("tickets");
  const [currentRun, setCurrentRun] = useState(null);
  const [resultNote, setResultNote] = useState(null);
  const [objective, setObjective] = useState(
    "客户 Orbit Retail 投诉上月服务中断，要求退费 800 元，请创建工单并准备回复 support@orbit.example",
  );
  const [busy, setBusy] = useState("");

  const refresh = useCallback(async () => {
    const [healthResult, metricsResult] = await Promise.all([
      api("/api/health"),
      api("/api/metrics/summary"),
    ]);
    setHealth(healthResult);
    setMetrics(metricsResult);
  }, []);

  const loadWorkflowResult = useCallback(async (workflowRunId) => {
    if (!workflowRunId) return;
    const workflow = await api(`/api/runs/${workflowRunId}`);
    setCurrentArtifacts(extractWorkflowArtifacts(workflow));
    setCurrentDecision(extractWorkflowDecision(workflow));
    setResultNote({
      title: workflow.status === "waiting_approval" ? "等待后台审批" : "处理结果已更新",
      status: workflow.status,
      detail: workflow.final_answer || "本次请求已记录处理结果。",
    });
  }, []);

  const loadSavedWorkflowResult = useCallback(async () => {
    const workflowRunId = localStorage.getItem("agent_current_workflow_run_id");
    if (workflowRunId) {
      try {
        await loadWorkflowResult(workflowRunId);
      } catch (err) {
        localStorage.removeItem("agent_current_workflow_run_id");
        setCurrentArtifacts({ tickets: [], emails: [], knowledge: [] });
        setCurrentDecision(null);
        setResultNote(null);
        throw err;
      }
    }
  }, [loadWorkflowResult]);

  const resetCurrentExecution = useCallback((mode) => {
    localStorage.removeItem("agent_current_workflow_run_id");
    setCurrentRun(null);
    setCurrentArtifacts({ tickets: [], emails: [], knowledge: [] });
    setCurrentDecision(null);
    setResultNote({
      title: mode === "queue" ? "正在加入队列" : "正在处理请求",
      status: mode === "queue" ? "queued" : "running",
      detail:
        mode === "queue"
          ? "正在创建异步任务，创建完成后会进入后台队列。"
          : "Agent 正在规划任务、查询知识、判断风险并准备调用工具。",
    });
  }, []);

  const refreshWorkspace = useCallback(async () => {
    const results = await Promise.allSettled([refresh(), loadSavedWorkflowResult()]);
    const rejected = results.find((result) => result.status === "rejected");
    if (rejected) onMessage(rejected.reason.message);
  }, [refresh, loadSavedWorkflowResult, onMessage]);

  const liveStatus = useEventStream(Boolean(user), (snapshot) => {
    setLiveSnapshot(snapshot);
    refreshWorkspace();
  });

  useEffect(() => {
    refreshWorkspace();
  }, [refreshWorkspace]);

  useEffect(() => {
    api("/api/demo-scenarios")
      .then((items) =>
        setDemoScenarios(
          items.map((item) => ({
            ...item,
            text: item.objective,
            icon: SCENARIO_ICONS[item.id] || SCENARIO_ICONS[item.category] || Workflow,
          })),
        ),
      )
      .catch(() => setDemoScenarios([]));
  }, []);

  useAutoRefresh(refreshWorkspace, 4000, !busy);

  useEffect(() => {
    const onFocus = () => refreshWorkspace();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refreshWorkspace]);

  const submitRun = async (mode) => {
    if (!objective.trim()) {
      onMessage("请输入业务请求");
      return;
    }
    resetCurrentExecution(mode);
    setBusy(mode);
    try {
      const body = {
        objective,
        requester_user_id: user.id,
        requester_department: user.department,
        requester_role: user.role,
      };
      if (mode === "queue") {
        const job = await api("/api/workflow/jobs", { method: "POST", body: JSON.stringify(body) });
        setResultNote({
          title: "已加入异步队列",
          status: job.status || "queued",
          detail: `任务 ${job.id} 已创建，后台会处理队列并记录完整轨迹。`,
        });
        emitRefreshSignal("job_created");
        onMessage("已加入异步队列");
      } else {
        const run = await api("/api/multi-agent/run", { method: "POST", body: JSON.stringify(body) });
        setCurrentRun(run);
        setResultNote(null);
        if (run.workflow_run_id) {
          localStorage.setItem("agent_current_workflow_run_id", run.workflow_run_id);
          await loadWorkflowResult(run.workflow_run_id);
        } else {
          localStorage.removeItem("agent_current_workflow_run_id");
          setCurrentArtifacts({ tickets: [], emails: [], knowledge: [] });
          setCurrentDecision(null);
        }
        emitRefreshSignal("run_created");
        onMessage("处理完成");
      }
      await refresh();
    } catch (err) {
      onMessage(err.message);
      setResultNote({
        title: "处理失败",
        status: "failed",
        detail: err.message || "请求处理失败，请稍后重试。",
      });
    } finally {
      setBusy("");
    }
  };

  return (
    <main className="workspace-shell">
      <section className="workspace-sidebar">
        <Card>
          <PanelHeader icon={UserRound} title={user.display_name || user.id} kicker={user.role} />
          <div className="profile-grid">
            <span>账号</span>
            <strong>{user.id}</strong>
            <span>部门</span>
            <strong>{user.department}</strong>
          </div>
        </Card>
        <Card>
          <PanelHeader icon={Gauge} title="状态概览" kicker={health?.status || "ready"} />
          <MetricGrid metrics={metrics} compact />
        </Card>
        <LiveStatusCard status={liveStatus} health={health} snapshot={liveSnapshot} />
      </section>

      <section className="workspace-main">
        <Card className="request-card">
          <PanelHeader icon={Send} title="业务请求" kicker="Agent Run" />
          <ScenarioPicker scenarios={demoScenarios.length ? demoScenarios : DEMO_SCENARIOS} onPick={(text) => setObjective(text)} />
          <textarea value={objective} onChange={(event) => setObjective(event.target.value)} />
          <div className="button-row">
            <IconButton
              icon={Play}
              label="提交并处理"
              disabled={busy === "run"}
              onClick={() => submitRun("run")}
            />
            <IconButton
              icon={FileClock}
              label="加入队列"
              variant="secondary"
              disabled={busy === "queue"}
              onClick={() => submitRun("queue")}
            />
            <IconButton icon={RefreshCw} label="刷新" variant="ghost" onClick={() => refresh().then(loadSavedWorkflowResult)} />
          </div>
        </Card>

        <Card className="result-card">
          <PanelHeader icon={ClipboardList} title="本次结果" kicker="Result" />
          <UserResult run={currentRun} note={resultNote} decision={currentDecision} artifacts={currentArtifacts} />
        </Card>

        <Card>
          <PanelHeader icon={Workflow} title="执行进度" kicker={liveStatus === "connected" ? "Live" : "Polling"} />
          <ProgressTimeline steps={buildProgressSteps({ busy, run: currentRun, note: resultNote, decision: currentDecision, artifacts: currentArtifacts })} />
        </Card>

        <Card>
          <PanelHeader icon={SearchCheck} title="决策解释" kicker="Why" />
          <DecisionSummary decision={currentDecision} />
        </Card>

        <Card>
          <PanelHeader icon={Archive} title="处理结果" kicker="Artifacts" />
          <ClientFeedback artifacts={currentArtifacts} />
          {false ? (
            <>
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              ["tickets", Ticket, "工单"],
              ["emails", Mail, "邮件"],
              ["knowledge", Database, "知识"],
            ]}
          />
          <div className="artifact-grid">
            {(currentArtifacts[tab] || []).length ? (
              currentArtifacts[tab].map((item) => <ArtifactCard key={item.id || item.chunk_id || item.title} item={item} type={tab} />)
            ) : (
              <Empty text="提交请求后展示本次产物" />
            )}
          </div>
            </>
          ) : null}
        </Card>
      </section>
    </main>
  );
}

function AdminConsole({ user, onMessage }) {
  const [metrics, setMetrics] = useState(null);
  const [liveSnapshot, setLiveSnapshot] = useState(null);
  const [approvals, setApprovals] = useState([]);
  const [multiRuns, setMultiRuns] = useState([]);
  const [workflowRuns, setWorkflowRuns] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [users, setUsers] = useState([]);
  const [artifacts, setArtifacts] = useState([]);
  const [retentionPlan, setRetentionPlan] = useState(null);
  const [tab, setTab] = useState("tickets");
  const [busy, setBusy] = useState("");
  const isAdmin = user.role === "admin";

  const refresh = useCallback(async () => {
    const [metricsResult, pendingApprovals, recentApprovals, multiResult, runsResult, jobsResult] = await Promise.all([
      api("/api/metrics/summary"),
      api("/api/approvals?status=pending&limit=20").catch(() => []),
      api("/api/approvals?limit=20").catch(() => []),
      api("/api/multi-agent/runs?limit=8"),
      api("/api/runs?limit=10"),
      api("/api/jobs?limit=12"),
    ]);
    setMetrics(metricsResult);
    setApprovals(dedupeBy([...pendingApprovals, ...recentApprovals], "id"));
    setJobs(jobsResult);
    setMultiRuns(await Promise.all(multiResult.slice(0, 5).map((run) => api(`/api/multi-agent/runs/${run.id}`))));
    setWorkflowRuns(await Promise.all(runsResult.slice(0, 6).map((run) => api(`/api/runs/${run.id}`))));
    if (isAdmin) {
      setUsers(await api("/api/users?limit=50").catch(() => []));
      setRetentionPlan(await api("/api/admin/retention/plan").catch(() => null));
    }
  }, [isAdmin]);

  const loadArtifacts = useCallback(async () => {
    const endpoints = {
      customers: "/api/customers?limit=50",
      tickets: "/api/tickets?limit=20",
      emails: "/api/emails?limit=20",
      knowledge: "/api/knowledge?limit=20",
      audit: "/api/audit-logs?limit=50",
      outbox: "/api/admin/external-outbox?limit=50",
    };
    setArtifacts(await api(endpoints[tab]).catch(() => []));
  }, [tab]);

  const refreshAdmin = useCallback(async () => {
    await Promise.all([refresh(), loadArtifacts()]);
  }, [refresh, loadArtifacts]);

  const liveStatus = useEventStream(Boolean(user), (snapshot) => {
    setLiveSnapshot(snapshot);
    refreshAdmin().catch((err) => onMessage(err.message));
  });

  useEffect(() => {
    refresh().catch((err) => onMessage(err.message));
  }, [refresh, onMessage]);

  useEffect(() => {
    loadArtifacts().catch((err) => onMessage(err.message));
  }, [loadArtifacts, onMessage]);

  useAutoRefresh(
    () => refreshAdmin().catch((err) => onMessage(err.message)),
    2500,
    !busy,
  );

  useRefreshSignal(() => refreshAdmin().catch((err) => onMessage(err.message)));

  useEffect(() => {
    const onFocus = () => refreshAdmin().catch((err) => onMessage(err.message));
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refreshAdmin, onMessage]);

  const adminAction = async (key, task, success) => {
    setBusy(key);
    try {
      await task();
      onMessage(success);
      await refresh();
      await loadArtifacts();
      emitRefreshSignal("admin_action");
    } catch (err) {
      onMessage(err.message);
    } finally {
      setBusy("");
    }
  };

  const decideApproval = (approvalId, approved) =>
    adminAction(
      approvalId,
      () =>
        api(`/api/approvals/${approvalId}/decide`, {
          method: "POST",
          body: JSON.stringify({
            approved,
            decided_by: user.id,
            reason: approved ? "后台审批通过" : "后台审批拒绝",
          }),
        }),
      approved ? "审批已通过" : "审批已拒绝",
    );

  const checkRag = () =>
    adminAction(
      "rag",
      async () => {
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
        const citations = response.result?.citations || [];
        onMessage(`RAG 可用，citations=${citations.length}`);
      },
      "RAG 检查完成",
    );

  const clearHistory = () => {
    if (!window.confirm("确认清理轨迹历史？用户和知识库会保留。")) return;
    adminAction("clear", () => api("/api/admin/clear-history", { method: "POST" }), "轨迹已清理");
  };

  const seedData = () => {
    if (!window.confirm("确认重置演示数据？")) return;
    adminAction("seed", () => api("/api/admin/seed?reset=true", { method: "POST" }), "演示数据已重置");
  };

  const retryJob = (jobId) =>
    adminAction("job", () => api(`/api/jobs/${jobId}/retry`, { method: "POST" }), "任务已重新入队");

  const applyRetentionPlan = () => {
    const matched = retentionPlan?.policies?.reduce((sum, policy) => sum + Number(policy.matched_count || 0), 0) || 0;
    if (!window.confirm(`Apply data retention now? Matched records: ${matched}`)) return;
    adminAction("retention", () => api("/api/admin/retention/apply", { method: "POST" }), "Data retention applied");
  };

  const replay = (runId) =>
    adminAction(
      `replay-${runId}`,
      () => api("/api/trace-replay", { method: "POST", body: JSON.stringify({ source_run_id: runId }) }),
      "Replay 已完成",
    );

  return (
    <main className="admin-shell">
      <Card className="admin-toolbar">
        <div className="toolbar-left">
          <PanelHeader icon={ShieldCheck} title="后台控制台" kicker={`${isAdmin ? "Admin" : user.role} · ${statusLabel(liveStatus)}`} />
        </div>
        <div className="toolbar-actions">
          <IconButton icon={RefreshCw} label="刷新" variant="secondary" onClick={() => refresh().then(loadArtifacts)} />
          <IconButton icon={SearchCheck} label="检查 RAG" variant="secondary" disabled={busy === "rag" || !isAdmin} onClick={checkRag} />
          <IconButton icon={RotateCcw} label="清理轨迹" variant="secondary" disabled={busy === "clear" || !isAdmin} onClick={clearHistory} />
          <IconButton icon={Database} label="重置数据" variant="danger" disabled={busy === "seed" || !isAdmin} onClick={seedData} />
        </div>
      </Card>

      <section className="admin-grid">
        <Card>
          <PanelHeader icon={Gauge} title="运行指标" kicker={liveSnapshot?.server_time ? "Live Ops" : "Ops"} />
          <MetricGrid metrics={metrics} />
        </Card>
        <Card className="span-3 approval-panel">
          <PanelHeader icon={ShieldCheck} title="审批队列" kicker="HITL" />
          <div className="approval-list">
            {approvals.length ? (
              approvals.map((approval) => (
                <ApprovalCard key={approval.id} approval={approval} busy={busy === approval.id} onDecide={decideApproval} />
              ))
            ) : (
              <Empty text="暂无审批项" />
            )}
          </div>
        </Card>
        <Card className="span-2">
          <PanelHeader icon={GitBranch} title="多智能体轨迹" kicker="Agent Trace" />
          <div className="item-list trace">
            {multiRuns.length ? multiRuns.map((run) => <RunCard key={run.id} run={run} admin onReplay={replay} />) : <Empty text="暂无多智能体轨迹" />}
          </div>
        </Card>
        <Card className="span-2">
          <PanelHeader icon={Workflow} title="工作流轨迹" kicker="Workflow" />
          <div className="item-list trace">
            {workflowRuns.length ? workflowRuns.map((run) => <WorkflowCard key={run.id} run={run} />) : <Empty text="暂无工作流轨迹" />}
          </div>
        </Card>
        <Card>
          <PanelHeader icon={FileClock} title="异步任务" kicker="Jobs" />
          <div className="item-list compact">
            {jobs.length ? jobs.map((job) => <JobCard key={job.id} job={job} onRetry={retryJob} />) : <Empty text="暂无任务" />}
          </div>
        </Card>
        <Card>
          <PanelHeader icon={Users} title="用户" kicker="Accounts" />
          <div className="item-list compact">
            {users.length ? users.map((account) => <UserCard key={account.id} account={account} />) : <Empty text={isAdmin ? "暂无用户" : "需要管理员权限"} />}
          </div>
        </Card>
        <Card className="span-2">
          <PanelHeader icon={Archive} title="Data Retention" kicker="Governance" />
          <RetentionPanel plan={retentionPlan} busy={busy === "retention"} isAdmin={isAdmin} onApply={applyRetentionPlan} />
        </Card>
        <Card className="span-2">
          <PanelHeader icon={Archive} title="业务产物" kicker="Artifacts" />
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              ["customers", Users, "CRM"],
              ["tickets", Ticket, "工单"],
              ["emails", Mail, "邮件"],
              ["knowledge", Database, "知识"],
              ["audit", Activity, "审计"],
              ["outbox", Archive, "Outbox"],
            ]}
          />
          {tab === "customers" ? <CustomerCreatePanel onCreated={loadArtifacts} onMessage={onMessage} /> : null}
          <div className="artifact-grid">
            {artifacts.length ? artifacts.map((item) => <ArtifactCard key={item.id || item.created_at || item.title} item={item} type={tab} />) : <Empty text="暂无数据" />}
          </div>
        </Card>
      </section>
    </main>
  );
}

function RetentionPanel({ plan, busy, isAdmin, onApply }) {
  const policies = plan?.policies || [];
  const enabled = policies.filter((policy) => policy.enabled);
  const matched = policies.reduce((sum, policy) => sum + Number(policy.matched_count || 0), 0);

  if (!plan) {
    return <Empty text="Retention plan is available to admin users." />;
  }

  return (
    <div className="retention-panel">
      <div className="retention-summary">
        <div>
          <strong>{matched}</strong>
          <span>expired records</span>
        </div>
        <div>
          <strong>
            {enabled.length}/{policies.length}
          </strong>
          <span>enabled policies</span>
        </div>
        <IconButton
          icon={Archive}
          label={busy ? "Applying" : "Apply"}
          variant="secondary"
          disabled={busy || !isAdmin || !enabled.length}
          onClick={onApply}
        />
      </div>
      <div className="retention-list">
        {policies.map((policy) => (
          <div className="retention-row" key={policy.name}>
            <div>
              <strong>{policy.name}</strong>
              <span>{policy.enabled ? `${policy.retention_days} days` : "disabled"}</span>
            </div>
            <div>
              <Status value={policy.enabled ? "enabled" : "disabled"} />
              <small>{Number(policy.matched_count || 0)} matched</small>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function Card({ className = "", children }) {
  return <section className={`card ${className}`}>{children}</section>;
}

function PanelHeader({ icon: Icon, title, kicker }) {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <Icon size={18} />
        <h2>{title}</h2>
      </div>
      {kicker ? <span className="kicker">{kicker}</span> : null}
    </div>
  );
}

function NavButton({ active, icon: Icon, label, onClick }) {
  return (
    <button className={`nav-button ${active ? "active" : ""}`} type="button" onClick={onClick}>
      <Icon size={16} />
      {label}
    </button>
  );
}

function IconButton({ icon: Icon, label, onClick, disabled = false, variant = "primary" }) {
  return (
    <button className={`action-button ${variant}`} type="button" onClick={onClick} disabled={disabled}>
      <Icon size={16} />
      {label}
    </button>
  );
}

function Tabs({ value, onChange, items }) {
  return (
    <div className="tabs">
      {items.map(([id, Icon, label]) => (
        <button className={value === id ? "active" : ""} type="button" key={id} onClick={() => onChange(id)}>
          <Icon size={15} />
          {label}
        </button>
      ))}
    </div>
  );
}

function ScenarioPicker({ scenarios, onPick }) {
  return (
    <div className="scenario-grid">
      {scenarios.map((scenario) => {
        const Icon = scenario.icon;
        const detail = scenario.demonstrates?.slice(0, 2).join(" / ") || scenario.category || "";
        return (
          <button className="scenario-button" type="button" key={scenario.id} onClick={() => onPick(scenario.text || scenario.objective)}>
            <Icon size={16} />
            <span>{scenario.title}</span>
            {detail ? <small>{detail}</small> : null}
          </button>
        );
      })}
    </div>
  );
}

function LiveStatusCard({ status, health, snapshot }) {
  const latest = snapshot?.server_time ? new Date(snapshot.server_time).toLocaleTimeString() : "-";
  const items = [
    ["实时流", statusLabel(status)],
    ["RAG", health?.rag === "connected" ? "已接入" : "未配置"],
    ["工单", health?.ticket_provider || "-"],
    ["邮件", health?.email_provider || "-"],
    ["最近事件", latest],
  ];
  return (
    <Card>
      <PanelHeader icon={Activity} title="运行连接" kicker={status === "connected" ? "Live" : statusLabel(status)} />
      <div className="readiness-list">
        {items.map(([label, value]) => (
          <div className="readiness-row" key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
          </div>
        ))}
      </div>
    </Card>
  );
}

function ProgressTimeline({ steps }) {
  return (
    <div className="progress-timeline">
      {steps.map((step, index) => (
        <div className={`progress-step ${step.state}`} key={step.key}>
          <span className="progress-index">{index + 1}</span>
          <div>
            <strong>{step.title}</strong>
            <p>{step.detail}</p>
          </div>
        </div>
      ))}
    </div>
  );
}

function buildProgressSteps({ busy, run, note, decision, artifacts }) {
  const status = note?.status || (run?.final_summary?.includes("waiting_approval") ? "waiting_approval" : run?.status);
  const hasTicket = Boolean((artifacts.tickets || []).length);
  const hasKnowledge = Boolean(decision?.evidence?.length || (artifacts.knowledge || []).length);
  const hasApproval = Boolean(decision?.approval?.id || status === "waiting_approval");
  const completed = ["completed", "cancelled", "failed", "refused"].includes(status);
  return [
    {
      key: "input",
      title: "接收业务请求",
      detail: busy ? "用户请求已提交，Agent 正在接管。" : run || note ? "请求已进入流程。" : "等待用户提交业务请求。",
      state: busy || run || note ? "done" : "todo",
    },
    {
      key: "plan",
      title: "Supervisor 规划",
      detail: decision ? `识别为 ${decision.category}，负责人 ${decision.owner}。` : "分类、风险和工具路径会在这里生成。",
      state: decision ? "done" : busy ? "active" : "todo",
    },
    {
      key: "rag",
      title: "RAG 查询知识",
      detail: hasKnowledge ? "已命中企业知识或本地政策依据。" : "检索企业知识库，寻找可引用依据。",
      state: hasKnowledge ? "done" : decision ? "active" : "todo",
    },
    {
      key: "risk",
      title: "Risk Agent 判断",
      detail: decision ? `风险等级 ${decision.riskLevel}，${decision.needsApproval ? "需要人工审批。" : "可自动执行。"}` : "判断是否需要审批和禁止动作。",
      state: decision ? "done" : "todo",
    },
    {
      key: "tool",
      title: "Tool Agent 执行",
      detail: hasApproval ? "已暂停在人工审批节点。" : hasTicket ? "已创建工单并同步业务产物。" : "等待调用工单、邮件等业务工具。",
      state: hasApproval ? "waiting" : hasTicket ? "done" : decision ? "active" : "todo",
    },
    {
      key: "audit",
      title: "结果与审计",
      detail: completed ? "流程结束，轨迹和审计已保留。" : status === "waiting_approval" ? "审批后会继续执行并更新结果。" : "流程完成后会生成可追溯记录。",
      state: completed ? "done" : status === "waiting_approval" ? "waiting" : "todo",
    },
  ];
}

function extractWorkflowArtifacts(workflow) {
  const artifacts = { tickets: [], emails: [], knowledge: [] };
  for (const step of workflow.steps || []) {
    const output = step.tool_output || {};
    if (step.tool_name === "create_ticket" && output.id) {
      artifacts.tickets.push(output);
    }
    if (step.tool_name === "query_tickets" && Array.isArray(output.tickets)) {
      artifacts.tickets.push(...output.tickets);
    }
    if (step.node_name === "update_existing_ticket" && output.ticket?.id) {
      artifacts.tickets.push(output.ticket);
    }
    if (step.node_name === "update_existing_ticket" && Array.isArray(output.tickets)) {
      artifacts.tickets.push(...output.tickets);
    }
    if (step.tool_name === "draft_email" && output.to_address) {
      artifacts.emails.push({
        id: `draft-${step.id}`,
        ...output,
        provider: "draft",
      });
    }
    if (step.tool_name === "send_email" && output.id) {
      artifacts.emails.push(output);
    }
    if (step.tool_name === "query_enterprise_rag") {
      artifacts.knowledge.push(...normalizeKnowledgeItemsRich(output.results || output.citations || [], "enterprise_rag"));
    }
    if (step.tool_name === "search_knowledge") {
      artifacts.knowledge.push(...normalizeKnowledgeItemsRich(output.results || [], "local_policy"));
    }
  }
  return {
    tickets: dedupeBy(artifacts.tickets, "id"),
    emails: dedupeBy(artifacts.emails, "id"),
    knowledge: dedupeBy(artifacts.knowledge, "id").slice(0, 6),
  };
}

function extractWorkflowDecision(workflow) {
  const steps = workflow.steps || [];
  const stepByNode = (nodeName) => steps.find((step) => step.node_name === nodeName)?.tool_output || {};
  const stepByTool = (toolName) => steps.find((step) => step.tool_name === toolName)?.tool_output || {};
  const plan = stepByNode("plan");
  const guard = stepByNode("guard");
  const createdTicket = stepByTool("create_ticket");
  const ticketQuery = stepByTool("query_tickets");
  const ticketUpdate = stepByNode("update_existing_ticket");
  const ticket =
    createdTicket.id
      ? createdTicket
      : ticketUpdate.ticket?.id
        ? ticketUpdate.ticket
        : ticketUpdate.tickets?.[0]?.id
          ? ticketUpdate.tickets[0]
          : ticketQuery.tickets?.[0] || {};
  const approval = stepByTool("request_approval");
  const rag = stepByTool("query_enterprise_rag");
  const local = stepByTool("search_knowledge");
  const draft = stepByTool("draft_email");
  const sent = stepByTool("send_email") || stepByNode("approval_execute_email");
  const category = plan.category || workflow.category || "unknown";
  const riskLevel = plan.risk_level || workflow.risk_level || "unknown";
  const needsApproval = Boolean(plan.needs_approval || workflow.needs_approval);
  const refused = workflow.status === "refused" || guard.allowed === false;

  return {
    status: workflow.status,
    category,
    workflowType: plan.workflow_type,
    riskLevel,
    owner: ticket.owner_department || plan.recommended_owner || ownerForCategory(category),
    amount: plan.amount,
    recipient: plan.recipient_email || draft.to_address || sent.to_address,
    approvalChain: plan.approval_chain || [],
    reason: refused ? workflow.final_answer || guard.message : plan.reason || workflow.final_answer,
    strategy: decisionStrategy({ workflow, plan, ticket, ticketQuery, ticketUpdate, approval, draft, sent, refused }),
    allowedActions: allowedActions({ ticket, ticketQuery, ticketUpdate, approval, draft, sent, workflow, plan }),
    blockedActions: blockedActions({ category, riskLevel, refused, objective: workflow.objective || "", plan }),
    evidence: decisionEvidenceRich(rag, local),
    ticket,
    approval,
    ragAvailable: Boolean(rag.available),
    ragSufficiency: rag.available ? (rag.can_answer ? "sufficient" : "partial") : "fallback",
    retrievedChunkCount: Array.isArray(rag.retrieved_chunks) ? rag.retrieved_chunks.length : 0,
    citationCount: Array.isArray(rag.citations) ? rag.citations.length : 0,
    needsApproval,
  };
}

function ownerForCategory(category) {
  const owners = {
    security: "Security",
    access_request: "IT Access",
    procurement: "Procurement",
    incident: "SRE",
    remote_work: "People Ops",
    refund: "Customer Success",
    complaint: "Customer Success",
    communication: "Customer Success",
  };
  return owners[category] || "Business Ops";
}

function decisionStrategy({ workflow, plan, ticket, ticketQuery = {}, ticketUpdate = {}, approval, draft, sent, refused }) {
  const flowCategory = workflow.category || plan.category;
  if (!refused && flowCategory === "ticket_query") {
    return ticketQuery.message || workflow.final_answer || "已按当前用户权限查询现有工单，没有创建新工单。";
  }
  if (!refused && flowCategory === "ticket_update") {
    return ticketUpdate.message || workflow.final_answer || "已按当前用户权限修改现有工单，并同步到外部工单系统。";
  }
  if (refused) return "拒绝执行危险指令，未创建工单、未发送邮件。";
  if (workflow.status === "waiting_approval") {
    if (!ticket.id) return "已生成审批草案并暂停执行：审批通过前不会创建外部工单、不会发送邮件。";
    if (draft.to_address) return "已暂停在审批节点；审批通过后才会发送邮件并更新工单状态。";
    return "已暂停在审批节点；负责人确认后才会继续创建或更新业务工单。";
  }
  if (sent.id) return "审批已通过，系统已继续执行并记录真实邮件发送结果。";
  if (ticket.id && !plan.needs_approval) return "低风险请求自动处理：查询知识、创建外部工单并完成状态更新。";
  if (approval.id) return "审批动作已处理，系统保留了审批记录和后续状态。";
  return workflow.final_answer || "系统已根据请求完成当前可执行动作。";
}

function allowedActions({ ticket, ticketQuery = {}, ticketUpdate = {}, approval, draft, sent, workflow, plan }) {
  const flowCategory = workflow.category || plan.category;
  if (flowCategory === "ticket_query") return [`Query existing tickets: ${ticketQuery.count || 0}`];
  if (flowCategory === "ticket_update") {
    return [ticketUpdate.ok ? `Update existing ticket: ${ticket.external_id || ticket.id || "-"}` : "Ticket update was not applied"];
  }
  const actions = [];
  for (const action of plan.auto_actions || []) actions.push(action);
  if (ticketQuery.count !== undefined) actions.push(`查询现有工单 ${ticketQuery.count} 个`);
  if (ticketUpdate.ok) actions.push(`修改现有工单 ${ticket.external_id || ticket.id}`);
  if (ticket.id) actions.push(`创建外部工单 ${ticket.external_id || ticket.id}`);
  if (draft.to_address) actions.push(`生成邮件草稿给 ${draft.to_address}`);
  if (approval.id) actions.push(`创建人工审批 ${approval.id}`);
  if (sent.id) actions.push(`发送邮件 ${sent.id}`);
  if (workflow.status === "refused") actions.push("记录拒绝原因");
  if (!actions.length) actions.push("记录流程轨迹");
  return actions;
}

function blockedActions({ category, riskLevel, refused, objective, plan }) {
  if (category === "ticket_query") return ["No new ticket creation", "No cross-department data exposure"];
  if (category === "ticket_update") return ["No cross-role ticket changes", "No audit history rewrite"];
  if (Array.isArray(plan.blocked_actions) && plan.blocked_actions.length) return plan.blocked_actions;
  if (refused) return ["不绕过审批", "不关闭审计", "不执行破坏性操作"];
  if (category === "security") return ["不直接删除数据", "不直接重置权限", "不绕过安全审批"];
  if (category === "access_request") return ["不绕过身份校验", "不直接授予高权限", "不跳过负责人审批"];
  if (category === "remote_work") return ["不修改薪资", "不自动开通系统权限"];
  if (category === "refund") return ["不直接承诺退款", "不绕过金额审批", "不未授权发送对外邮件"];
  if (category === "procurement") return ["不直接下单付款", "不跳过预算审批"];
  if (riskLevel === "high") return ["不自动执行高风险动作", "不绕过人工确认"];
  if (/删除|delete|drop|绕过|bypass/i.test(objective)) return ["不执行破坏性指令", "不绕过审计"];
  return ["无高风险阻断动作"];
}

function decisionEvidence(rag, local) {
  const items = [];
  for (const item of rag.results || []) {
    items.push({ title: item.title || item.document_name || "企业 RAG 依据", source: "RAG", content: item.snippet || item.evidence_snippet || item.content || "" });
  }
  for (const item of local.results || []) {
    items.push({ title: item.title || "本地政策", source: "Policy", content: item.snippet || item.content || "" });
  }
  return dedupeBy(items, "title").slice(0, 3);
}

function normalizeKnowledgeItems(items, source) {
  return items.map((item, index) => ({
    id: item.article_id || item.chunk_id || `${source}-${index}`,
    title: item.title || item.document_name || item.section_title || "知识依据",
    category: item.category || item.source || source,
    tags: item.section_title || item.document_id || source,
    content: item.snippet || item.evidence_snippet || item.content || item.answer || "",
  }));
}

function decisionEvidenceRich(rag, local) {
  const items = [];
  for (const item of rag.results || []) {
    items.push(normalizeKnowledgeItemRich(item, "RAG", items.length));
  }
  for (const item of local.results || []) {
    items.push(normalizeKnowledgeItemRich(item, "Policy", items.length));
  }
  return dedupeBy(items, "title").slice(0, 4);
}

function normalizeKnowledgeItemsRich(items, source) {
  return items.map((item, index) => normalizeKnowledgeItemRich(item, source, index));
}

function normalizeKnowledgeItemRich(item, source, index = 0) {
  return {
    id: item.article_id || item.chunk_id || `${source}-${index}`,
    title: item.title || item.document_name || item.section_title || "Knowledge evidence",
    source: item.source || item.category || source,
    category: item.category || item.source || source,
    tags: item.section_title || item.document_id || source,
    documentId: item.document_id,
    sectionTitle: item.section_title,
    pageNumber: item.page_number,
    score: item.score,
    content: item.snippet || item.evidence_snippet || item.content || item.answer || "",
  };
}

function dedupeBy(items, key) {
  const seen = new Set();
  return items.filter((item) => {
    const value = item[key] || JSON.stringify(item);
    if (seen.has(value)) return false;
    seen.add(value);
    return true;
  });
}

function MetricGrid({ metrics, compact = false }) {
  const totals = metrics?.totals || {};
  const values = compact
    ? [
        ["待审批", totals.pending_approvals ?? 0],
        ["外部工单", totals.external_tickets ?? totals.tickets ?? 0],
        ["已发邮件", totals.sent_emails ?? 0],
        ["质量分", Number(totals.avg_critic_score || 0).toFixed(0)],
      ]
    : [
        ["工作流", totals.runs ?? 0],
        ["待审批", totals.pending_approvals ?? 0],
        ["失败", totals.failed_runs ?? 0],
        ["外部工单", totals.external_tickets ?? totals.tickets ?? 0],
        ["邮件发送", `${totals.sent_emails ?? 0}/${totals.emails ?? 0}`],
        ["质量分", Number(totals.avg_critic_score || 0).toFixed(0)],
        ["平均耗时", `${Number(totals.avg_latency_ms || 0).toFixed(0)}ms`],
      ];
  return (
    <div className={`metrics ${compact ? "compact" : ""}`}>
      {values.slice(0, compact ? 4 : 6).map(([label, value]) => (
        <div className="metric" key={label}>
          <strong>{value}</strong>
          <span>{label}</span>
        </div>
      ))}
    </div>
  );
}

function ClientFeedback({ artifacts = {} }) {
  const tickets = artifacts.tickets || [];
  const emails = artifacts.emails || [];
  const knowledge = artifacts.knowledge || [];
  if (!tickets.length && !emails.length && !knowledge.length) {
    return <Empty text="提交请求后，这里只展示可交付给用户的结果反馈" />;
  }
  return (
    <div className="client-feedback-list">
      {tickets.slice(0, 3).map((ticket) => (
        <article className="client-feedback-item ticket" key={ticket.id || ticket.external_id}>
          <div>
            <span>外部工单</span>
            <strong>{ticket.external_id || ticket.id}</strong>
          </div>
          {ticket.external_url ? (
            <a href={ticket.external_url} target="_blank" rel="noreferrer">
              打开
            </a>
          ) : null}
        </article>
      ))}
      {emails.slice(0, 2).map((email) => (
        <article className="client-feedback-item email" key={email.id || email.to_address}>
          <div>
            <span>邮件</span>
            <strong>{email.status || email.provider || "draft"}</strong>
            <small>{email.to_address}</small>
          </div>
        </article>
      ))}
      {knowledge.length ? (
        <article className="client-feedback-item knowledge">
          <div>
            <span>知识依据</span>
            <strong>{knowledge.length} 条已记录</strong>
            <small>详细引用保留在后台轨迹中</small>
          </div>
        </article>
      ) : null}
    </div>
  );
}

function UserResult({ run, note, decision, artifacts = {} }) {
  if (!run && !note) {
    return <Empty text="提交请求后，这里会展示本次处理结果" />;
  }
  if (run) {
    const workflowStatus = note?.status || (run.final_summary?.includes("waiting_approval") ? "waiting_approval" : run.status);
    const waitingApproval = workflowStatus === "waiting_approval";
    const failed = ["failed", "refused"].includes(workflowStatus);
    const summary = parseRunSummary(run);
    const tickets = artifacts.tickets || [];
    const emails = artifacts.emails || [];
    const knowledge = artifacts.knowledge || [];
    const primaryTicket = tickets[0] || decision?.ticket || {};
    const category = decision?.category || summary.category || "-";
    const risk = decision?.riskLevel || summary.risk || "-";
    const owner = decision?.owner || primaryTicket.owner_department || "-";
    const quality = Number(run.critic_score || summary.critic_score || 0).toFixed(0);
    const duration = run.latency_ms ? `${run.latency_ms}ms` : "-";
    const answer = note?.detail || run.final_answer || formatRunSummary(run);
    const allowedActions = (decision?.allowedActions || []).slice(0, 4);
    const resultClass = failed ? "bad" : waitingApproval ? "warn" : "ok";
    const resultTitle = failed ? "处理未完成" : waitingApproval ? "等待审批" : "处理完成";
    const resultHint = failed
      ? "本次请求被拒绝或执行失败，未继续执行高风险动作。"
      : waitingApproval
        ? "流程已暂停在人工审批节点，审批通过后才会继续执行后续动作。"
        : "Agent 已完成本次可执行动作，相关工单、邮件和知识依据已记录。";
    return (
      <article className={`user-result-panel ${resultClass}`}>
        <header className="result-hero">
          <div className="result-title">
            <span className="result-eyebrow">{category}</span>
            <strong>{resultTitle}</strong>
            <p>{short(run.objective, 132)}</p>
          </div>
          <Status value={workflowStatus} />
        </header>

        <dl className="result-kpis">
          <div>
            <dt>风险</dt>
            <dd>{risk}</dd>
          </div>
          <div>
            <dt>质量分</dt>
            <dd>{quality}</dd>
          </div>
          <div>
            <dt>耗时</dt>
            <dd>{duration}</dd>
          </div>
          <div>
            <dt>产物</dt>
            <dd>{tickets.length}/{emails.length}/{knowledge.length}</dd>
          </div>
        </dl>

        <section className="result-answer" aria-label="本次处理摘要">
          <ResultNarrative
            title={resultTitle}
            summary={resultHint}
            answer={answer}
            category={category}
            ticket={primaryTicket}
            actions={allowedActions}
          />
        </section>

        <dl className="result-details">
          <div>
            <dt>运行编号</dt>
            <dd>{run.id}</dd>
          </div>
          <div>
            <dt>负责人</dt>
            <dd>{owner}</dd>
          </div>
          <div>
            <dt>工单</dt>
            <dd>
              {primaryTicket.external_url ? (
                <a href={primaryTicket.external_url} target="_blank" rel="noreferrer">
                  {primaryTicket.external_id || primaryTicket.id}
                </a>
              ) : (
                primaryTicket.external_id || primaryTicket.id || "-"
              )}
            </dd>
          </div>
          <div>
            <dt>工单状态</dt>
            <dd>{primaryTicket.status || "-"}</dd>
          </div>
        </dl>

        {allowedActions.length ? (
          <section className="result-actions" aria-label="Agent 执行动作">
            <span>已执行</span>
            <ul>
              {allowedActions.map((action) => (
                <li key={action}>{action}</li>
              ))}
            </ul>
          </section>
        ) : null}
      </article>
    );
    return (
      <article className="record-card user-result">
        <div className="record-head">
          <strong>{short(run.objective, 120)}</strong>
          <Status value={workflowStatus} />
        </div>
        <div className="muted">{run.id} · critic={Number(run.critic_score || 0).toFixed(0)}</div>
        <p>{note?.detail || formatRunSummary(run)}</p>
        <div className={failed ? "result-banner bad" : waitingApproval ? "result-banner warn" : "result-banner ok"}>
          {failed
            ? "处理失败，本次没有生成新的业务产物。请查看后台轨迹里的失败原因后重新提交。"
            : waitingApproval
              ? "已生成审批草案并暂停执行。审批通过后才会创建工单、发送邮件或继续高风险动作。"
              : "请求已处理完成，下方可以查看生成的工单、邮件和知识依据。"}
        </div>
      </article>
    );
  }
  return (
    <article className="record-card user-result">
      <div className="record-head">
        <strong>{note.title}</strong>
        <Status value={note.status} />
      </div>
      <p>{note.detail}</p>
    </article>
  );
}

function ResultNarrative({ title, summary, answer, category, ticket = {}, actions = [] }) {
  const narrative = parseResultNarrative(answer);
  const ticketRef = ticket.external_id || ticket.id;
  return (
    <div className="result-narrative">
      <header className="result-section-header">
        <span>处理摘要</span>
        <h3>{title || "本次结果"}</h3>
      </header>

      <p className="result-lead">
        <strong>{summary}</strong>
      </p>

      {narrative.lead ? <p>{narrative.lead}</p> : null}

      {narrative.bullets.length ? (
        <section className="result-subsection">
          <h4>关键结果</h4>
          <ul className="rich-result-list">
            {narrative.bullets.map((item, index) => {
              const parsed = splitResultReference(item);
              return (
                <li key={`${parsed.ref || "item"}-${index}`}>
                  {parsed.ref ? <strong>{parsed.ref}</strong> : null}
                  <span>{parsed.text}</span>
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}

      {narrative.paragraphs.length ? (
        <section className="result-subsection">
          <h4>详细说明</h4>
          {narrative.paragraphs.map((paragraph, index) => (
            <p key={`${paragraph}-${index}`}>{paragraph}</p>
          ))}
        </section>
      ) : null}

      <section className="result-subsection">
        <h4>业务对象</h4>
        <dl className="result-mini-facts">
          <div>
            <dt>类型</dt>
            <dd>{category || "-"}</dd>
          </div>
          <div>
            <dt>工单</dt>
            <dd>{ticketRef || "-"}</dd>
          </div>
          <div>
            <dt>优先级</dt>
            <dd>{ticket.priority || "-"}</dd>
          </div>
        </dl>
      </section>

      {actions.length ? (
        <section className="result-subsection">
          <h4>执行动作</h4>
          <ul className="rich-result-list compact">
            {actions.map((action) => (
              <li key={action}>
                <span>{action}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

function parseResultNarrative(answer) {
  const text = String(answer || "").replace(/\s+/g, " ").trim();
  if (!text) return { lead: "", bullets: [], paragraphs: [] };
  const colonIndex = firstColonIndex(text);
  if (colonIndex >= 0) {
    const lead = text.slice(0, colonIndex + 1);
    const rest = text.slice(colonIndex + 1).trim();
    const bullets = rest.includes(" | ")
      ? rest.split(" | ").map((item) => item.trim()).filter(Boolean)
      : rest.split(/[；;]/).map((item) => item.trim()).filter(Boolean);
    return { lead, bullets, paragraphs: [] };
  }
  const paragraphs = text
    .split(/(?<=[。.!?])\s+|[；;]/)
    .map((item) => item.trim())
    .filter(Boolean);
  return { lead: "", bullets: [], paragraphs: paragraphs.length ? paragraphs : [text] };
}

function firstColonIndex(text) {
  const chinese = text.indexOf("：");
  const ascii = text.indexOf(":");
  if (chinese < 0) return ascii;
  if (ascii < 0) return chinese;
  return Math.min(chinese, ascii);
}

function splitResultReference(value) {
  const text = String(value || "").trim();
  const match = text.match(/\b(EXT-\d+|ticket_[a-z0-9]+)\b/i);
  if (!match) return { ref: "", text };
  const before = text.slice(0, match.index).trim();
  const after = text.slice((match.index || 0) + match[0].length).trim();
  return { ref: match[0], text: [before, after].filter(Boolean).join(" ") || text };
}

function DecisionSummary({ decision }) {
  if (!decision) {
    return <Empty text="提交请求后，这里会解释 Agent 为什么这样处理" />;
  }
  const badges = [
    ["分类", decision.category],
    ["流程", decision.workflowType || "-"],
    ["风险", decision.riskLevel],
    ["负责人", decision.owner],
    ["审批", decision.needsApproval ? "需要" : "不需要"],
  ];
  if (decision.amount) badges.splice(2, 0, ["金额", `${decision.amount} 元`]);
  if (decision.recipient) badges.push(["收件人", decision.recipient]);
  if (decision.approvalChain?.length) badges.push(["审批链", decision.approvalChain.join(" -> ")]);

  return (
    <div className="decision-summary">
      <div className="decision-badges">
        {badges.map(([label, value]) => (
          <div className="decision-badge" key={label}>
            <span>{label}</span>
            <strong>{value || "-"}</strong>
          </div>
        ))}
      </div>

      <div className="decision-explain">
        <div>
          <h3>处理策略</h3>
          <p>{decision.strategy}</p>
        </div>
        <div>
          <h3>判断依据</h3>
          <p>{decision.reason || "系统根据分类、风险等级和工具结果决定后续动作。"}</p>
        </div>
      </div>

      <div className="decision-columns">
        <div className="decision-list allowed">
          <h3>允许执行</h3>
          {decision.allowedActions.map((action) => (
            <span key={action}>
              <Check size={14} />
              {action}
            </span>
          ))}
        </div>
        <div className="decision-list blocked">
          <h3>不会直接做</h3>
          {decision.blockedActions.map((action) => (
            <span key={action}>
              <ShieldCheck size={14} />
              {action}
            </span>
          ))}
        </div>
      </div>

      <div className="decision-evidence">
        <div className="decision-evidence-head">
          <h3>知识依据</h3>
          <span>{decision.ragAvailable ? `RAG citations=${decision.citationCount}` : "本地政策兜底"}</span>
        </div>
        {decision.evidence.length ? (
          decision.evidence.map((item) => (
            <div className="evidence-line" key={`${item.source}-${item.title}`}>
              <strong>{item.source} · {item.title}</strong>
              <div className="evidence-meta">
                {item.score !== undefined ? <span>score {formatScore(item.score)}</span> : null}
                {item.sectionTitle ? <span>{item.sectionTitle}</span> : null}
                {item.pageNumber ? <span>page {item.pageNumber}</span> : null}
                {item.documentId ? <span>{short(item.documentId, 22)}</span> : null}
              </div>
              <p>{short(item.content, 150)}</p>
            </div>
          ))
        ) : (
          <p className="muted">暂无可展示的知识依据。</p>
        )}
      </div>
    </div>
  );
}

function Status({ value }) {
  return <span className={`status ${value || ""}`}>{value || "unknown"}</span>;
}

function RunCard({ run, admin = false, onReplay }) {
  const agents = run.messages || [];
  const tasks = run.tasks || [];
  const handoffs = run.handoffs || [];
  return (
    <article className="record-card">
      <div className="record-head">
        <strong>{short(run.objective, 110)}</strong>
        <Status value={run.status} />
      </div>
      <div className="muted">
        {run.id} · critic={Number(run.critic_score || 0).toFixed(0)} · {run.latency_ms || 0}ms
      </div>
      <p>{formatRunSummary(run)}</p>
      {tasks.length ? (
        <div className="agent-steps">
          <div className="muted">任务图 · {tasks.length} tasks · {handoffs.length} handoffs</div>
          {tasks.map((task) => (
            <div className="agent-step" key={task.id}>
              <div>
                <code>{task.task_key}</code>
                <Status value={task.status} />
              </div>
              <span>
                {task.assigned_agent}
                {(task.dependencies || []).length ? ` ← ${(task.dependencies || []).join(", ")}` : ""}
              </span>
            </div>
          ))}
        </div>
      ) : null}
      <div className="agent-steps">
        {agents.map((message) => (
          <div className="agent-step" key={message.id}>
            <div>
              <code>{message.agent_name}</code>
              <Status value={message.status} />
            </div>
            <span>{describeAgentContent(message.content)}</span>
          </div>
        ))}
      </div>
      {admin && onReplay ? (
        <div className="button-row">
          <IconButton icon={RotateCcw} label="Replay" variant="ghost" onClick={() => onReplay(run.id)} />
        </div>
      ) : null}
    </article>
  );
}

function WorkflowCard({ run }) {
  return (
    <article className="record-card">
      <div className="record-head">
        <strong>{short(run.objective, 110)}</strong>
        <Status value={run.status} />
      </div>
      <div className="muted">
        {run.id} · {run.category || "-"} · risk={run.risk_level || "-"}
      </div>
      <p>{run.final_answer || run.refusal_reason || "暂无最终输出"}</p>
      <div className="workflow-steps">
        {(run.steps || []).map((step) => (
          <div className="workflow-step" key={step.id}>
            <span>#{step.step_index}</span>
            <code>{step.node_name}</code>
            <small>{step.tool_name || step.action_type}</small>
          </div>
        ))}
      </div>
    </article>
  );
}

function ApprovalCard({ approval, busy, onDecide }) {
  const summary = approvalBusinessSummary(approval);
  return (
    <article className="record-card approval-card">
      <div className="approval-card-head">
        <div>
          <span className="approval-eyebrow">{summary.actionLabel}</span>
          <strong>{summary.title}</strong>
          <small>{approval.id} · run={approval.run_id}</small>
        </div>
        <Status value={approval.status} />
      </div>

      <div className="approval-card-body">
        <div className="approval-main">
          <section className="approval-section">
            <h3>
              <ClipboardList size={15} />
              用户请求
            </h3>
            <p className="approval-request">{summary.objective}</p>
          </section>

          <section className="approval-ticket-preview">
            <div className="approval-ticket-head">
              <span>拟创建工单</span>
              <strong>{summary.ticket.ref}</strong>
            </div>
            <h3>
              <BriefcaseBusiness size={15} />
              {summary.ticket.title}
            </h3>
            <p>{summary.ticket.description}</p>
            <dl className="approval-ticket-fields">
              {summary.ticket.fields.map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{value || "-"}</dd>
                </div>
              ))}
            </dl>
          </section>
        </div>

        <aside className="approval-side">
          <section className="approval-section">
            <h3>
              <ShieldCheck size={15} />
              审批判断
            </h3>
            <div className="approval-summary">
              {summary.items.map(([label, value]) => (
                <div key={label}>
                  <span>{label}</span>
                  <strong>{value || "-"}</strong>
                </div>
              ))}
            </div>
            <p>{summary.reason}</p>
          </section>

          <section className="approval-section">
            <h3>
              <Database size={15} />
              知识依据
            </h3>
            {summary.evidence.length ? (
              <ul className="approval-evidence">
                {summary.evidence.map((item) => (
                  <li key={`${item.title}-${item.source}`}>
                    <strong>{item.title}</strong>
                    <span>{item.source}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p>暂无可展示的知识依据。</p>
            )}
          </section>

          {approval.status === "pending" ? (
            <div className="approval-actions">
              <IconButton icon={Check} label="通过并创建工单" disabled={busy} onClick={() => onDecide(approval.id, true)} />
              <IconButton icon={X} label="拒绝" variant="danger" disabled={busy} onClick={() => onDecide(approval.id, false)} />
            </div>
          ) : null}
        </aside>
      </div>
    </article>
  );
}

function approvalBusinessSummary(approval) {
  const payload = approval.payload || {};
  const plan = payload.plan || {};
  const email = payload.email || {};
  const proposedTicket = payload.proposed_ticket || {};
  const knowledge = payload.knowledge || [];
  const objective = payload.objective || extractObjectiveFromTicketDescription(proposedTicket.description) || "未记录原始请求";
  const title = `${plan.category || proposedTicket.category || "business"} / ${plan.risk_level || proposedTicket.risk_level || "risk"} 审批`;
  const actionLabel = approvalActionLabel(approval, proposedTicket);
  const ticketRef = payload.ticket_id || (proposedTicket.title ? `DRAFT-${approval.id.slice(-6).toUpperCase()}` : "-");
  const owner = proposedTicket.owner_department || plan.recommended_owner || "-";
  const priority = proposedTicket.priority || plan.priority || "-";
  const workflowType = proposedTicket.workflow_type || plan.workflow_type || "-";
  const category = proposedTicket.category || plan.category || "-";
  const riskLevel = proposedTicket.risk_level || plan.risk_level || "-";
  return {
    title,
    actionLabel,
    objective,
    reason: plan.reason || "该动作需要人工确认后才能继续执行。",
    ticket: {
      ref: ticketRef,
      title: proposedTicket.title || payload.ticket_id || "待审批业务动作",
      description: short(extractObjectiveFromTicketDescription(proposedTicket.description) || proposedTicket.description || objective, 260),
      fields: [
        ["负责人", owner],
        ["优先级", priority],
        ["流程", workflowType],
        ["分类", category],
        ["风险", riskLevel],
        ["状态", proposedTicket.title ? "审批通过后创建" : "待处理"],
      ],
    },
    evidence: knowledge.slice(0, 3).map((item) => ({
      title: item.title || "知识片段",
      source: item.source || item.document_id || item.category || "local policy",
    })),
    items: [
      ["流程", workflowType],
      ["动作", actionLabel],
      ["风险", riskLevel],
      ["负责人", owner],
      ["审批链", (plan.approval_chain || []).join(" -> ") || "-"],
      ["金额", plan.amount ? `${plan.amount} 元` : "-"],
      ["工单", ticketRef],
      ["收件人", email.to_address || plan.recipient_email || "-"],
      ["知识依据", `${knowledge.length || 0} 条`],
    ],
  };
}

function approvalActionLabel(approval, proposedTicket) {
  if (proposedTicket.title) return "审批通过后创建工单";
  if (approval.tool_name === "send_email") return "发送客户邮件";
  if (approval.tool_name === "update_ticket") return "更新工单状态";
  return approval.tool_name || "业务动作";
}

function extractObjectiveFromTicketDescription(description) {
  const text = String(description || "").trim();
  if (!text) return "";
  const match = text.match(/^用户请求：([\s\S]*?)(?:\n\n流程类型：|\n\n检索到的政策依据：|$)/);
  return (match ? match[1] : text).trim();
}

function CustomerCreatePanel({ onCreated, onMessage }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [tier, setTier] = useState("starter");
  const [busy, setBusy] = useState(false);

  const submit = async (event) => {
    event.preventDefault();
    if (!name.trim() || !email.trim()) return;
    setBusy(true);
    try {
      await api("/api/customers", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), email: email.trim(), tier }),
      });
      setName("");
      setEmail("");
      setTier("starter");
      onMessage("客户档案已创建");
      await onCreated();
    } catch (error) {
      onMessage(error.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="ticket-ops customer-create" onSubmit={submit}>
      <input value={name} onChange={(event) => setName(event.target.value)} placeholder="customer name" maxLength={200} />
      <input value={email} onChange={(event) => setEmail(event.target.value)} placeholder="email" type="email" maxLength={320} />
      <select value={tier} onChange={(event) => setTier(event.target.value)}>
        {["starter", "growth", "enterprise", "strategic"].map((value) => <option key={value} value={value}>{value}</option>)}
      </select>
      <button className="action-button" type="submit" disabled={busy || !name.trim() || !email.trim()}>Create customer</button>
    </form>
  );
}

function ArtifactCard({ item, type }) {
  const [ticketStatus, setTicketStatus] = useState(item.status || "open");
  const [ticketOwner, setTicketOwner] = useState(item.owner_department || "Business Ops");
  const [ticketPriority, setTicketPriority] = useState(item.priority || "normal");
  const [ticketComment, setTicketComment] = useState("");
  const [ticketBusy, setTicketBusy] = useState(false);
  const [customerStatus, setCustomerStatus] = useState(item.status || "active");
  const [customerHealth, setCustomerHealth] = useState(item.health_score ?? 100);
  const [customerNote, setCustomerNote] = useState("");

  useEffect(() => {
    setTicketStatus(item.status || "open");
    setTicketOwner(item.owner_department || "Business Ops");
    setTicketPriority(item.priority || "normal");
  }, [item.id, item.status, item.owner_department, item.priority]);

  useEffect(() => {
    setCustomerStatus(item.status || "active");
    setCustomerHealth(item.health_score ?? 100);
  }, [item.id, item.status, item.health_score]);

  const updateTicketFromCard = async () => {
    if (type !== "tickets" || !item.id) return;
    setTicketBusy(true);
    try {
      await api(`/api/tickets/${item.id}/ops`, {
        method: "PATCH",
        body: JSON.stringify({
          status: ticketStatus,
          owner_department: ticketOwner,
          priority: ticketPriority,
          comment: ticketComment || "Updated from admin console.",
        }),
      });
      setTicketComment("");
      emitRefreshSignal("ticket_updated");
    } finally {
      setTicketBusy(false);
    }
  };

  const updateCustomerFromCard = async () => {
    if (type !== "customers" || !item.id) return;
    setTicketBusy(true);
    try {
      await api(`/api/customers/${item.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status: customerStatus, health_score: Number(customerHealth) }),
      });
      if (customerNote.trim()) {
        await api(`/api/customers/${item.id}/interactions`, {
          method: "POST",
          body: JSON.stringify({ summary: customerNote.trim(), interaction_type: "note", channel: "admin-console" }),
        });
      }
      setCustomerNote("");
      emitRefreshSignal("customer_updated");
    } finally {
      setTicketBusy(false);
    }
  };

  if (type === "customers") {
    return (
      <article className="record-card">
        <div className="record-head">
          <strong>{item.name}</strong>
          <Status value={item.status} />
        </div>
        <div className="muted">{item.email} · {item.tier} · {item.owner_department}</div>
        <div className={`ticket-sla ${Number(item.health_score) < 50 ? "danger" : Number(item.health_score) < 75 ? "warn" : "ok"}`}>
          <span>Health</span><strong>{item.health_score}/100</strong>
        </div>
        <div className="ticket-ops">
          <select value={customerStatus} onChange={(event) => setCustomerStatus(event.target.value)}>
            {["active", "onboarding", "at_risk", "inactive", "churned"].map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
          <input type="number" min="0" max="100" value={customerHealth} onChange={(event) => setCustomerHealth(event.target.value)} />
          <input value={customerNote} onChange={(event) => setCustomerNote(event.target.value)} placeholder="add interaction note" />
          <button className="action-button secondary" type="button" disabled={ticketBusy} onClick={updateCustomerFromCard}>Update</button>
        </div>
        {item.tags?.length ? <p>Tags: {item.tags.join(", ")}</p> : null}
        {item.notes ? <p>{short(item.notes, 220)}</p> : null}
      </article>
    );
  }

  if (type === "tickets") {
    return (
      <article className="record-card">
        <div className="record-head">
          <strong>{item.title}</strong>
          <Status value={item.status} />
        </div>
        <div className="muted">
          {item.id} · {item.owner_department} · provider={item.provider || "mock"}
        </div>
        {item.external_id || item.external_url ? (
          <div className="external-line">
            <BriefcaseBusiness size={14} />
            <span>外部工单：{item.external_id || "-"}</span>
            {item.external_url ? (
              <a href={item.external_url} target="_blank" rel="noreferrer">
                打开
              </a>
            ) : null}
          </div>
        ) : null}
        <div className={`ticket-sla ${ticketSla(item).state}`}>
          <span>SLA</span>
          <strong>{ticketSla(item).label}</strong>
        </div>
        <div className="ticket-ops">
          <select value={ticketStatus} onChange={(event) => setTicketStatus(event.target.value)}>
            {["open", "investigating", "waiting_approval", "approved", "rejected", "waiting_customer", "resolved", "closed"].map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
          <select value={ticketPriority} onChange={(event) => setTicketPriority(event.target.value)}>
            {["low", "normal", "high", "urgent"].map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
          <select value={ticketOwner} onChange={(event) => setTicketOwner(event.target.value)}>
            {["Customer Success", "Security", "IT Access", "SRE", "Procurement", "People Ops", "Finance", "Operations", "Business Ops"].map((value) => (
              <option key={value} value={value}>{value}</option>
            ))}
          </select>
          <input value={ticketComment} onChange={(event) => setTicketComment(event.target.value)} placeholder="internal note" />
          <button className="action-button secondary" type="button" disabled={ticketBusy} onClick={updateTicketFromCard}>
            Update
          </button>
        </div>
        <p>{short(item.description, 260)}</p>
      </article>
    );
  }
  if (type === "emails") {
    return (
      <article className="record-card">
        <div className="record-head">
          <strong>{item.subject}</strong>
          <Status value={item.status} />
        </div>
        <div className="muted">
          {item.to_address} · provider={item.provider || "mock"}
          {item.external_message_id ? ` · ${item.external_message_id}` : ""}
        </div>
        {item.error_message ? <p className="error-text">{item.error_message}</p> : null}
        <p>{short(item.body, 260)}</p>
      </article>
    );
  }
  if (type === "outbox") {
    return (
      <article className="record-card">
        <div className="record-head">
          <strong>{item.action_type || item.id}</strong>
          <Status value={item.status} />
        </div>
        <div className="muted">
          {item.id} 路 provider={item.provider || "-"} 路 attempts={item.attempt_count || 0}
        </div>
        {item.target_id ? (
          <div className="external-line">
            <Archive size={14} />
            <span>{item.target_type || "target"}: {item.target_id}</span>
          </div>
        ) : null}
        {item.error_message ? <p className="error-text">{short(item.error_message, 220)}</p> : null}
        <p>{short(JSON.stringify(item.payload || {}, null, 2), 260)}</p>
      </article>
    );
  }
  if (type === "audit") {
    return (
      <article className="record-card">
        <div className="record-head">
          <strong>{item.action}</strong>
          <span className="status">{item.target_type}</span>
        </div>
        <div className="muted">{item.actor} · {item.created_at}</div>
        <pre>{short(JSON.stringify(item.payload || {}, null, 2), 360)}</pre>
      </article>
    );
  }
  return (
    <article className="record-card">
      <div className="record-head">
        <strong>{item.title}</strong>
        <span className="status">{item.category}</span>
      </div>
      <div className="muted">{item.tags}</div>
      <p>{short(item.content, 260)}</p>
    </article>
  );
}

function JobCard({ job, onRetry }) {
  return (
    <article className="record-card">
      <div className="record-head">
        <strong>{short(job.objective, 90)}</strong>
        <Status value={job.status} />
      </div>
      <div className="muted">{job.id} · attempts={job.attempts}/{job.max_attempts}</div>
      {job.error_message ? <p className="error-text">{short(job.error_message, 180)}</p> : null}
      {["failed", "queued"].includes(job.status) ? (
        <div className="button-row">
          <IconButton icon={RotateCcw} label="重试" variant="ghost" onClick={() => onRetry(job.id)} />
        </div>
      ) : null}
    </article>
  );
}

function UserCard({ account }) {
  return (
    <article className="record-card">
      <div className="record-head">
        <strong>{account.display_name}</strong>
        <Status value={account.role} />
      </div>
      <div className="muted">{account.id} · {account.department}</div>
    </article>
  );
}

function Empty({ text }) {
  return (
    <div className="empty">
      <ClipboardList size={18} />
      {text}
    </div>
  );
}

function ticketSla(ticket) {
  if (ticket.sla) {
    if (ticket.sla.breached) return { label: "breached", state: "danger" };
    if (ticket.sla.state === "met") return { label: "met", state: "ok" };
    if (ticket.sla.due_at) {
      const due = new Date(ticket.sla.due_at);
      return { label: Number.isNaN(due.getTime()) ? "active" : `due ${due.toLocaleString()}`, state: "ok" };
    }
  }
  const status = String(ticket.status || "open");
  if (["resolved", "closed", "rejected"].includes(status)) return { label: "done", state: "ok" };
  if (status === "waiting_approval") return { label: "approval", state: "warn" };
  return { label: "not set", state: "warn" };
}

function formatScore(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number <= 1 ? `${Math.round(number * 100)}%` : number.toFixed(2);
}

function short(value, limit = 160) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit - 3)}...` : text;
}

function statusLabel(status) {
  const labels = {
    connected: "已连接",
    connecting: "连接中",
    reconnecting: "重连中",
    disabled: "未启用",
  };
  return labels[status] || status || "-";
}

function parseRunSummary(run) {
  const summary = run?.final_summary || "";
  if (!summary.includes("=")) return {};
  return Object.fromEntries(
    summary
      .split(";")
      .map((part) => part.trim().split("="))
      .filter((pair) => pair.length >= 2)
      .map(([key, ...rest]) => [key, rest.join("=")]),
  );
}

function formatRunSummary(run) {
  const summary = run.final_summary || "";
  if (!summary.includes("=")) return summary || "等待最终总结";
  const fields = Object.fromEntries(
    summary
      .split(";")
      .map((part) => part.trim().split("="))
      .filter((pair) => pair.length >= 2)
      .map(([key, ...rest]) => [key, rest.join("=")]),
  );
  const category = fields.category || "-";
  const risk = fields.risk || "-";
  const decision = fields.decision || "-";
  const score = fields.critic_score || run.critic_score || "-";
  return `类型 ${category} · 风险 ${risk} · 决策 ${decision} · 质量 ${score}`;
}

function describeAgentContent(content) {
  if (!content || typeof content !== "object") return "无内容";
  if (Array.isArray(content.similar_cases)) return `相似案例 ${content.similar_cases.length} 条`;
  if (content.plan) {
    const tools = Array.isArray(content.plan.proposed_tools) ? content.plan.proposed_tools.length : 0;
    return short(`规划：${content.plan.category || "-"} · 风险 ${content.plan.risk_level || "-"} · 工具 ${tools} 个`, 140);
  }
  if (content.enterprise_rag || content.local_policy) {
    const rag = content.enterprise_rag || {};
    const local = content.local_policy || {};
    const citationCount = Array.isArray(rag.citations) ? rag.citations.length : 0;
    const localCount = Array.isArray(local.results) ? local.results.length : 0;
    return short(`知识检索：RAG 引用 ${citationCount} 条，本地政策 ${localCount} 条，来源 ${content.selected_source || "-"}`, 140);
  }
  if (typeof content.passed === "boolean" || content.score !== undefined) {
    const findings = Array.isArray(content.findings) ? content.findings.length : 0;
    return short(`质量检查：score=${content.score ?? "-"} · findings=${findings} · workflow=${content.workflow_status || "-"}`, 140);
  }
  if (content.workflow_run_id || content.workflow_status) {
    return short(`工具执行：workflow=${content.workflow_run_id || "-"} · ${content.workflow_status || "-"}`, 140);
  }
  if (content.risk_level || content.decision) return short(`风险判断：${content.risk_level || "-"} · ${content.decision || "-"}`, 140);
  if (content.memory_type || content.summary) return short(content.summary || `记忆：${content.memory_type}`, 140);
  if (content.final_answer) return short(content.final_answer, 140);
  if (content.answer) return short(content.answer, 140);
  if (content.error) return short(content.error, 140);
  return short(JSON.stringify(content), 140);
}
