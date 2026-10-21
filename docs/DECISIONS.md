# DECISIONS (Micah's answers win)

## 2026-10-21 — answers to PLAN.md section 1
- General: "ok pour tes recommandations" — every recommendation in PLAN.md section 1 is accepted: Q1 (API spending allowed under a hard monthly cap Micah sets in the app; mocks first; first live smoke test limited to about 2 USD, Haiku/Sonnet by default), Q2 (code in `D:\projects\ClaudeProjectManager`, OneDrive folder = docs only), Q4 (no paid third-party media API in v1), Q5 (branch + PR only, fine-grained GitHub token in Windows Credential Manager), Q6 (job board read-only in v1), Q7 (worktrees under `D:\projects\cpm-worktrees\`, one persistent Unity validation worktree), Q8 (3 coding agents, max 1 heavy task), Q9 (CLI subscription mode = interface only), Q10 (English UI), Q11 (no autostart).
- Project folder: Claude work happens in `D:\projects\ClaudeProjectManager` (approved on the device). CLAUDE.md, PLAN.md and docs are copied there; the OneDrive copy stays as a mirror of the documents.
- Q3 (tools on the PC), partial answer:
  - Unity 6000.3.25f1: Start Menu folder `C:\Users\xxobs\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Unity 6000.3.25f1`. The real `Unity.exe` path is still to be found by WP0 (usually `C:\Program Files\Unity\Hub\Editor\6000.3.25f1\Editor\Unity.exe`; must be verified, not assumed).
  - Blender 5.2: `C:\Program Files\Blender Foundation\Blender 5.2\blender-launcher.exe` (for batch/background runs use `blender.exe` in the same folder with `-b`; WP0 verifies).
  - Still unknown: local voice pipeline (TTS/training tools), the map generator and the 3D generation scripts in Nirandyr. WP0 inspects `D:\projects\game\tools\` read-only to list them, and the lane packages (WP13/WP14) use that list.
- Still open for Micah: the monthly cap amount (Q1).

## 2026-10-21 (later) — Q1 answered
- Micah: yes to API spending and to the 2 USD first live smoke test. "We must always know the remaining budget and work according to the plan; if the tokens run out there is a described procedure."
- Applied: CPM always shows remaining budget (global, project, task) and admits work only if it fits; at exhaustion it follows PLAN.md WP8 / master prompt section 12 (PAUSED_BUDGET, preserved worktree, handoff note per CLAUDE.md rule 15).
- The monthly cap amount was not given: it is set by Micah in the first-run wizard. Until a cap is configured, the scheduler admits no live (billable) run; mock runs are unaffected.

## 2026-10-21 (later) — Unity path
- Unity.exe: `D:\Unity\Editors\6000.3.25f1\Editor\Unity.exe` (given by Micah; WP0 only confirms it runs).

## 2026-10-21 (WP0) — findings awaiting Micah (recommendation applies until answered)
- Branch: the GitHub repo has no `main`; the default branch is `claude/project-thread-px0aun`. WP0 PR targets it. Recommended: create `main` from it after merge.
- T-20: Nirandyr `docs/procedures/api_parallel.md` forbids Unity/Blender/GPU for API jobs. Recommended: allow CPM lanes to use them under devlock / gpu_lock / BUILD_RUNNING, and update that procedure via a Nirandyr PR. Lanes WP13/WP14 wait for this answer.
- Confirmed paths: Unity.exe `D:\Unity\Editors\6000.3.25f1\Editor\Unity.exe`; Blender 5.2.2 `blender.exe` in `C:\Program Files\Blender Foundation\Blender 5.2\`.
- Maps: no overworld map generator found in Nirandyr tools, only the dungeon generator (`tools/sim/dungeon_gen.sh`). Question: is the dungeon generator the "map" lane target?

## 2026-10-21 (WP0 answers) — Micah: "je suis d'accord avec tout"
- T-20 accepted: CPM lanes may use Unity, Blender and GPU tools under devlock / gpu_lock / BUILD_RUNNING. A Nirandyr PR must update `docs/procedures/api_parallel.md` accordingly (to do, not done by CPM).
- Create `main` on `Xxobster/claudeapiweb` from the planning branch after PR #1 merges (not done yet).
- Map lane target (answer from the Nirandyr side, relayed by Micah): `tools/sim/zone_gen.sh` (`--world` for the world map) with `tools/sim/zonegen/village_gen.py`, `mountain_caves.py`, its tests and golden files `golden_frozen_v1.txt`, `golden_layouts*.txt`. `dungeon_gen.sh` is a separate "dungeons" lane. The map lane only runs the generator and tests, compares with the golden files and reports differences, never fixes (map frozen, Nirandyr decision 413). `zone_gen.sh` waits while `BUILD_RUNNING.lock` exists; no adb, no GPU. Not verified by WP0 (not found in the local checkout); WP14 must confirm the paths on `main` of Nirandyr.
