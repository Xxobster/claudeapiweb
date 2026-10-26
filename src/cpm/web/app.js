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
    claims.claims.length ? h("table", null, h("thead", null, h("tr", null, ["Path", "Note", "Expires", "Tasks affected", ""].map((t) => h("th", null, t)))),
      h("tbody", null, claims.claims.map((c) => h("tr", null, h("td", null, c.path), h("td", null, c.note || "—"), h("td", null, c.expires_at ? when(c.expires_at) : "never"), h("td", null, (c.affected_tasks || []).length ? c.affected_tasks.map((t) => link(`#/task/${t}`, `#${t} `)) : "—"), h("td", null, h("button", { onclick: () => act(() => api.del(`/api/desktop-claims/${c.id}`), onDone) }, "Release")))))) : h("p", { class: "muted" }, "No claim. Workers will not touch a path you claim."),
    h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); act(() => api.post(`/api/projects/${project.id}/desktop-claims`, { path: path.value, note: note.value }), onDone); } }, path, note, h("button", { type: "submit" }, "Claim path")),
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
  if (!why.blockers.length) return h("div", { class: "note", id: "conflicts" }, "No lock or reservation blocks this task right now.");
  return h("div", { class: "note warn", id: "conflicts" }, h("strong", null, "Blocked by:"),
    h("ul", null, why.blockers.map((b) => h("li", null, `${b.message}`, b.holder_task_id ? [" (", link(`#/task/${b.holder_task_id}`, `task #${b.holder_task_id}`), ")"] : null))),
    h("button", { onclick: () => confirm("Release this task's own reservations? Only do this if nothing is working on it.") && act(() => api.post(`/api/tasks/${taskId}/reservations/release`), onDone) }, "Release this task's reservations"));
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

async function gitView() {
  const my = navToken;
  const list = await api.get("/api/projects");
  const details = await Promise.all(list.projects.map((p) => api.get(`/api/projects/${p.id}`)));
  show(my, h("h1", null, "Git & review"),
    details.length ? details.map((p) => h("div", { class: "card" }, h("strong", null, link(`#/project/${p.id}/sources`, p.name)), h("div", { class: "muted" }, `integration: ${p.integration_mode}`), h("div", { class: "muted" }, p.git && p.git.is_repo ? JSON.stringify({ branch: p.git.branch, dirty_files: p.git.dirty_count }) : "not a git repository")) ) : empty("No project yet."),
    h("div", { class: "note" }, "Diff review, the acceptance gate and pull-request creation arrive with the test/review work package. Workers never push to main; their results will appear here as branches for you to review."));
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
