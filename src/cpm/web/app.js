import * as api from "/ui/api.js";

const view = document.getElementById("view");
const banner = document.getElementById("banner");
const conn = document.getElementById("conn");
let cleanup = [];  // timers / sockets of the current view
let navToken = 0;

// ---------------------------------------------------------------- tiny DOM helper (text only)
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const usd = (micro) => `$${(Number(micro || 0) / 1e6).toFixed(4)}`;
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
// `owner` is the navigation that started the view: a slow, superseded view must not overwrite a newer one.
const show = (owner, ...nodes) => { if (owner === navToken) view.replaceChildren(...nodes.flat()); };
const empty = (text, ...more) => h("div", { class: "empty" }, text, ...more);
const errBox = (e) => h("div", { class: "note warn", role: "alert" }, e.message || String(e));
const link = (href, text) => h("a", { href }, text);

function onLeave(fn) { cleanup.push(fn); }
function resetView() { for (const fn of cleanup) { try { fn(); } catch { /* ignore */ } } cleanup = []; }

async function act(fn, then) {
  const where = location.hash;  // do not drag the user back if they navigated away meanwhile
  try { await fn(); setBanner(null); if (then && location.hash === where) await then(); }
  catch (e) { setBanner(e.message || String(e), "error"); }
}
function setBanner(text, kind) {
  banner.hidden = !text;
  banner.className = "banner" + (kind === "info" ? " info" : "");
  banner.textContent = text || "";
}

// ---------------------------------------------------------------- connection state
async function pollHealth() {
  try {
    const hl = await api.health();
    conn.textContent = `connected · v${hl.version}`; conn.className = "pill ok";
  } catch {
    conn.textContent = "disconnected"; conn.className = "pill bad";
  }
}
setInterval(pollHealth, 5000);
document.getElementById("lock").addEventListener("click", () => { api.clearToken(); route("#/"); });

// ---------------------------------------------------------------- shared pieces
function field(label, input) { return h("div", null, h("label", null, label), input); }
const statusPill = (s) => {
  const cls = ["COMPLETED", "INTEGRATED"].includes(s) ? "ok" : ["FAILED", "CANCELLED"].includes(s) ? "bad" : ["PAUSED", "PAUSED_BUDGET", "BLOCKED"].includes(s) ? "warn" : "";
  return h("span", { class: `pill ${cls}` }, s);
};

function trustNote(project) {
  if (project.workspace_trusted) {
    return h("div", { class: "note warn" },
      "This workspace is trusted. Workers load the project's own CLAUDE.md and settings, and the project's hooks, MCP servers and test scripts can run in the task's worktree.");
  }
  return h("div", { class: "note" },
    h("strong", null, "Not trusted: "),
    "workers will not load this project's CLAUDE.md, hooks, MCP servers or skills, so they do not see the project's own rules. ",
    "Trust the workspace once you have looked at the repository; the repository's scripts and hooks are code that will then run on this PC.");
}

// ---------------------------------------------------------------- views
async function dashboardView() {
  const my = navToken;
  const [wiz, dash] = await Promise.all([api.get("/api/wizard/status"), api.get("/api/dashboard")]);
  const parts = [h("h1", null, "Dashboard")];
  if (!wiz.completed) {
    parts.push(h("div", { class: "note warn" }, "Setup is not finished: no live (billed) run is possible until an API key and a monthly cap are saved. ", link("#/settings", "Open settings"), ". Mock runs work without a key."));
  } else {
    parts.push(h("div", { class: "card" }, "Billing mode: ", h("strong", null, wiz.billing_mode), " · API key: ", wiz.api_key_configured ? h("span", { class: "ok" }, "saved") : h("span", { class: "warn" }, "missing"), " · live runs: ", wiz.live_runs_allowed ? "allowed by setup" : "not allowed yet"));
  }
  parts.push(h("h2", null, "Running now"));
  parts.push(dash.active_runs.length ? agentTable(dash.active_runs, dash.projects) : empty("No worker is running."));
  parts.push(h("h2", null, "Projects"));
  if (!dash.projects.length) parts.push(empty("No project yet. ", link("#/projects", "Add one")));
  else parts.push(h("div", { class: "grid" }, dash.projects.map(projectCard)));
  show(my, parts);
}

function projectCard(p) {
  const counts = Object.entries(p.task_counts).sort();
  return h("div", { class: "card" },
    h("strong", null, link(`#/project/${p.id}`, p.name)), " ",
    p.workspace_trusted ? h("span", { class: "pill ok" }, "trusted") : h("span", { class: "pill" }, "not trusted"),
    h("div", { class: "muted" }, counts.length ? counts.map(([s, n]) => `${s} ${n}`).join(" · ") : "no tasks"),
    h("div", { class: "muted" }, `spend ${usd(p.spend_microusd.reconciled + p.spend_microusd.estimated)} (estimate ${usd(p.spend_microusd.estimated)}, reconciled ${usd(p.spend_microusd.reconciled)})`));
}

function agentTable(runs, projects) {
  const names = Object.fromEntries((projects || []).map((p) => [p.id, p.name]));
  return h("table", null,
    h("thead", null, h("tr", null, ["Project", "Task", "Run", "Model", "Mode", "Status", "Heartbeat"].map((t) => h("th", null, t)))),
    h("tbody", null, runs.map((r) => h("tr", null,
      h("td", null, names[r.project_id] || `#${r.project_id}`),
      h("td", null, link(`#/task/${r.task_id}`, r.task_title)),
      h("td", null, `#${r.run_id} (attempt ${r.attempt})`),
      h("td", null, r.model || "—"), h("td", null, r.billing_mode), h("td", null, r.status), h("td", null, when(r.heartbeat_at))))));
}

function lockDetail(l) {
  if (l.kind === "lock_file") return l.active ? `${l.file}, ${l.age_s ?? "?"} s old` : l.file;
  if (l.kind === "lock_dir") return l.unreadable ? "folder unreadable: treated as held" : (l.files || []).join(", ");
  if (l.kind === "devlock") return (l.held || []).map((x) => x.file).join(", ");
  if (l.kind === "job_board") return l.problem || (l.claims || []).map((c) => `${c.path} (${c.owner || "?"})`).join(", ");
  return "";
}

// Reservations, Desktop claims and the managed project's own locks (WP6). Read-only except claim add/release.
async function resourcesPane(project, onDone) {
  const [res, claims, locks] = await Promise.all([api.get(`/api/projects/${project.id}/reservations`), api.get(`/api/projects/${project.id}/desktop-claims`), api.get(`/api/projects/${project.id}/locks`)]);
  const path = h("input", { id: `claim-path-${project.id}`, placeholder: "path in the project, e.g. Assets/Game/UI/", size: 36 });
  const note = h("input", { id: `claim-note-${project.id}`, placeholder: "note (optional)", size: 24 });
  const scanOut = h("div", { class: "muted", role: "status" });
  return h("div", { class: "card", "data-project": project.id },
    h("h2", null, project.name),
    h("h3", null, "Reserved by tasks"),
    res.reservations.length ? h("table", null, h("thead", null, h("tr", null, ["Task", "Resource", "Mode", "Last heartbeat", "Lease until"].map((t) => h("th", null, t)))),
      h("tbody", null, res.reservations.map((r) => h("tr", null, h("td", null, link(`#/task/${r.task_id}`, `#${r.task_id}`)), h("td", null, r.label || `${r.type}: ${r.key}`), h("td", null, r.mode), h("td", null, when(r.heartbeat_at)), h("td", null, when(r.lease_expires_at)))))) : h("p", { class: "muted" }, "Nothing reserved."),
    h("h3", null, "Claimed by you in Claude Desktop"),
    claims.claims.length ? h("table", null, h("thead", null, h("tr", null, ["Path", "Note", "Expires", ""].map((t) => h("th", null, t)))),
      h("tbody", null, claims.claims.map((c) => h("tr", null, h("td", null, c.path), h("td", null, c.note || "—"), h("td", null, c.expires_at ? when(c.expires_at) : "never"), h("td", null, h("button", { onclick: () => act(() => api.del(`/api/desktop-claims/${c.id}`), onDone) }, "Release")))))) : h("p", { class: "muted" }, "No claim. Workers will not touch a path you claim."),
    h("form", { class: "row", onsubmit: (e) => {
      e.preventDefault();
      let made = null;  // the list endpoint never carries affected_tasks, only the creation answer does
      act(async () => { made = await api.post(`/api/projects/${project.id}/desktop-claims`, { path: path.value, note: note.value }); }, async () => {
        await onDone();
        if (made && made.affected_tasks.length) setBanner(`Claim added, but task(s) ${made.affected_tasks.map((t) => `#${t}`).join(", ")} already hold this path and keep running. Pause them before you edit it.`, "info");
      });
    } }, path, note, h("button", { type: "submit" }, "Claim path")),
    h("h3", null, "The project's own locks (read-only)"),
    locks.locks.length ? h("ul", null, locks.locks.map((l) => h("li", null, h("strong", null, l.name), ` ${l.active ? "HELD" : "free"} `, h("span", { class: "muted" }, lockDetail(l))))) : h("p", { class: "muted" }, "No external lock configured for this project."),
    h("div", { class: "row" }, h("button", { onclick: () => act(async () => { const r = await api.post(`/api/projects/${project.id}/external-scan`); scanOut.textContent = r.conflicts.length ? `${r.conflicts.length} task(s) have reserved paths edited in the main tree: ${r.conflicts.map((c) => `#${c.task_id} (${c.paths.join(", ")})`).join("; ")}` : `${r.changed.length} changed file(s) in the main tree, none on a reserved path.`; }) }, "Scan the main tree for outside edits"), scanOut));
}

async function agentsView() {
  const my = navToken;
  const draw = async () => {
    const d = await api.get("/api/dashboard");
    const holds = await api.get("/api/budget/reservations");
    const pane = await Promise.all(d.projects.map((p) => resourcesPane(p, () => draw().catch(() => {}))));
    show(my, h("h1", null, "Agent monitor"), d.active_runs.length ? agentTable(d.active_runs, d.projects) : empty("No worker is running."),
      h("h2", null, "Budget held by running workers"),
      holds.length ? h("table", null, h("thead", null, h("tr", null, ["Task", "Run", "Held"].map((t) => h("th", null, t)))), h("tbody", null, holds.map((r) => h("tr", null, h("td", null, link(`#/task/${r.task_id}`, `#${r.task_id}`)), h("td", null, r.run_id ? `#${r.run_id}` : "—"), h("td", null, usd(r.amount_microusd)))))) : h("p", { class: "muted" }, "Nothing held."),
      h("h1", null, "Reservations and locks"),
      h("p", { class: "muted" }, "A task only starts when its files, devices and the project's locks are free. Claim a path here to keep workers away from it while you edit it in Claude Desktop."),
      pane.length ? pane : empty("No project yet."));
  };
  await draw();
  const t = setInterval(() => { if (!document.activeElement || !document.activeElement.closest("form")) draw().catch(() => {}); }, 3000);
  onLeave(() => clearInterval(t));
}

async function schedulerView() {
  const my = navToken;
  const draw = async () => {
    const [st, q] = await Promise.all([api.get("/api/scheduler"), api.get("/api/scheduler/queue")]);
    const cfg = st.config;
    const lim = st.limits;
    const running = q.running.length;
    const head = h("div", { class: "card" },
      h("div", { class: "row" },
        h("span", { class: `pill ${st.paused ? "warn" : "ok"}` }, st.paused ? "paused: nothing new starts" : "running"),
        !st.enabled ? h("span", { class: "pill warn" }, "background loop is off (CPM_SCHEDULER): use Run one pass") : null,
        st.paused
          ? h("button", { class: "primary", onclick: () => act(() => api.post("/api/scheduler/resume"), draw) }, "Resume")
          : h("button", { onclick: () => act(() => api.post("/api/scheduler/pause"), draw) }, "Pause"),
        h("button", { onclick: () => act(async () => { const r = await api.post("/api/scheduler/tick"); setBanner(`Pass done: ${r.started.length} started, ${Object.keys(r.skipped).length} waiting, ${r.retried.length} retried, ${r.gave_up.length} given up${r.errors.length ? `; errors: ${r.errors.join("; ")}` : ""}`, "info"); }, draw) }, "Run one pass")),
      h("p", { class: "muted" }, "Pausing only stops new starts; running workers are untouched (cancel or pause them from their task). A pass does exactly what the loop does, so it cannot skip locks or budget checks."),
      st.last_tick ? h("p", { class: "muted" }, `Last pass ${when(st.last_tick.at)}`, st.last_tick.errors.length ? h("span", { class: "warn" }, ` · ${st.last_tick.errors.join("; ")}`) : null) : h("p", { class: "muted" }, "No pass yet since the server started."));
    const limits = h("div", { class: "grid" },
      h("div", { class: "card" }, h("h3", null, "Workers at once"), h("strong", null, `${running} / ${lim.global}`), h("div", { class: "muted" }, "global limit (Settings → parallel agents)")),
      h("div", { class: "card" }, h("h3", null, "Per project"), h("strong", null, lim.per_project ? String(lim.per_project) : "no extra limit"), h("div", { class: "muted" }, "workers one project may run")),
      h("div", { class: "card" }, h("h3", null, "Heavy jobs"), h("strong", null, `${q.ready.filter((r) => r.heavy).length} waiting · max ${lim.heavy} at once`), h("div", { class: "muted" }, "Unity, Blender, GPU; fixed at 1")));
    const runningTbl = running ? h("table", null, h("thead", null, h("tr", null, ["Task", "Project", "Mode"].map((t) => h("th", null, t)))),
      h("tbody", null, q.running.map((r) => h("tr", null, h("td", null, link(`#/task/${r.task_id}`, `#${r.task_id} ${r.title}`)), h("td", null, r.project), h("td", null, r.billing_mode))))) : h("p", { class: "muted" }, "No worker is running.");
    const queueTbl = q.ready.length ? h("table", null, h("thead", null, h("tr", null, ["#", "Task", "Project", "Priority", "Mode", "Estimate", "Why it waits"].map((t) => h("th", null, t)))),
      h("tbody", null, q.ready.map((r) => h("tr", null,
        h("td", null, String(r.position)),
        h("td", null, link(`#/task/${r.task_id}`, `#${r.task_id} ${r.title}`), r.heavy ? h("span", { class: "pill warn" }, "heavy") : null),
        h("td", null, r.project), h("td", null, String(r.priority)), h("td", null, r.billing_mode),
        h("td", null, r.estimate_microusd ? usd(r.estimate_microusd) : "—"),
        h("td", null,
          r.backoff_s > 0 ? h("span", { class: "pill warn" }, `backing off ${Math.ceil(r.backoff_s)} s (${r.refused_starts} refused start${r.refused_starts === 1 ? "" : "s"})`) : null,
          r.reason ? h("span", { class: r.reason.startsWith("waiting") ? "muted" : "warn" }, ` ${r.reason}`) : (r.backoff_s > 0 ? null : h("span", { class: "muted" }, "next in line, no refusal recorded"))))))) : empty("The queue is empty: no task is READY.");
    const retryTbl = q.failed.length ? h("table", null, h("thead", null, h("tr", null, ["Failed task", "Mode", "Retries", "Automatic retry", "Because"].map((t) => h("th", null, t)))),
      h("tbody", null, q.failed.map((r) => h("tr", null, h("td", null, link(`#/task/${r.task_id}`, `#${r.task_id} ${r.title}`)), h("td", null, r.billing_mode), h("td", null, String(r.retry_count)),
        h("td", null, h("span", { class: `pill ${r.action === "give_up" ? "bad" : r.action === "wait" ? "warn" : "ok"}` }, { retry: "will retry", wait: "waiting", give_up: "gave up" }[r.action] || r.action)), h("td", null, r.why))))) : h("p", { class: "muted" }, "No failed task.");
    const apiRetry = cfg.retry_billing_modes.includes("api");
    const num = (id, v, attrs) => h("input", { id, type: "number", value: v, step: "any", ...attrs });
    const ppl = num("sc-ppl", cfg.per_project_limit ?? "", { min: 1, max: 8, placeholder: "none" });
    const mr = num("sc-mr", cfg.max_auto_retries, { min: 0, max: 5 });
    const il = num("sc-il", cfg.identical_failure_limit, { min: 1, max: 5 });
    const rd = num("sc-rd", cfg.retry_delay_s, { min: 0, max: 3600 });
    const save = (modes) => act(() => api.put("/api/scheduler/config", {
      per_project_limit: ppl.value === "" ? null : Number(ppl.value), max_auto_retries: Number(mr.value),
      identical_failure_limit: Number(il.value), retry_delay_s: Number(rd.value),
      hang_timeout_s: cfg.hang_timeout_s, max_runtime_s: cfg.max_runtime_s, external_scan_s: cfg.external_scan_s,
      retry_billing_modes: modes,
    }), draw);
    const toggle = h("button", { type: "button", class: apiRetry ? "danger" : "", onclick: () => {
      const next = apiRetry ? cfg.retry_billing_modes.filter((m) => m !== "api") : [...cfg.retry_billing_modes, "api"];
      if (!apiRetry && !confirm("Allow the scheduler to re-run a failed API task by itself? Each retry spends money from the budget (still capped by the task, project and month limits).")) return;
      save(next);
    } }, apiRetry ? "Turn off automatic retry for API tasks" : "Turn on automatic retry for API tasks");
    const form = h("form", { class: "card", onsubmit: (e) => { e.preventDefault(); save(cfg.retry_billing_modes); } },
      h("div", { class: "row" }, field("Workers per project (empty = none)", ppl), field("Automatic retries per task", mr), field("Stop after N identical failures", il), field("Wait before a retry (s)", rd)),
      h("p", null, h("button", { class: "primary", type: "submit" }, "Save limits")),
      h("p", null, h("strong", null, "Automatic retry of API tasks: "), h("span", { class: `pill ${apiRetry ? "warn" : "ok"}` }, apiRetry ? "on" : "off"), " ", toggle),
      h("p", { class: "muted" }, apiRetry ? "A failed API task is re-run up to the retry limit, each run charged to the budget." : "Off by default: a failed API task stays FAILED until you retry it, so nothing is spent without your click.", ` Retry applies to: ${cfg.retry_billing_modes.join(", ") || "nothing"}.`));
    show(my, h("h1", null, "Scheduler"), head, limits,
      h("h2", null, "Running now"), runningTbl,
      h("h2", null, "Queue (order of start)"),
      h("p", { class: "muted" }, "Sorted by priority (lower number first); among heavy tasks the cheaper estimate goes first; ties by task number."), queueTbl,
      h("h2", null, "Failed tasks and automatic retry"), retryTbl,
      h("h2", null, "Limits and retry"), form);
  };
  await draw();
  const t = setInterval(() => { if (!document.activeElement || !document.activeElement.closest("form")) draw().catch(() => {}); }, 3000);
  onLeave(() => clearInterval(t));
}

async function projectsView() {
  const my = navToken;
  const [list, profiles] = await Promise.all([api.get("/api/projects"), api.get("/api/profiles")]);
  const name = h("input", { id: "p-name", required: true });
  const path = h("input", { id: "p-path", size: 40, required: true });
  const remote = h("input", { id: "p-remote", size: 40, placeholder: "optional, https or ssh" });
  const prof = h("select", { id: "p-profile" }, h("option", { value: "" }, "none"), (profiles.profiles || []).map((p) => h("option", { value: p.id }, p.id)));
  const form = h("form", { class: "card", onsubmit: (e) => {
    e.preventDefault();
    act(() => api.post("/api/projects", { name: name.value, path: path.value, remote_url: remote.value || null, profile: prof.value || null }), () => route());
  } },
    h("h2", null, "Add a project"),
    h("p", { class: "muted" }, "CPM only reads the folder. Registering a project approves that folder for workers' git worktrees; nothing is written into it."),
    field("Name", name), field("Folder on this PC", path), field("Git remote", remote), field("Profile", prof),
    h("p", null, h("button", { class: "primary", type: "submit" }, "Add project")));
  show(my, h("h1", null, "Projects"),
    list.projects.length ? h("div", { class: "grid" }, list.projects.map((p) => h("div", { class: "card" }, h("strong", null, link(`#/project/${p.id}`, p.name)), h("div", { class: "muted" }, p.path), p.needs_remote ? h("div", { class: "warn" }, "no git remote known") : h("div", { class: "muted" }, p.remote_url)))) : empty("No project registered yet."),
    form);
}

const BOARD = ["BACKLOG", "READY", "BLOCKED", "RESERVED", "RUNNING", "TESTING", "REVIEWING", "CORRECTING", "PAUSED", "PAUSED_BUDGET", "FAILED", "COMPLETED", "INTEGRATED", "CANCELLED"];

async function projectView(id, tab) {
  const my = navToken;
  const [project, tasks] = await Promise.all([api.get(`/api/projects/${id}`), api.get(`/api/projects/${id}/tasks`)]);
  const tabs = ["board", "table", "sources", "add"].map((t) => h("button", { class: tab === t ? "on" : "", onclick: () => route(`#/project/${id}/${t}`) }, { board: "Board", table: "Table", sources: "Sources & trust", add: "Add tasks" }[t]));
  let body;
  if (tab === "table") body = taskTable(tasks);
  else if (tab === "sources") body = sourcesPane(project);
  else if (tab === "add") body = addPane(id);
  else body = board(tasks);
  const holder = h("div", null, body);
  show(my, h("h1", null, project.name), h("div", { class: "muted" }, project.path, project.remote_url ? ` · ${project.remote_url}` : " · no git remote"), h("div", { class: "tabs" }, tabs), trustNote(project), holder);
  if (tab === "board" || tab === "table") {  // keep the board current while workers run
    const t = setInterval(async () => {
      try {
        const fresh = await api.get(`/api/projects/${id}/tasks`);
        if (my === navToken) holder.replaceChildren(tab === "table" ? taskTable(fresh) : board(fresh));
      } catch { /* the connection pill already says so */ }
    }, 3000);
    onLeave(() => clearInterval(t));
  }
}

function board(tasks) {
  if (!tasks.length) return empty("No task yet. Use “Add tasks”.");
  return h("div", { class: "board" }, BOARD.map((s) => {
    const here = tasks.filter((t) => t.status === s);
    return h("div", { class: "col" }, h("h3", null, `${s} (${here.length})`),
      here.map((t) => h("div", { class: "tcard" }, link(`#/task/${t.id}`, t.title), h("div", { class: "muted" }, `prio ${t.priority} · ${t.billing_mode}`))));
  }));
}

function taskTable(tasks) {
  if (!tasks.length) return empty("No task yet.");
  return h("table", null,
    h("thead", null, h("tr", null, ["#", "Title", "Status", "Priority", "Mode", "Depends on", "Updated"].map((t) => h("th", null, t)))),
    h("tbody", null, tasks.map((t) => h("tr", null, h("td", null, t.id), h("td", null, link(`#/task/${t.id}`, t.title)), h("td", null, statusPill(t.status)), h("td", null, t.priority), h("td", null, t.billing_mode), h("td", null, t.depends_on.join(", ") || "—"), h("td", null, when(t.updated_at))))));
}

function sourcesPane(project) {
  const trustBtn = h("button", { class: project.workspace_trusted ? "danger" : "primary", onclick: () => {
    if (!project.workspace_trusted && !confirm("Trust this workspace? Its hooks, MCP servers and scripts can run on this PC.")) return;
    act(() => api.put(`/api/projects/${project.id}/trust`, { trusted: !project.workspace_trusted }), () => route());
  } }, project.workspace_trusted ? "Remove trust" : "Trust this workspace");
  const git = project.git || {};
  return h("div", null,
    h("div", { class: "card" }, trustBtn, " ", h("button", { onclick: () => act(() => api.post(`/api/projects/${project.id}/refresh`), () => route()) }, "Rescan files")),
    h("h2", null, "Instruction and task sources"),
    project.sources.length ? h("table", null, h("thead", null, h("tr", null, ["File", "Kind", "Used by workers", ""].map((t) => h("th", null, t)))),
      h("tbody", null, project.sources.map((s) => h("tr", null, h("td", null, s.rel_path), h("td", null, s.kind),
        h("td", null, s.active ? "yes" : s.requires_trust && !project.workspace_trusted ? "no (needs trust)" : "no (excluded)"),
        h("td", null, h("button", { onclick: () => act(() => api.put(`/api/projects/${project.id}/sources/${s.id}`, { included: !s.included }), () => route()) }, s.included ? "Exclude" : "Include")))))) : empty("No CLAUDE.md, memory or TODO file was found."),
    h("h2", null, "Git"),
    h("div", { class: "card muted" }, git.is_repo ? JSON.stringify({ branch: git.branch, dirty_files: git.dirty_count, remotes: git.remotes }) : "Not a git repository, or unreadable."),
    h("h2", null, "Detected commands"),
    project.commands.length ? h("ul", null, project.commands.map((c) => h("li", null, `${c.purpose}: ${c.argv.join(" ")}`))) : h("p", { class: "muted" }, "None."));
}

function addPane(id) {
  const title = h("input", { id: "t-title", size: 50 });
  const desc = h("textarea", { id: "t-desc" });
  const prio = h("input", { id: "t-prio", type: "number", value: "100", min: "0", max: "10000" });
  const mode = h("select", { id: "t-mode" }, ["mock", "api"].map((m) => h("option", { value: m }, m)));
  const todo = h("textarea", { id: "todo-text", placeholder: "- [ ] first item\n- [ ] second item" });
  return h("div", null,
    h("form", { class: "card", onsubmit: (e) => { e.preventDefault(); act(() => api.post(`/api/projects/${id}/tasks`, { title: title.value, description: desc.value, priority: Number(prio.value), billing_mode: mode.value }), () => route(`#/project/${id}/board`)); } },
      h("h2", null, "New task"), field("Title", title), field("Description", desc), field("Priority (lower runs first)", prio),
      field("Billing mode (api needs a key and a cap; mock is free)", mode), h("p", null, h("button", { class: "primary", type: "submit" }, "Create task"))),
    h("form", { class: "card", onsubmit: (e) => { e.preventDefault(); act(() => api.post(`/api/projects/${id}/tasks/import-todo`, { text: todo.value }), () => route(`#/project/${id}/board`)); } },
      h("h2", null, "Import a TODO list"), todo, h("p", null, h("button", { type: "submit" }, "Import checkboxes"))));
}

// ---------------------------------------------------------------- task + live monitor
// Why a task cannot start now (WP6 admission) and the manual release of a stuck reservation.
function conflictsBox(why, onDone, taskId) {
  // The release button is always offered: the holder of a stale reservation is not blocked by it, so "no blockers" must not hide it.
  const release = h("button", { onclick: () => confirm("Release this task's own reservations? Only do this if nothing is working on it.") && act(() => api.post(`/api/tasks/${taskId}/reservations/release`), onDone) }, "Release this task's reservations");
  if (!why.blockers.length) return h("div", { class: "note", id: "conflicts" }, "No lock or reservation blocks this task right now. ", release);
  return h("div", { class: "note warn", id: "conflicts" }, h("strong", null, "Blocked by:"),
    h("ul", null, why.blockers.map((b) => h("li", null, `${b.message}`, b.holder_task_id ? [" (", link(`#/task/${b.holder_task_id}`, `task #${b.holder_task_id}`), ")"] : null))), release);
}

async function taskView(id) {
  const my = navToken;
  const [task, states, runs] = await Promise.all([api.get(`/api/tasks/${id}`), api.get("/api/task-states"), api.get(`/api/tasks/${id}/runs`)]);
  const project = await api.get(`/api/projects/${task.project_id}`);
  const moves = (states.transitions[task.status] || []).filter((s) => states.user_targets.includes(s));
  const latest = runs.length ? runs[runs.length - 1] : null;
  const live = latest ? (await api.get(`/api/runs/${latest.id}/events?limit=1`)).live : false;
  const tb = await api.get(`/api/tasks/${id}/budget`);
  const waiting = ["READY", "RESERVED", "PAUSED", "PAUSED_BUDGET", "BLOCKED"].includes(task.status) && !live;
  const why = waiting ? await api.get(`/api/tasks/${id}/conflicts`) : null;

  const controls = h("div", { class: "row" }, moves.map((s) => h("button", { class: s === "CANCELLED" ? "danger" : "", onclick: () => act(() => api.post(`/api/tasks/${id}/transition`, { to: s }), () => route()) }, `→ ${s}`)));
  const runBox = h("div", { class: "card" }, h("h2", null, "Run"));
  if (live) {
    runBox.append(h("div", { class: "row" },
      h("button", { onclick: () => act(() => api.post(`/api/runs/${latest.id}/pause`), () => route()) }, "Pause"),
      h("button", { class: "danger", onclick: () => confirm("Kill the worker's process tree now?") && act(() => api.post(`/api/runs/${latest.id}/stop`), () => route()) }, "Cancel run")));
  } else if (["READY", "PAUSED", "PAUSED_BUDGET"].includes(task.status)) {
    const scen = h("select", { id: "r-scen" }, ["success", "fail", "slow", "hang", "crash", "budget_burst"].map((s) => h("option", { value: s }, s)));
    const budget = h("input", { id: "r-budget", type: "number", step: "0.01", min: "0.01", placeholder: "USD for this run" });
    const isApi = task.billing_mode === "api";
    runBox.append(isApi
      ? h("div", { class: "note warn" }, "Billed API run: needs the live-run switch, a saved key and a cap, and a per-run budget (at most the setup's task budget). Nothing is spent without all of these.")
      : h("div", { class: "note" }, "Mock run: scripted, no network, no key, no cost."),
      isApi ? field("Per-run budget (USD)", budget) : field("Mock scenario", scen),
      h("p", null, h("button", { class: "primary", onclick: () => act(() => {
        const body = isApi ? { max_budget_microusd: Math.round(Number(budget.value) * 1e6) } : { scenario: scen.value };
        return api.post(task.status.startsWith("PAUSED") ? `/api/tasks/${id}/resume` : `/api/tasks/${id}/runs`, body);
      }, () => route()) }, task.status.startsWith("PAUSED") ? "Resume" : "Start run")));
  } else {
    runBox.append(h("p", { class: "muted" }, `No run can start from ${task.status}.`));
  }

  const reviewBox = (await reviewPane(task, () => route())) || null;
  const logBox = h("pre", { class: "log", id: "run-log", "aria-label": "Live worker log" }, latest ? "" : "No run yet.");
  const streamState = h("span", { class: "pill" }, latest ? "…" : "idle");
  show(my, h("h1", null, task.title), h("div", { class: "muted" }, link(`#/project/${task.project_id}/board`, project.name)),
    h("div", { class: "row" }, statusPill(task.status), `priority ${task.priority} · ${task.billing_mode} · model ${task.model || "default"} · retries ${task.retry_count}`),
    task.block_reason ? h("div", { class: "note warn" }, `Blocked: ${task.block_reason}`) : null,
    trustNote(project),
    task.status === "PAUSED_BUDGET" ? budgetActions(task, tb.remaining_microusd, () => route()) : null,
    h("div", { class: "card", id: "task-budget" }, h("strong", null, "Budget "), `spent ${usd(tb.spent_microusd)}`, tb.cap_microusd != null ? ` of ${usd(tb.cap_microusd)} (left ${usd(tb.remaining_microusd)})` : " (no task cap)", h("span", { class: "muted" }, ` · ${tb.label}`)),
    why ? conflictsBox(why, () => route(), id) : null,
    h("div", { class: "card" }, h("p", null, task.description || "No description."), h("h2", null, "Acceptance"), task.acceptance.length ? h("ul", null, task.acceptance.map((a) => h("li", null, a))) : h("p", { class: "muted" }, "None."), h("h2", null, "Declared scope"), task.declared_scope.length ? h("ul", null, task.declared_scope.map((a) => h("li", null, a))) : h("p", { class: "muted" }, "None.")),
    h("h2", null, "Move this task"), moves.length ? controls : h("p", { class: "muted" }, "No move is available to you from this state."),
    runBox,
    reviewBox,
    h("h2", null, "Runs"),
    runs.length ? h("table", null, h("thead", null, h("tr", null, ["#", "Attempt", "Status", "Model", "Started", "Ended", "Cost (estimate)"].map((t) => h("th", null, t)))),
      h("tbody", null, runs.map((r) => h("tr", null, h("td", null, r.id), h("td", null, r.attempt), h("td", null, r.status), h("td", null, r.model || "—"), h("td", null, when(r.started_at)), h("td", null, when(r.ended_at)), h("td", null, usd(r.cost_microusd)))))) : empty("No run yet."),
    h("h2", null, "Live log ", streamState), logBox,
    h("h2", null, "History"),
    h("ul", null, task.events.map((e) => h("li", { class: "muted" }, `${when(e.ts)} · ${e.kind}`))));

  if (latest) streamRun(latest.id, logBox, streamState, live ? id : null);
}

function streamRun(runId, logBox, stateEl, refreshTask) {
  let ws = null; let last = 0; let closed = false; let retry = 0;
  const line = (ev) => {
    const { seq, ts, type, ...rest } = ev;
    logBox.append(document.createTextNode(`${seq ?? ""} ${type}${Object.keys(rest).length ? " " + JSON.stringify(rest) : ""}\n`));
    logBox.scrollTop = logBox.scrollHeight;
  };
  const set = (t, cls) => { stateEl.textContent = t; stateEl.className = `pill ${cls || ""}`; };
  let timer = null;
  const retryLater = () => {
    if (closed || timer) return;  // one retry per dead socket (error and close both report it)
    set("reconnecting…", "warn");
    timer = setTimeout(() => { timer = null; open(); }, Math.min(1000 * 2 ** retry++, 10000));
  };
  const open = async () => {
    if (closed) return;
    try {
      const sock = await api.openStream(runId, last, (ev) => {
        if (ev.type === "end") { set("run finished", "ok"); closed = true; if (refreshTask) route(); return; }
        if (typeof ev.seq === "number") last = ev.seq;
        line(ev);
      }, (st) => {
        if (st === "live") { set("streaming", "ok"); retry = 0; }
        else if (!closed) retryLater();
      });
      if (closed) { sock.close(); return; }  // the view was left while the ticket was requested
      ws = sock;
    } catch (e) { if (!closed) { set(e.message || "stream failed", "bad"); retryLater(); } }
  };
  onLeave(() => { closed = true; clearTimeout(timer); if (ws) ws.close(); });
  open();
}

// ---------------------------------------------------------------- costs / git / settings
const LEVEL_TEXT = { 0: "below 70 %: nothing to report", 70: "70 % reached: information only", 90: "90 % reached: alert; a worker is asked for a short checkpoint while budget remains", 100: "100 % reached: no new run starts and running tasks pause as PAUSED_BUDGET" };
const meterClass = (pct) => (pct == null ? "" : pct >= 100 ? "bad" : pct >= 90 ? "high" : pct >= 70 ? "mid" : "");
function meter(pct) {
  const w = pct == null ? 0 : Math.min(pct, 100);
  const at = (el, left) => { el.style.left = `${left}%`; return el; };  // CSSOM, not a style attribute: the CSP forbids those
  const fill = h("div", { class: "fill" });
  fill.style.width = `${w}%`;
  return h("div", { class: `meter ${meterClass(pct)}`, role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(w), "aria-label": "Budget used" },
    fill, at(h("i", { class: "mark" }), 70), at(h("i", { class: "mark" }), 90));
}
const pctText = (pct) => (pct == null ? "no cap" : `${pct} %`);

// PAUSED_BUDGET: the five choices of MASTER_PROMPT section 12. None of them starts a worker; "Resume" does, through admission.
function budgetActions(task, headroom, onDone) {
  const amount = h("input", { id: "b-amount", type: "number", step: "0.01", min: "0.01", placeholder: "USD to add" });
  const model = h("select", { id: "b-model" }, ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"].map((m) => h("option", { value: m, selected: m === task.model }, m)));
  const run = (body) => act(() => api.post(`/api/tasks/${task.id}/budget-action`, body), onDone);
  return h("div", { class: "card", id: "budget-actions" },
    h("h2", null, "This task stopped because its budget ran out"),
    h("p", { class: "muted" }, "The worktree and partial work are kept. Nothing is spent until you choose one of these; adding budget is your explicit authorisation to spend that amount."),
    h("div", { class: "row" }, field("Add a fixed amount (USD)", amount), h("button", { class: "primary", onclick: () => { if (!amount.value) { setBanner("Enter an amount first", "error"); return; } if (confirm(`Authorise ${amount.value} USD more for this task?`)) run({ action: "add_budget", amount_usd: amount.value }); } }, "Add budget")),
    h("div", { class: "row" }, field("Cheaper model", model), h("button", { onclick: () => run({ action: "reassign_model", model: model.value }) }, "Reassign model")),
    h("div", { class: "row" },
      h("button", { onclick: () => run({ action: "resume_no_increase" }) }, "Check: resume without increase"),
      h("button", { onclick: () => run({ action: "defer" }) }, "Defer until the month renews"),
      h("button", { class: "danger", onclick: () => confirm("Cancel the task? The worktree stays on disk.") && run({ action: "cancel" }) }, "Cancel, keep the work")),
    h("p", { class: "muted" }, headroom == null ? "No cap applies to this task." : `Room left under the task cap: ${usd(headroom)}. “Resume without increase” only works once an accounting error is corrected.`));
}

async function costsView() {
  const my = navToken;
  const [b, wiz, dash, prices] = await Promise.all([api.get("/api/budget"), api.get("/api/wizard/status"), api.get("/api/dashboard"), api.get("/api/budget/prices")]);
  const paused = (await Promise.all(dash.projects.map((p) => api.get(`/api/projects/${p.id}/tasks?status=PAUSED_BUDGET`).then((l) => l.map((t) => ({ ...t, project: p.name })))))).flat();
  const names = Object.fromEntries(dash.projects.map((p) => [p.id, p.name]));
  const m = b.month;
  const top = h("input", { id: "c-topup", type: "number", step: "0.01", min: "0.01", placeholder: "USD" });
  const bal = h("input", { id: "c-credit", type: "number", step: "0.01", min: "0", placeholder: "balance in USD" });
  const act2 = h("input", { id: "c-actual", type: "number", step: "0.01", min: "0", placeholder: "Console total in USD" });
  const refresh = () => route();
  show(my, h("h1", null, "Costs"),
    paused.length ? h("div", { class: "note warn", role: "alert" }, `${paused.length} task(s) stopped for budget: `, paused.map((t, i) => [i ? ", " : "", link(`#/task/${t.id}`, t.title)])) : null,
    !b.configured ? h("div", { class: "note warn" }, "No monthly cap saved: live runs are refused. ", link("#/settings", "Set it in settings"), ".") : null,
    h("div", { class: "card", id: "month-card" },
      h("h2", null, `Month ${b.period}`),
      h("div", { class: "row" }, h("strong", null, usd(m.spent_microusd)), m.cap_microusd ? `of ${usd(m.cap_microusd)}` : "(no cap)", `· ${pctText(m.percent)}`, h("span", { class: `pill ${meterClass(m.percent) === "bad" ? "bad" : m.level >= 70 ? "warn" : "ok"}` }, m.level ? `${m.level} % level` : "OK")),
      meter(m.percent),
      h("div", { class: "muted" }, LEVEL_TEXT[m.level] || LEVEL_TEXT[0]),
      h("div", { class: "muted" }, `Held by running workers: ${usd(m.held_microusd)} · available for new runs: ${usd(m.remaining_microusd)} · figures: ${m.label}`),
      m.reconciliation ? h("div", { class: "muted" }, `Reconciled with the Console: ${usd(m.reconciliation.actual_microusd ?? 0)}`) : null,
      h("p", { class: "muted" }, "Thresholds are fixed at 70 / 90 / 100 % of the cap. The cap is changed in ", link("#/settings", "Settings"), "; a one-off increase is below.")),
    h("div", { class: "card" }, h("h2", null, "Add to this month's cap"),
      h("div", { class: "row" }, top, h("button", { class: "primary", onclick: () => { if (!top.value) { setBanner("Enter an amount first", "error"); return; } if (confirm(`Authorise ${top.value} USD more this month?`)) act(() => api.post("/api/budget/top-up", { scope: "month", amount_usd: top.value }), refresh); } }, "Add to cap")),
      h("p", { class: "muted" }, "Authorising more spend is always your decision. Credits are never reloaded automatically.")),
    h("h2", null, "Per project"),
    b.projects.length ? h("table", null, h("thead", null, h("tr", null, ["Project", "Spent", "Held", "Cap", "Used", "Room left"].map((t) => h("th", null, t)))),
      h("tbody", null, b.projects.map((p) => h("tr", null, h("td", null, names[p.project_id] || `#${p.project_id}`), h("td", null, usd(p.spent_microusd)), h("td", null, usd(p.held_microusd)), h("td", null, p.cap_microusd ? usd(p.cap_microusd) : "—"), h("td", null, pctText(p.percent)), h("td", null, p.remaining_microusd == null ? "—" : usd(p.remaining_microusd)))))) : empty("No spend or hold this month."),
    h("h2", null, "Recorded spend (all time, per project)"),
    dash.projects.length ? h("table", null, h("thead", null, h("tr", null, ["Project", "Estimated (SDK/price table)", "Reconciled"].map((t) => h("th", null, t)))),
      h("tbody", null, dash.projects.map((p) => h("tr", null, h("td", null, p.name), h("td", null, usd(p.spend_microusd.estimated)), h("td", null, usd(p.spend_microusd.reconciled)))))) : empty("No project yet."),
    paused.length ? h("div", null, h("h2", null, "Tasks waiting for a budget decision"), paused.map((t) => h("div", { class: "card" }, h("strong", null, link(`#/task/${t.id}`, t.title)), h("span", { class: "muted" }, ` · ${t.project} · `), "choose an option on the task page"))) : null,
    h("div", { class: "card" }, h("h2", null, "Credit balance and reconciliation"),
      b.credit_balance ? h("p", null, `Entered ${usd(b.credit_balance.entered_microusd)} on ${when(b.credit_balance.as_of)}; estimated left ${usd(b.credit_balance.estimated_remaining_microusd)} (${b.credit_balance.label}).`) : h("p", { class: "muted" }, "No balance entered. The Anthropic API does not expose the remaining credit, so enter it by hand if you want an estimate."),
      h("div", { class: "row" }, bal,
        h("button", { onclick: () => bal.value !== "" ? act(() => api.put("/api/budget/credit-balance", { balance_usd: bal.value }), refresh) : setBanner("Enter the balance first", "error") }, "Save balance"),
        b.credit_balance ? h("button", { onclick: () => act(() => api.del("/api/budget/credit-balance"), refresh) }, "Clear") : null),
      h("div", { class: "row" }, act2, h("button", { onclick: () => act2.value !== "" ? act(() => api.post("/api/budget/reconcile", { actual_usd: act2.value }), refresh) : setBanner("Enter the Console total first", "error") }, "Reconcile this month")),
      h("p", { class: "muted" }, "Copy the month's total from the Anthropic Console; CPM corrects its estimate to it.")),
    h("h2", null, "Limits you should know"),
    h("ul", null, b.limitations.map((l) => h("li", { class: "muted" }, l))),
    h("h2", null, "Price table"),
    prices.length ? h("table", null, h("thead", null, h("tr", null, ["Model", "Input", "Output", "Cache read", "Cache write", "Verified", "Version"].map((t) => h("th", null, t)))),
      h("tbody", null, prices.map((r) => h("tr", null, h("td", null, r.model), h("td", null, `$${r.input_usd}`), h("td", null, `$${r.output_usd}`), h("td", null, `$${r.cache_read_usd}`), h("td", null, `$${r.cache_write_usd}`), h("td", null, r.verified_on), h("td", null, r.version))))) : empty("No price loaded."),
    h("p", { class: "muted" }, "USD per million tokens. Check them against the official pricing page before relying on an estimate."));
}


// ---------------------------------------------------------------- review: gate, diff, pull request (WP10)
const OUTCOME_TEXT = {
  passed: "passed", tests_failed: "tests failed", review_rejected: "review rejected",
  unexpected_files: "unexpected files", criteria_missing: "no acceptance criteria", no_changes: "no changes",
  needs_trust: "workspace not trusted", conflict: "merge conflict", review_pending: "waiting for your review", error: "could not run",
};
const outcomePill = (r) => h("span", { class: `pill ${r.outcome === "passed" ? "ok" : r.action === "waiting" ? "warn" : "bad"}` }, OUTCOME_TEXT[r.outcome] || r.outcome);
const kb = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : n >= 1e3 ? `${(n / 1e3).toFixed(1)} kB` : `${n} B`);

function diffBlock(text, truncated) {
  const lines = String(text || "").split("\n");
  const box = h("pre", { class: "diff", "aria-label": "Diff" });
  for (const line of lines.slice(0, 4000)) {
    const cls = line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") ? "dh" : line.startsWith("@@") ? "dk" : line.startsWith("+") ? "da" : line.startsWith("-") ? "dd" : "";
    box.append(h("div", { class: cls }, line === "" ? " " : line));
  }
  if (truncated || lines.length > 4000) box.append(h("div", { class: "dk" }, "… diff shortened"));
  return box;
}

function gateSummary(r) {
  const sm = r.summary || {};
  const out = [];
  if (sm.reason) out.push(h("p", null, sm.reason));
  if (sm.unexpected && sm.unexpected.length) out.push(h("div", null, h("strong", null, "Unexpected files: "), h("ul", null, sm.unexpected.map((u) => h("li", null, `${u.path} — ${u.why}`)))));
  if (sm.tests) {
    out.push(h("div", null, h("strong", null, "Tests: "), sm.tests.status,
      (sm.tests.runs || []).map((t) => h("div", { class: "muted" }, `$ ${t.argv.join(" ")} → exit ${t.exit_code}${t.timed_out ? " (timed out)" : ""} in ${t.duration_s} s`)),
      sm.tests.status === "failed" && sm.tests.runs.length ? h("pre", { class: "log" }, sm.tests.runs[sm.tests.runs.length - 1].tail) : null,
      sm.tests.status === "skipped" ? h("div", { class: "warn" }, sm.tests.reason) : null));
  }
  if (sm.conflicts && sm.conflicts.length) out.push(h("div", { class: "warn" }, `Conflicts with the base: ${sm.conflicts.join(", ")}`));
  if (sm.review && sm.review.criteria) {
    out.push(h("div", null, h("strong", null, `Independent review (${sm.review.status}): `), sm.review.summary || "",
      h("ul", null, sm.review.criteria.map((c) => h("li", { class: c.met ? "ok" : "bad" }, `${c.met ? "met" : "NOT met"}: ${c.text}`, c.note ? h("span", { class: "muted" }, ` — ${c.note}`) : null))),
      sm.review.issues && sm.review.issues.length ? h("ul", null, sm.review.issues.map((i) => h("li", { class: i.severity === "blocker" ? "bad" : "warn" }, `${i.severity} · ${i.category}: ${i.text}`))) : null));
  } else if (sm.review && sm.review.reason) out.push(h("div", { class: "warn" }, sm.review.reason));
  if (sm.gave_up) out.push(h("div", { class: "bad" }, `Stopped: ${sm.gave_up}`));
  if (sm.correction) out.push(h("div", { class: "muted" }, `Sent back to a worker: correction ${sm.correction.attempt} of ${sm.correction.of}.`));
  return out;
}

async function reviewPane(task, onDone) {
  const id = task.id;
  const hasWork = ["TESTING", "REVIEWING", "CORRECTING", "COMPLETED", "INTEGRATED", "FAILED", "PAUSED"].includes(task.status);
  if (!hasWork) return null;
  const [rv, changes] = await Promise.all([api.get(`/api/tasks/${id}/review`), api.get(`/api/tasks/${id}/changes`).catch((e) => ({ error: e.message }))]);
  const latest = rv.results[0];
  const diffBox = h("div", { id: "diff-box" });
  const showDiff = (path) => act(async () => {
    const d = await api.get(`/api/tasks/${id}/diff${path ? `?path=${encodeURIComponent(path)}` : ""}`);
    diffBox.replaceChildren(h("div", { class: "muted" }, path || "whole change"), diffBlock(d.diff, d.truncated));
  });
  const head = h("div", { class: "row" },
    task.status === "TESTING" ? h("button", { class: "primary", id: "gate-run", onclick: () => act(async () => { const r = await api.post(`/api/tasks/${id}/gate`); setBanner(`Gate: ${OUTCOME_TEXT[r.outcome] || r.outcome} — ${r.message}`, r.action === "completed" ? "info" : "error"); }, onDone) }, "Run the acceptance gate") : null,
    latest && latest.outcome === "review_pending" && task.status === "TESTING"
      ? h("button", { id: "gate-approve", onclick: () => confirm("No automatic reviewer is available. Approve this result after reading the diff yourself?") && act(() => api.post(`/api/tasks/${id}/gate/approve`, { gate_id: latest.id }), onDone) }, "Approve after my review") : null,
    rv.running ? h("span", { class: "pill warn" }, "gate running…") : null,
    h("button", { onclick: () => showDiff(null) }, "View whole diff"));
  const files = changes.error
    ? h("p", { class: "muted" }, `No changes to show yet: ${changes.error}`)
    : changes.files.length
      ? h("table", null, h("thead", null, h("tr", null, ["File", "Change", "Lines", "Size", "Check", ""].map((t) => h("th", null, t)))),
        h("tbody", null, changes.files.map((f) => h("tr", null, h("td", null, f.path), h("td", null, f.status), h("td", null, f.added == null ? "binary" : `+${f.added} −${f.deleted}`), h("td", null, kb(f.size)),
          h("td", null, f.unexpected ? h("span", { class: "pill bad" }, f.unexpected) : h("span", { class: "pill ok" }, "expected")),
          h("td", null, h("button", { onclick: () => showDiff(f.path) }, "Diff"))))))
      : h("p", { class: "muted" }, "The worktree has no change yet.");
  const history = rv.results.length
    ? rv.results.map((r) => h("div", { class: "card" }, h("div", { class: "row" }, outcomePill(r), h("span", { class: "muted" }, `${when(r.at)} · ${r.action}`)), gateSummary(r)))
    : [h("p", { class: "muted" }, "The gate has not judged this task yet. It runs by itself when the scheduler is on, or press the button above.")];
  return h("div", null,
    h("h2", null, "Review"),
    changes.error ? null : h("div", { class: "muted" }, `Branch ${changes.branch} against ${changes.base_ref} (${changes.base})${changes.unexpected ? ` · ${changes.unexpected} unexpected file(s)` : ""}`),
    head, files, diffBox,
    h("h3", null, `Acceptance gate (up to ${rv.max_corrections} corrections)`), history,
    integrationBox(task, rv.integration, onDone),
    jobBoardBox(task, onDone));
}

function jobBoardBox(task, onDone) {
  const id = task.id;
  const box = h("div", { class: "card", id: "job-board" }, h("h3", null, "Job board"), h("p", { class: "muted" }, "Loading…"));
  api.get(`/api/tasks/${id}/job-board`).then((st) => {
    const prLink = (i) => (i && i.pr_url && /^https:\/\/github\.com\//.test(i.pr_url) ? h("a", { href: i.pr_url, rel: "noopener noreferrer", target: "_blank" }, `#${i.pr_number}`) : i ? `#${i.pr_number}` : null);
    const open = (i) => i && i.state === "pr_open";
    const rows = [h("p", { class: "muted" }, "Rule 40: other threads see this task's files as claimed once a docs-only pull request that adds a row to the job board is merged. CPM opens it; you merge it on GitHub.")];
    if (st.problem) rows.push(h("div", { class: "note warn" }, st.problem));
    if (st.on_board) rows.push(h("p", null, `On the base branch's board: ${st.on_board}`));
    if (open(st.claim)) rows.push(h("p", null, "Claim pull request ", prLink(st.claim)));
    if (open(st.release)) rows.push(h("p", null, "Release pull request ", prLink(st.release)));
    const buttons = [];
    if (!open(st.claim) && !st.release) buttons.push(h("button", { id: "jb-claim", onclick: () => confirm("Open a docs-only pull request that claims this task's files on the job board?") && act(async () => { const r = await api.post(`/api/tasks/${id}/job-board/claim`, {}); setBanner(`Claim pull request #${r.pr_number} opened`, "info"); }, onDone) }, st.claim ? "Retry claim pull request" : "Claim files on the job board"));
    if (st.claim && !open(st.release)) buttons.push(h("button", { id: "jb-release", onclick: () => confirm("Open a docs-only pull request that releases this task's claim?") && act(async () => { const r = await api.post(`/api/tasks/${id}/job-board/release`, {}); setBanner(`Release pull request #${r.pr_number} opened`, "info"); }, onDone) }, st.release ? "Retry release pull request" : "Release the claim"));
    if (buttons.length) rows.push(h("p", { class: "row" }, buttons));
    box.replaceChildren(h("h3", null, "Job board"), ...rows);
  }).catch(() => box.replaceChildren(h("h3", null, "Job board"), h("p", { class: "muted" }, "Not available for this task.")));
  return box;
}

function integrationBox(task, integ, onDone) {
  const id = task.id;
  const pr = integ && integ.pr_url && /^https:\/\/github\.com\//.test(integ.pr_url) ? integ.pr_url : null;
  if (integ && integ.state === "pr_open") {
    return h("div", { class: "card", id: "integration" }, h("strong", null, "Pull request opened: "), pr ? h("a", { href: pr, rel: "noopener noreferrer", target: "_blank" }, `#${integ.pr_number}`) : `#${integ.pr_number}`,
      h("div", { class: "muted" }, `branch ${integ.branch}. CPM never merges: review and merge it on GitHub.`));
  }
  if (task.status !== "COMPLETED") return null;
  const note = h("textarea", { id: "pr-needs-pc", maxlength: 2000, placeholder: "What still needs the PC (Unity, devices, a manual check)? Shown in the pull request." });
  return h("div", { class: "card", id: "integration" }, h("h3", null, "Publish"),
    integ ? h("div", { class: "note warn" }, `A previous attempt stopped at “${integ.state}”. Retrying continues from there; nothing is pushed twice.`) : null,
    h("p", { class: "muted" }, "Pushes the task branch (never main, never forced) and opens a pull request with your saved GitHub token. CPM does not merge."),
    field("What still needs the PC", note),
    h("p", null, h("button", { class: "primary", id: "integrate", onclick: () => confirm("Push this branch to GitHub and open a pull request?") && act(async () => { const r = await api.post(`/api/tasks/${id}/integrate`, { needs_pc: note.value }); setBanner(`Pull request #${r.pr_number} opened`, "info"); }, onDone) }, "Push branch and open pull request")));
}

async function reviewQueue() {
  const list = await api.get("/api/projects");
  const per = await Promise.all(list.projects.map((p) => api.get(`/api/projects/${p.id}/tasks`).then((r) => (r.tasks || r).map((t) => ({ ...t, project: p.name })))));
  const all = per.flat();
  const want = ["TESTING", "REVIEWING", "CORRECTING", "COMPLETED"];
  const rows = all.filter((t) => want.includes(t.status));
  return rows.length
    ? h("table", null, h("thead", null, h("tr", null, ["Task", "Project", "Status", "Next step"].map((t) => h("th", null, t)))),
      h("tbody", null, rows.map((t) => h("tr", null, h("td", null, link(`#/task/${t.id}`, `#${t.id} ${t.title}`)), h("td", null, t.project), h("td", null, statusPill(t.status)),
        h("td", { class: "muted" }, { TESTING: "gate runs, or press it", REVIEWING: "independent review running", CORRECTING: "a worker is fixing the findings", COMPLETED: "publish a pull request" }[t.status])))))
    : empty("Nothing waits for review.");
}

async function githubCard() {
  const [st, gc] = await Promise.all([api.get("/api/credentials/github"), api.get("/api/gate/config")]);
  const token = h("input", { id: "gh-token", type: "password", autocomplete: "off", size: 50, placeholder: st.configured ? "saved — type to replace" : "github_pat_…" });
  const mc = h("input", { id: "gate-max", type: "number", min: "0", max: "5", value: gc.max_corrections });
  const auto = h("input", { id: "gate-auto", type: "checkbox", checked: gc.auto });
  return h("div", { class: "card" }, h("h2", null, "GitHub token and gate"),
    h("p", { class: "muted" }, `Token: ${st.configured ? "saved in the Windows credential store" : "not saved"}. Use a fine-grained token limited to the project repositories (contents: write, pull requests: write). It is never shown again.`),
    field("GitHub token", token),
    h("p", { class: "row" },
      h("button", { class: "primary", onclick: () => act(async () => { await api.put("/api/credentials/github", { token: token.value }); token.value = ""; }, () => route("#/git")) }, "Save token"),
      h("button", { class: "danger", onclick: () => confirm("Remove the saved GitHub token?") && act(() => api.del("/api/credentials/github"), () => route("#/git")) }, "Remove token")),
    h("div", { class: "row" }, field("Corrections per task", mc), h("label", null, auto, " run the gate by itself (needs the scheduler)")),
    h("p", null, h("button", { onclick: () => act(() => api.put("/api/gate/config", { max_corrections: Number(mc.value), auto: auto.checked }), () => route("#/git")) }, "Save gate settings")));
}

async function gitView() {
  const my = navToken;
  const [queue, card, list] = await Promise.all([reviewQueue(), githubCard(), api.get("/api/projects")]);
  const details = await Promise.all(list.projects.map((p) => api.get(`/api/projects/${p.id}`)));
  show(my, h("h1", null, "Git & review"),
    h("p", { class: "muted" }, "Worker results wait here as branches. The acceptance gate checks scope, tests, merge safety and an independent review; you publish a pull request. Workers never push to main and CPM never merges."),
    h("h2", null, "Waiting for review"), queue,
    h("h2", null, "Projects"),
    details.length ? details.map((p) => h("div", { class: "card" }, h("strong", null, link(`#/project/${p.id}/sources`, p.name)), h("div", { class: "muted" }, `integration: ${p.integration_mode}`), h("div", { class: "muted" }, p.git && p.git.is_repo ? JSON.stringify({ branch: p.git.branch, dirty_files: p.git.dirty_count }) : "not a git repository"))) : empty("No project yet."),
    card);
}

async function settingsView() {
  const my = navToken;
  const [wiz, cred] = await Promise.all([api.get("/api/wizard/status"), api.get("/api/credentials/anthropic")]);
  const cfg = wiz.config || {};
  const key = h("input", { id: "s-key", type: "password", autocomplete: "off", size: 50, placeholder: cred.configured ? "saved — type to replace" : "sk-ant-…" });
  const f = (id, v, extra) => h("input", { id, value: v ?? "", ...extra });
  const name = f("w-name", cfg.default_project_name); const dir = f("w-dir", cfg.project_dir, { size: 50 });
  const git = f("w-git", cfg.git_url); const gb = f("w-global", cfg.global_budget_usd, { type: "number", step: "0.01", min: "0.01" });
  const pb = f("w-proj", cfg.project_budget_usd, { type: "number", step: "0.01", min: "0" }); const tb = f("w-task", cfg.task_budget_usd, { type: "number", step: "0.01", min: "0" });
  const par = f("w-par", cfg.max_parallel_agents ?? 3, { type: "number", min: "1", max: "8" });
  const model = h("select", { id: "w-model" }, wiz.models.map((m) => h("option", { value: m, selected: m === (cfg.default_model || "claude-sonnet-5-5") }, m)));
  const effort = h("select", { id: "w-effort" }, ["low", "medium", "high", "xhigh", "max"].map((m) => h("option", { value: m, selected: m === (cfg.default_effort || "high") }, m)));
  const result = h("div", { id: "s-result", class: "muted", role: "status" });
  show(my, h("h1", null, "Settings"),
    h("div", { class: "card" }, h("h2", null, "Anthropic API key"),
      h("p", { class: "muted" }, `Status: ${cred.configured ? "saved in the Windows credential store" : "not saved"}${cred.storage_available ? "" : " (secure storage unavailable)"}. The key is never shown again after saving.`),
      field("API key", key),
      h("p", { class: "row" },
        h("button", { class: "primary", onclick: () => act(async () => { const r = await api.put("/api/credentials/anthropic", { api_key: key.value }); key.value = ""; result.textContent = r.message; }, () => route("#/settings")) }, "Save key"),
        h("button", { onclick: () => act(async () => { const r = await api.post("/api/credentials/anthropic/test", {}); result.textContent = r.message; }) }, "Test saved key"),
        h("button", { class: "danger", onclick: () => confirm("Remove the saved key?") && act(() => api.del("/api/credentials/anthropic"), () => route("#/settings")) }, "Remove key")),
      h("p", { class: "muted" }, "The format check is local; a wrong key is only discovered at the first live run. Billing is API-only and never switches silently."), result),
    h("form", { class: "card", onsubmit: (e) => {
      e.preventDefault();
      act(() => api.put("/api/wizard/config", { default_project_name: name.value, project_dir: dir.value, git_url: git.value || null, default_model: model.value, default_effort: effort.value, task_budget_usd: tb.value, project_budget_usd: pb.value, global_budget_usd: gb.value, max_parallel_agents: Number(par.value) }), () => route("#/settings"));
    } },
      h("h2", null, wiz.completed ? "Setup" : "First-run setup"),
      field("Default project name", name), field("Project folder", dir), field("Git remote (optional)", git),
      field("Monthly cap, global (USD, required)", gb), field("Cap per project (USD)", pb), field("Cap per task (USD)", tb),
      field("Parallel agents (1–8)", par), field("Default model", model), field("Default effort", effort),
      h("p", null, h("button", { class: "primary", type: "submit" }, "Save setup"))));
}

function tokenGate(message) {
  const my = navToken;
  const input = h("input", { id: "tok", type: "password", size: 50, autocomplete: "off" });
  show(my, h("h1", null, "Access token needed"),
    h("div", { class: "card" }, h("p", null, message || "Open CPM with scripts\\start.ps1 (it opens this page with the token), or paste the token printed by scripts\\show_token.ps1."),
      field("Install token", input),
      h("p", null, h("button", { class: "primary", onclick: () => { api.setToken(input.value.trim()); route("#/"); } }, "Continue"))));
}

// ---------------------------------------------------------------- router
async function route(to) {
  if (to && location.hash !== to) { location.hash = to; return; }
  const my = ++navToken;
  resetView();
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("on", a.dataset.route === (parts[0] || "")));
  if (!api.hasToken()) { tokenGate(); return; }
  view.replaceChildren(h("p", { class: "muted" }, "Loading…"));
  try {
    const [a, b, c] = parts;
    if (a === "projects") await projectsView();
    else if (a === "project" && b) await projectView(b, c || "board");
    else if (a === "task" && b) await taskView(b);
    else if (a === "agents") await agentsView();
    else if (a === "scheduler") await schedulerView();
    else if (a === "costs") await costsView();
    else if (a === "git") await gitView();
    else if (a === "settings") await settingsView();
    else await dashboardView();
    if (my === navToken) setBanner(null);
  } catch (e) {
    if (my !== navToken) return;
    if (e.status === 401) { api.clearToken(); tokenGate("The access token was refused. Paste the current one."); return; }
    show(my, h("h1", null, "Something went wrong"), errBox(e), h("button", { onclick: () => route() }, "Retry"));
  }
}

api.loadToken();
window.addEventListener("hashchange", () => { api.loadToken(); route(); });  // also catches a pasted #token= link
pollHealth();
route();
