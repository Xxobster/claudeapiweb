# ARCHITECTURE (WP0 design lock, v0.0.1, 2026-10-21)

Refines PLAN.md section 3. If this file and PLAN.md differ, PLAN.md wins until a decision in `docs/DECISIONS.md` says otherwise. Every SDK claim cites `docs/SDK_NOTES.md`.

## 1. Verified environment (scripts/check_env.ps1, run 2026-10-21)
| Item | Value |
|---|---|
| OS | Windows 11 Home 10.0.26200 |
| Shell | Windows PowerShell 5.1 only (no `pwsh`); Git Bash available |
| Python | 3.12.10 (`C:\Users\xxobs\AppData\Local\Programs\Python\Python312`) |
| git / gh | 2.54.0 / 2.97.0, `gh` logged in as `Xxobster` |
| Node / uv | v24.19.0 / 0.12.1 |
| Claude Code CLI | 2.1.289 (`C:\Users\xxobs\.local\bin\claude.exe`) |
| Unity | `D:\Unity\Editors\6000.3.25f1\Editor\Unity.exe` exists |
| Blender | 5.2.2 LTS, `blender.exe` (batch) and `blender-launcher.exe` in `C:\Program Files\Blender Foundation\Blender 5.2\` |
| Free disk | C: 76 GB, D: 155 GB |
| API key env | `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are NOT set (good: no silent API billing) |
| Missing | `D:\projects\cpm-worktrees` (created by WP5), OneDrive docs path in the check script returned false (path to re-verify, docs mirror only) |

Consequences: all CPM scripts must run under PowerShell 5.1 (no `&&`, no `?:`); `pwsh` is optional. Child processes use `subprocess` with argument lists.

## 2. Components
```
Browser (127.0.0.1:8765, token + Origin check)
   | HTTP + WebSocket
FastAPI backend = supervisor  ---- SQLite (WAL, FK on, busy_timeout)
   |-- scheduler (one asyncio task, deterministic)
   |-- reservations + lock adapters (CPM table + Nirandyr locks)
   |-- budget ledger (admission control)
   |-- git policy engine (worktrees, merge-tree, pr_only)
   |-- discovery (CLAUDE.md, docs sources, git state)
   '-- spawns per task:  python -m cpm.workers.run --task <id>   (Windows Job Object)
                              |-- Provider: Mock | AgentSdk (API-billed) | ClaudeCli (stub)
                              '-- cwd = git worktree under D:\projects\cpm-worktrees\<project>\<task>
```
Layout, stack and process model: PLAN.md section 3 (unchanged).

## 3. Key design rules
1. **One writer per truth.** All task state changes go through `transition()` with an allowed-transition table; each change appends an `events` row.
2. **Admission before spawn.** Order: ready queue, conflict check (reservations, Desktop claims, external locks), budget admission (`BEGIN IMMEDIATE`), spawn. Anything failing leaves the task READY/BLOCKED with a visible reason.
3. **Billing mode is explicit per run** (`api` or `cli`), stored on the run, never switched silently. The child env is built from an allowlist; `ANTHROPIC_API_KEY` is passed only to `api` runs (SDK_NOTES: auth precedence).
4. **No live run without a configured cap** (DECISIONS 2026-10-21 Q1). Mock runs are unaffected.
5. **Workers only touch their worktree.** The managed project's main tree is read-only for CPM; the Unity validation worktree is a single persistent one.
6. **Acceptance is deterministic** (PLAN.md section 3 "Task acceptance gate"); an agent saying "done" is never enough.
7. **Windows-first abstractions**: `ProcessTree` (Job Object / psutil fallback), `SecretStore` (keyring / DPAPI / in-memory test double), `PathNormalizer` (case-insensitive, `\`->`/`, no `..`, junction-resolving, `.meta` pairing). These keep logic testable on Linux CI.

## 4. Nirandyr integration surface (read-only inspection of `D:\projects\game\tools`, 2026-10-21)
| Concern | What exists | CPM adapter / lane use |
|---|---|---|
| Device locks | `tools/devices/devlock.py` (`run`, `status`, `bump`, `hold`, `release`; file locks, renewed every minute, prio 1-4) | `devlock` adapter calls it with structured args, read `status` for display |
| GPU lock | `tools/gpu_lock.sh`: lock files in `D:/projects/gpu-locks/<tool>-<pid>.lock`, also creates `D:/projects/game-build/BUILD_RUNNING.lock`; GPU jobs (voice, art) pause while any file exists | `gpu_lock` + `build_lock` adapters: watch the folder/file; heavy tasks refuse to start while a Unity-run lock exists |
| Unity tests | `tools/run-editmode-tests.ps1`: finds `Unity.exe` under `D:\Unity\Editors`, `-batchmode -nographics -runTests -testPlatform EditMode`, results `Logs/editmode-results.xml` (NUnit), TEMP=`D:\unity-temp` | Unity validation lane (WP14) runs the same command on the dedicated validation worktree and parses the XML |
| Voice | `tools/voice/`: `render_fixed.py` (Kokoro-82M int8 ONNX, Piper fallback, own `.venv` at `tools/voice/.venv`), `fetch_kokoro.sh`, `story_qc.py`, `make_ab.py`; output `Assets/StreamingAssets/Voice/lines/<id>.rva` + `index.json` with hashes; `--check` mode exits 1 on missing/stale audio | Voice lane (WP13): allowlist `render_fixed.py [--check]`, validators from `index.json`; GPU/voice priority rules from Nirandyr |
| 3D | `tools/art/*_build.py`: run as `blender -b --python tools/art/<x>.py -- args`, export FBX via `bpy.ops.export_scene.fbx` into `Assets/Game/V2/...`; `fbx_preview.py`, `lod_preview.py`, `inspect_hero.py` exist | 3D lane (WP14): allowlist per script; validators on exported FBX. Note `drakhor_api.ps1` already does "Claude API -> spec JSON -> Blender -> FBX" |
| Maps | No general map-image tool found. `tools/sim/dungeon_gen.sh` + `tools/sim/dungeongen/{dungeon_gen.py,render.py,golden_dungeons.txt}` generate dungeon plans/maps with golden-file checks; `art/gen_map_icons.py` makes icons only | Map lane (WP14) targets the dungeon generator and its golden check; overworld map generation: Q to Micah if wanted |
| Compute jobs | `tools/jobs/` queue (VPS runner, `python` jobs only) | Out of scope for CPM v1; mention only |
| Existing API procedure | `docs/procedures/api_parallel.md` (Nirandyr rule 50): isolated text jobs only, worktree `D:\projects\game-api`, explicit budget, key only in user env var | CPM must stay compatible: CPM is the managed, safer version of that procedure. **Clash to resolve (see THREATS T-20):** that procedure forbids Unity/Blender/GPU for API jobs, while PLAN.md lanes drive them. Needs Micah's go that CPM lanes may run them under the existing locks |

## 5. Open items for later packages
- Overworld map generator: confirm with Micah whether one is wanted (Q3 partly answered).
- Voice venv: lanes must call `tools/voice/.venv/Scripts/python.exe`; CPM never installs into it.
- Whether `D:\projects\game-build` and `D:\projects\gpu-locks` paths are stable (both hard-coded in Nirandyr scripts); profile stores them, not the code.

## Module map as built (v0.18.0)
`api/` HTTP routes (one router per area; reference in `docs/API.md`) · `security/` token, CSRF/Origin/Host guard, secret store, path validator, child env · `db/` SQLAlchemy models, migrations, settings · `discovery/` read-only project inspection · `tasks/` state machine, DAG, TODO import · `workers/` runner, supervisor, process-tree kill, worktrees, permission policy · `providers/` MockProvider, AgentSdkProvider (live-gated), key check · `locks/` reservations, Desktop claims, external lock adapters, admission · `scheduler/` tick loop and limits · `budget/` price table, ledger, admission · `recovery/` restart reconciliation, backups · `review/` acceptance gate, PR integration, job board writer · `lanes/` lane framework, voice, 3D, map, Unity validators · `web/` static UI. Flow: UI -> `api/` -> services -> SQLite; the scheduler admits READY tasks (locks + budget), the supervisor spawns a worker child per task in its own worktree, the gate alone moves a finished task to COMPLETED, publishing a PR is a click.
