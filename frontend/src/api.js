const TOKEN_KEY = "agent_access_token";

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  sessionStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(TOKEN_KEY);
}

export async function api(path, options = {}) {
  const token = getToken();
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof payload === "object" ? payload.detail || JSON.stringify(payload) : payload;
    const error = new Error(message || response.statusText);
    error.status = response.status;
    throw error;
  }
  return payload;
}

export async function login(userId, password) {
  const result = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ user_id: userId, password }),
  });
  setToken(result.access_token);
  return result.user;
}

export function startOidcLogin(nextPath = "/") {
  window.location.href = `/api/auth/oidc/start?next=${encodeURIComponent(nextPath)}`;
}

export async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } finally {
    clearToken();
  }
}

export function openEventStream(onSnapshot, onError) {
  const token = getToken();
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  const source = new EventSource(`/api/events${query}`);

  source.addEventListener("snapshot", (event) => {
    try {
      onSnapshot(JSON.parse(event.data));
    } catch (err) {
      if (onError) onError(err);
    }
  });

  source.onerror = () => {
    if (onError) onError(new Error("实时事件流正在重连"));
  };

  return () => source.close();
}
