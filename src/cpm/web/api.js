// Thin client for the CPM API. The install token is kept in this browser's localStorage for this origin
// (so a second tab works without pasting it again; same exposure as data/install_token on disk).
// It is taken from the URL fragment (#token=...) and the fragment is removed at once.
const KEY = "cpm.token";
let token = null;
let csrf = null;

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export function loadToken() {
  const m = /(?:^#|&)token=([A-Za-z0-9_-]+)/.exec(location.hash);
  if (m) {
    setToken(m[1]);
    history.replaceState(null, "", location.pathname + "#/");
  } else if (!token) {
    try { token = localStorage.getItem(KEY); } catch { token = null; }
  }
  return token;
}

export function setToken(value) {
  token = value; csrf = null;
  try { localStorage.setItem(KEY, value); } catch { /* storage blocked: keep in memory only */ }
}
export function clearToken() {
  token = null; csrf = null;
  try { localStorage.removeItem(KEY); } catch { /* ignore */ }
}
export const hasToken = () => Boolean(token);

async function csrfToken() {
  if (!csrf) csrf = (await call("GET", "/api/session")).csrf;
  return csrf;
}

export async function call(method, path, body) {
  const headers = { "X-CPM-Token": token || "" };
  if (method !== "GET") {
    if (path !== "/api/session") headers["X-CPM-CSRF"] = await csrfToken();
    headers["Content-Type"] = "application/json";
  }
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "Cannot reach the CPM server (is it running?)");
  }
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON body */ }
  if (!res.ok) {
    let msg = res.statusText;
    if (data && typeof data.detail === "string") msg = data.detail;
    else if (data && Array.isArray(data.detail)) msg = data.detail.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("; ");
    else if (data && data.error) msg = data.error === "token_required" ? "Access token missing or wrong" : data.error;
    throw new ApiError(res.status, msg);
  }
  return data;
}
export const get = (p) => call("GET", p);
export const post = (p, b) => call("POST", p, b ?? {});
export const put = (p, b) => call("PUT", p, b ?? {});
export const patch = (p, b) => call("PATCH", p, b ?? {});
export const del = (p) => call("DELETE", p);

export async function health() {
  const res = await fetch("/health");
  if (!res.ok) throw new ApiError(res.status, "unhealthy");
  return res.json();
}

// Live stream: ask for a one-time ticket (the token never goes into a URL), then open the socket.
export async function openStream(runId, since, onEvent, onState) {
  const t = await post("/api/ws-ticket", { run_id: runId });
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}${t.path}?since=${since}&ticket=${encodeURIComponent(t.ticket)}`);
  ws.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch { /* ignore garbage */ } };
  ws.onopen = () => onState("live");
  ws.onclose = () => onState("closed");
  ws.onerror = () => onState("error");
  return ws;
}
