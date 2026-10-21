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
