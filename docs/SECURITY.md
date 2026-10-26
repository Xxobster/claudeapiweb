# SECURITY (v0.18.0, 2026-10-26)

Scope: one user, one Windows PC, loopback only. The full threat list is `docs/THREATS.md`; this page is the summary of what is enforced and where the tests are.

## Model
CPM is a local server that can start processes, write to git worktrees and (when allowed) spend money. The attackers considered: a web page in your browser talking to localhost, another local user or process, hostile content in a managed repository (files, test output, diffs), a confused or prompt-injected worker, and your own mistakes (spend, wrong branch).

## Enforced controls
| Area | Control | Tests |
|---|---|---|
| Network | Binds loopback only (config refuses anything else); Host header allow-list (DNS rebinding); no CORS; API docs page off | `tests/security/test_guard.py` |
| Access | Install token on every `/api` route and WebSocket (one-time ticket for streams); CSRF header and same-origin `Origin` on every mutation | `test_guard.py`, `test_hardening.py` (walks every route) |
| Browser | CSP without inline script or remote resources, `nosniff`, `X-Frame-Options: DENY`, no-store; UI builds DOM with `textContent` only; no key in the frontend; token only in this origin's storage | `test_hardening.py`, `tests/ui/e2e.mjs` |
| API key | Windows Credential Manager; never returned, logged, stored in DB/git/PR text; workers get an explicit credential-free environment | `test_key_exposure.py`, `test_childenv.py`, `test_redaction.py` |
| Paths | Approved roots, no traversal, no junction/symlink escape, reserved names, case and 8.3 forms | `test_paths.py`, `test_locks_security.py` |
| Commands | Structured argument lists only; no `shell=True`, `eval`, `exec`, `os.system`, pickle, unsafe YAML (source tripwire); lane commands are exact allow-lists | `test_injection.py`, `test_hardening.py`, `tests/lanes/` |
| Workers | Own worktree, tool permission policy, protected paths, process-tree kill, heartbeat watchdog, budget backstop | `tests/workers/` |
| Git | Only `claude/*` branches, never a protected branch, never force, no merge/rebase/reset/clean; PR needs a click, a passed gate and the same tree | `tests/review/` |
| Money | `Decimal` only (source tripwire); atomic admission; explicit billing mode; live runs off by default | `tests/budget/`, `test_hardening.py` |
| Supply chain | Hash-locked dependencies, `pip-audit` and lockfile freshness in CI | `.github/workflows/ci.yml` |
| Scripts | No download or evaluation of code, no execution-policy bypass inside scripts; uninstall never deletes data | `test_hardening.py` |

## What is deliberately not protected
Anyone who can read `data\install_token` (your Windows account) can drive CPM: same trust as the DB. Backups are unencrypted. A trusted workspace runs repository code (tests, Unity, Blender) with your rights; trust only repositories you control. See `docs/KNOWN_LIMITATIONS.md`.

## Reporting
Private project: tell Micah in the thread. Every change records its security findings in its PR and in `docs/IMPACT_MAP.md`.

## WP16 review (2026-10-26)
Whole-codebase sweep, no new vulnerability found. Added source tripwires (dangerous calls, string-built subprocess, float money, frontend key material, `innerHTML`/`eval`, remote page resources, secrets in docs/profiles/scripts) and a route-walking test: every `/api` route refuses a caller without the token, and every mutating route refuses a foreign `Origin` even with a valid token. Not covered by this sweep (needs the PC): Credential Manager round trip, Job Object tree kill, real GitHub token flow.
