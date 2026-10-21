# THREATS (WP0, v0.0.1, 2026-10-21)

Risk list written before code. Each row: threat, mitigation (the design rule), where it is tested/owned. Severity H/M/L. Update with every WP (CLAUDE.md rules 2 and 6).

## Security
| ID | Sev | Threat | Mitigation | Owner/test |
|---|---|---|---|---|
| T-01 | H | API key leaks to frontend, logs, DB, git, prompts | Key only in Windows Credential Manager (keyring/DPAPI); never returned by any API; log redaction filter; child env allowlist; `.gitignore` for `.env`/db/logs; secret-scan of every diff before PR | WP1 redaction test, WP2 tests/security, WP10 gate |
| T-02 | H | Other websites call the local server (CSRF, DNS rebinding) | Bind 127.0.0.1; random install token; Host and Origin allowlist; no CORS; CSRF token on mutations; WebSocket checks Origin | WP2 |
| T-03 | H | Path traversal / junction escape out of approved roots | `PathNormalizer`: resolve real path, reject `..`, symlinks/junctions leaving root, UNC, device names, trailing dots/spaces; test with spaces and non-ASCII | WP2 |
| T-04 | H | Command injection | Only `subprocess` with argument lists; lane commands from allowlists; no shell strings built from task/UI input; `bash -c` forbidden | WP2, WP12 |
| T-05 | H | Prompt injection via managed repo files (CLAUDE.md, TODO, issues, web) causes tool misuse | Repo instructions are guidance, never authorization; hooks/MCP/scripts of a managed repo off until workspace marked trusted; tool allowlist per lane; deny list for `git push`, `rm -rf`, network tools | WP3, WP5 |
| T-06 | H | Permission bypass (`bypassPermissions` used by default) | Default mode is not bypass; allowed tools explicit; `can_use_tool` callback logs decisions; bypass only inside a disposable worktree and only if Micah enables it per lane (SDK_NOTES) | WP5 |
| T-07 | M | Secrets committed by a worker (keys in generated files) | Diff scanner (regex + high-entropy) in the acceptance gate; block PR | WP10 |
| T-08 | M | GitHub token over-privileged | Fine-grained token, PR+contents on `Xxobster/Nirandyr` only (Q5); stored in Credential Manager; used only by the git integration module | WP10 |
| T-09 | M | Supply chain (unpinned deps, SDK updates) | Pin versions in lockfile; `pip-audit` in CI; review SDK upgrades | WP1, WP16 |
| T-10 | M | Worker executes untrusted generated code (Blender/Unity scripts) | Run only in worktree; scripts reviewed in diff; Job Object kill-on-close; no network for lane commands unless allowlisted | WP12-14 |
| T-11 | L | Log files contain file contents with PII/secrets | Logs store events/metadata; redaction; retention setting | WP1 |

## Billing and budget
| ID | Sev | Threat | Mitigation | Owner/test |
|---|---|---|---|---|
| T-12 | H | Silent switch from subscription to API billing (or the reverse) because `ANTHROPIC_API_KEY` is in the inherited environment | Child env built explicitly; billing mode stored per run and shown in UI; `check_env.ps1` reports key presence; startup warns if a key is set in the user env | WP2, WP5 |
| T-13 | H | Concurrent workers overspend a shared cap | Admission reserves estimated cost in a `BEGIN IMMEDIATE` transaction; reconcile with actual usage; per-run `max_budget_usd` only as backstop; no live run without a configured cap | WP8 property test |
| T-14 | M | Wrong prices or model IDs | Price table in DB, versioned, sourced from SDK_NOTES with date; UI labels "estimate" vs "reconciled" | WP8 |
| T-15 | M | Duplicate usage events inflate/deflate ledger | Dedup key = SDK message id | WP8 |
| T-16 | M | Float rounding in money | `Decimal` in code, integer micro-dollars in DB | WP8 |
| T-17 | L | Auto top-up of credits | Never enabled (CLAUDE.md rule 7) | docs |

## Concurrency and integrity
| ID | Sev | Threat | Mitigation | Owner/test |
|---|---|---|---|---|
| T-18 | H | Worker edits files Micah/Desktop is editing, or a path claimed on the job board | Worktrees only; reservations with prefix overlap; Desktop claims; job-board adapter; merge-tree dry run before PR; refuse if main tree dirty in scope | WP6, WP10 |
| T-19 | H | Heavy jobs overlap (Unity, GPU voice, builds); GPU driver crashed once per Nirandyr coordinator note | Typed exclusive resources (`unity_editor`, `gpu`, `blender`, `device`); refuse while `BUILD_RUNNING.lock` or any gpu-lock file exists; max 1 heavy task (Q8); locks released in `finally` even on failure (rule 27) | WP6, WP7 |
| T-20 | H | **Rule clash:** Nirandyr `api_parallel.md` forbids Unity/Blender/GPU for API jobs; PLAN.md lanes use them | Needs Micah's explicit go (question below). Until then lanes 13/14 are not built; text/code tasks only | Open question |
| T-21 | M | Two processes open the same Unity project | `unity_editor` exclusive; one persistent validation worktree; never open Desktop's main tree | WP14 |
| T-22 | M | Crash leaves orphan processes/locks/worktrees | Job Object tree-kill; leases + heartbeat; restart reconciliation; never delete a worktree with changes | WP5, WP9 |
| T-23 | M | SQLite corruption (OneDrive, concurrent writers) | DB outside OneDrive (verified: repo is on D:); WAL; single writer discipline; `.backup` | WP1, WP9 |
| T-24 | M | Case/slash/`..`/short-name aliasing makes two reservations look disjoint | Normalize before compare; `.meta` pairing | WP6 |
| T-25 | M | Agent claims success without evidence | Deterministic acceptance gate; no auto-complete | WP10 |
| T-26 | L | Windows path with spaces or non-ASCII (OneDrive `20 - PROJECTS`) | Structured args; tests with such paths | all |

## Process risks
- PLAN.md says PRs go to `main`; the GitHub repo has no `main`, the default branch is the planning branch `claude/project-thread-px0aun`. WP0's PR targets that branch. Micah may want `main` created from it (one line decision).
- Scope is large (7 phases); vertical slices S1-S5 stay.

## Questions for Micah (answers recorded in DECISIONS.md)
1. T-20: may CPM lanes run Unity, Blender and GPU tools (under devlock/gpu_lock/BUILD_RUNNING), given Nirandyr's `api_parallel.md` forbids it for the PowerShell procedure? Recommendation: yes for CPM only, because CPM enforces the same locks; update `api_parallel.md` via a Nirandyr PR to say so.
2. Create a `main` branch on `Xxobster/claudeapiweb`? Recommendation: yes, from the planning branch after this PR merges.
