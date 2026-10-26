// Playwright check of the web UI against a real CPM server (mock runs only, no key, no network).
// Run from the repo root:  node tests/ui/e2e.mjs   (needs Node, `playwright`, git, and the dev venv)
// Not part of CI (CI has no Node/browser); run it on the PC or in the cloud container and say so.
import { createRequire } from "node:module";
import { spawn, execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";

const { chromium } = createRequire(import.meta.url)("playwright"); // honours NODE_PATH

const port = 18765;
const base = `http://127.0.0.1:${port}`;
const root = mkdtempSync(join(tmpdir(), "cpm-e2e-"));
const data = join(root, "data");
function repo(name) {
  const p = join(root, name);
  mkdirSync(p, { recursive: true });
  const g = (...a) => execFileSync("git", ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", p, ...a]);
  g("init", "-q", "-b", "main");
  writeFileSync(join(p, "README.md"), "# demo\n");
  writeFileSync(join(p, "CLAUDE.md"), "# rules\n");
  g("add", "-A"); g("commit", "-q", "-m", "init");
  return p;
}
const repos = [repo("alpha"), repo("beta")];
const server = spawn(process.env.PYTHON || "python3", ["tests/ui/serve_for_e2e.py", data, String(port), join(root, "wt")], { stdio: "inherit" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
for (let i = 0; i < 60; i++) { try { if ((await fetch(`${base}/health`)).ok) break; } catch {} await sleep(250); }
const token = readFileSync(join(data, "install_token"), "utf8").trim();

const browser = await chromium.launch();
const ctx = await browser.newContext();
const errors = [];
try {
  const a = await ctx.newPage();
  a.on("pageerror", (e) => errors.push(String(e)));
  a.on("console", (m) => m.type() === "error" && errors.push(m.text()));

  // 0. No token: gate, nothing leaks.
  await a.goto(base);
  assert.match(await a.textContent("h1"), /Access token needed/);

  // 1. Token in the fragment is consumed and removed from the address bar.
  await a.goto(`${base}/#token=${token}`);
  await a.waitForSelector("text=Dashboard");
  assert.ok(!a.url().includes(token), "token must leave the URL");
  await a.waitForSelector("text=connected");
  await a.waitForSelector("text=Setup is not finished");
  await a.waitForSelector("text=No project yet");

  // 2. Setup: key + cap.
  await a.goto(`${base}/#/settings`);
  await a.fill("#s-key", "sk-ant-api03-" + "x".repeat(40));
  await a.click("text=Save key");
  await a.waitForSelector("text=saved in the Windows credential store");
  assert.ok(!(await a.content()).includes("sk-ant-api03-xxxx"), "key must not be echoed");
  await a.fill("#w-name", "Alpha"); await a.fill("#w-dir", repos[0]);
  await a.fill("#w-global", "20"); await a.fill("#w-proj", "10"); await a.fill("#w-task", "2");
  await a.click("text=Save setup");
  await a.waitForSelector("h2:text-is('Setup')");

  // 3. Two projects.
  for (const [n, p] of [["Alpha", repos[0]], ["Beta", repos[1]]]) {
    await a.goto(`${base}/#/projects`);
    await a.waitForSelector("#p-name", { timeout: 5000 }).catch(async () => { throw new Error("projects view: " + a.url() + " " + (await a.textContent("main")).slice(0,80) + " ERR " + errors.join("|")); });
    await a.fill("#p-name", n); await a.fill("#p-path", p);
    await a.click("text=Add project");
    await a.waitForSelector(`a:text-is('${n}')`);
  }

  // 4. Trust explanation on an untrusted project.
  await a.click("a:text-is('Alpha')");
  await a.waitForSelector("text=workers will not load this project's CLAUDE.md");

  // 5. Two tabs, one project each, a mock run each, live log in both.
  const b = await ctx.newPage();
  b.on("pageerror", (e) => errors.push(String(e)));
  await b.goto(`${base}/#/projects`);
  await b.click("a:text-is('Beta')");
  for (const [page, title] of [[a, "alpha task"], [b, "beta task"]]) {
    await page.click("text=Add tasks");
    await page.fill("#t-title", title);
    await page.selectOption("#t-mode", "mock");
    await page.click("text=Create task");
    await page.click(`a:text-is('${title}')`);
    await page.click("text=→ READY");
    await page.waitForSelector("text=Mock run");
    await page.selectOption("#r-scen", "slow");
    await page.click("text=Start run");
  }
  for (const page of [a, b]) {
    await page.waitForSelector("text=streaming", { timeout: 15000 }).catch(() => {});
    await page.waitForFunction(() => /run_created/.test(document.getElementById("run-log")?.textContent || ""), null, { timeout: 15000 })
      .catch(async () => { throw new Error("no live log: " + (await page.textContent("main")).slice(0, 1500) + " ERR " + errors.join("|")); });
  }
  await a.goto(`${base}/#/agents`);
  // Runs are short; either listed or already finished, but the page must render without fake rows.
  await a.waitForSelector("h1:text('Agent monitor')");
  await a.goto(`${base}/#/projects`); await a.click("a:text-is('Alpha')");
  await a.waitForSelector("text=alpha task");
  await a.waitForFunction(() => document.body.textContent.includes("TESTING (1)"), null, { timeout: 20000 })
    .catch(async () => { throw new Error("board: " + (await a.textContent("main")).slice(0, 600)); });

  // 6. Costs and git pages render real (empty) data.
  await a.goto(`${base}/#/costs`); await a.waitForSelector("text=Recorded spend (all time");
  await a.goto(`${base}/#/git`); await a.waitForSelector("text=never push to main");

  // 6b. Cost center (WP8): month meter, thresholds, top-up.
  await a.goto(`${base}/#/costs`);
  await a.waitForSelector("#month-card");
  assert.match(await a.textContent("#month-card"), /of \$20\.0000/);
  await a.waitForSelector("text=Price table");
  a.once("dialog", (d) => d.accept());
  await a.fill("#c-topup", "5"); await a.click("text=Add to cap");
  await a.waitForFunction(() => /of \$25\.0000/.test(document.getElementById("month-card")?.textContent || ""), null, { timeout: 5000 });

  // 6c. A task that runs out of budget offers the five options (driven through the API, mock worker, no cost).
  const hdr = { "X-CPM-Token": token, "Content-Type": "application/json" };
  const sess = await (await fetch(`${base}/api/session`, { headers: hdr })).json();
  const post = async (path, body) => { const r = await fetch(`${base}${path}`, { method: "POST", headers: { ...hdr, "X-CPM-CSRF": sess.csrf }, body: JSON.stringify(body) }); assert.ok(r.ok, `${path} ${r.status}`); return r.json(); };
  const alphaId = (await (await fetch(`${base}/api/projects`, { headers: hdr })).json()).projects.find((p) => p.name === "Alpha").id;
  const burst = await post(`/api/projects/${alphaId}/tasks`, { title: "burst task", billing_mode: "mock" });
  await post(`/api/tasks/${burst.id}/transition`, { to: "READY" });
  await post(`/api/tasks/${burst.id}/runs`, { scenario: "budget_burst", scenario_args: { step_microusd: 600000 }, max_budget_microusd: 1000000 });
  await a.goto(`${base}/#/task/${burst.id}`);
  await a.waitForSelector("#budget-actions", { timeout: 15000 }).catch(async () => { await a.goto(`${base}/#/task/${burst.id}`); await a.waitForSelector("#budget-actions", { timeout: 10000 }); });
  await a.goto(`${base}/#/costs`);
  await a.waitForSelector("text=stopped for budget");
  await a.goto(`${base}/#/task/${burst.id}`);
  await a.waitForSelector("#budget-actions");
  await a.fill("#b-amount", "1"); a.once("dialog", (d) => d.accept());
  await a.click("text=Add budget");
  await a.waitForFunction(() => /of \$3\.0000/.test(document.getElementById("task-budget")?.textContent || ""), null, { timeout: 5000 });

  // 6d. Reservations and locks: claim a path, see it, release it.
  await a.goto(`${base}/#/agents`);
  await a.waitForSelector("text=Reservations and locks");
  await a.fill(`#claim-path-${alphaId}`, "docs/");
  await a.click(`[data-project='${alphaId}'] >> text=Claim path`);
  await a.waitForSelector(`[data-project='${alphaId}'] td:text-is('docs/')`, { timeout: 5000 });
  await a.click(`[data-project='${alphaId}'] >> text=Release`);
  await a.waitForSelector(`[data-project='${alphaId}'] >> text=No claim`, { timeout: 5000 });

  // 6e. Scheduler screen (WP7 API): queue order, refusal reason, limits, API auto-retry toggle (mock tasks only).
  const sched = (await post(`/api/projects/${alphaId}/tasks`, { title: "sched waits", billing_mode: "api" }));
  await post(`/api/tasks/${sched.id}/transition`, { to: "READY" });
  await post("/api/scheduler/pause", {});
  await a.goto(`${base}/#/scheduler`);
  await a.waitForSelector("text=paused: nothing new starts");
  await a.waitForSelector(`td:has-text('sched waits')`);
  await a.waitForSelector("text=Workers at once");
  await a.waitForSelector("text=Automatic retry of API tasks:");
  await a.click("button:has-text('Resume')");
  await a.waitForSelector("text=running", { timeout: 5000 });
  await a.click("button:has-text('Run one pass')");
  await a.waitForSelector("td >> text=deferred: ", { timeout: 8000 }).catch(async (e) => { console.log(await a.textContent("main")); throw e; });  // API task refused: live runs are off, shown with its reason
  a.once("dialog", (d) => d.accept());
  await a.click("text=Turn on automatic retry for API tasks");
  await a.waitForSelector("text=Turn off automatic retry for API tasks");
  await a.click("text=Turn off automatic retry for API tasks");
  await a.waitForSelector("text=Turn on automatic retry for API tasks");

  // 6e2. Lanes screen (WP12 API): the project tab and the lane box on a task page. The API answer is stubbed in the
  // browser (profiles/ has no lane for the throwaway project); the real endpoint is covered by tests/lanes.
  const laneBody = { lanes: [{ id: "voice", type: "voice", title: "Voice lines", description: "d", enabled: true, resources: [{ type: "gpu", name: null }],
    commands: [{ name: "tts", program: "tts.exe", prefix: [], flags: { "--out": "out_path" }, positional: "none", needs_trust: false, description: "speak" }],
    validators: [{ kind: "files_exist", params: { glob: "a/*.wav" }, severity: "error" }],
    artifacts: { asset_paths: ["assets/voice/"], extensions: [".wav"], binary_extensions: [".wav"], max_file_bytes: 5000000, binary_policy: "ask" },
    report: { required_sections: ["Summary"], max_chars: 20000 }, task_template: {}, required: { tools: {}, paths: {} }, problems: ["required tool 'tts' not found at C:\\x"] }] };
  await a.route(`**/api/projects/${alphaId}/lanes`, (r) => r.fulfill({ json: laneBody }));
  await a.goto(`${base}/#/project/${alphaId}/lanes`);
  await a.waitForSelector("[data-lane='voice'] >> text=Allowed commands");
  await a.waitForSelector("[data-lane='voice'] >> text=not ready on this PC");
  await a.waitForSelector("[data-lane='voice'] >> text=stop and ask a person for each binary");
  await a.waitForSelector("[data-lane='voice'] >> text=The final answer must contain: Summary");
  const lt = await post(`/api/projects/${alphaId}/tasks`, { title: "no lane task", billing_mode: "mock" });
  await a.goto(`${base}/#/task/${lt.id}`);
  await a.waitForSelector("text=No description.");
  assert.equal(await a.locator("#task-lane").count(), 0, "a task without a lane has no lane box");
  await a.route(`**/api/tasks/${lt.id}`, async (r) => { const real = await (await r.fetch()).json(); await r.fulfill({ json: { ...real, lane: "voice" } }); });
  await a.goto(`${base}/#/projects`);
  await a.goto(`${base}/#/task/${lt.id}`);
  await a.waitForSelector("#task-lane >> text=Voice lines");
  await a.waitForSelector("#task-lane >> text=not ready on this PC");
  await a.unroute(`**/api/tasks/${lt.id}`);
  await a.unroute(`**/api/projects/${alphaId}/lanes`);

  // 6f. Review (WP10): a finished mock task is judged by the gate, its diff is shown, publishing without a remote is refused.
  const rv = await post(`/api/projects/${alphaId}/tasks`, { title: "review me", billing_mode: "mock", acceptance: ["the output file exists"] });
  await post(`/api/tasks/${rv.id}/transition`, { to: "READY" });
  await post(`/api/tasks/${rv.id}/runs`, { scenario: "success" });
  await a.goto(`${base}/#/task/${rv.id}`);
  await a.waitForSelector("#gate-run", { timeout: 15000 }).catch(async () => { await a.goto(`${base}/#/task/${rv.id}`); await a.waitForSelector("#gate-run", { timeout: 10000 }); });
  await a.waitForSelector(`td:text-is('mock_output_task${rv.id}.txt')`);
  await a.click(`tr:has-text('mock_output_task${rv.id}.txt') >> text=Diff`);
  await a.waitForSelector("pre.diff .da:has-text('mock result for task')");
  await a.click("#gate-run");
  await a.waitForSelector("text=Independent review (approved)", { timeout: 15000 });
  await a.waitForSelector("#integrate");
  await a.fill("#pr-needs-pc", "nothing to do on the PC");
  a.once("dialog", (d) => d.accept());
  await a.click("#integrate");
  await a.waitForSelector("text=origin", { timeout: 10000 });  // banner: this fixture has no github.com remote
  errors.splice(0, errors.length, ...errors.filter((e) => !/status of 409/.test(e)));  // the refused publish is expected
  await a.goto(`${base}/#/git`);
  await a.waitForSelector("text=Waiting for review");
  await a.waitForSelector(`td:has-text('review me')`);
  await a.fill("#gh-token", "github_pat_" + "k".repeat(30)); await a.click("text=Save token");
  await a.waitForSelector("text=Token: saved in the Windows credential store");
  assert.ok(!(await a.content()).includes("github_pat_kkkk"), "token must not be echoed");

  assert.deepEqual(errors, [], errors.join("\n"));  // no page or console error while connected

  // 7. Disconnected state.
  server.kill();
  await a.waitForSelector("text=disconnected", { timeout: 15000 });
  console.log("UI e2e: all checks passed");
} finally {
  await browser.close();
  server.kill();
}
