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
  await a.goto(`${base}/#/costs`); await a.waitForSelector("text=Recorded spend per project");
  await a.goto(`${base}/#/git`); await a.waitForSelector("text=never push to main");

  assert.deepEqual(errors, [], errors.join("\n"));  // no page or console error while connected

  // 7. Disconnected state.
  server.kill();
  await a.waitForSelector("text=disconnected", { timeout: 15000 });
  console.log("UI e2e: all checks passed");
} finally {
  await browser.close();
  server.kill();
}
