# KNOWN LIMITATIONS (v0.18.0, 2026-10-26)

## Needs Micah's PC (nothing here can be proved from the cloud/CI)
1. **First live run** (2 USD, `CPM_ALLOW_LIVE_RUNS=1`): never done; needs Micah's go. Until then every run is a mock run.
2. **Windows-only parts** (tests marked `windows`): Job Object tree kill, Credential Manager key and GitHub token round trip, PowerShell scripts (`install.ps1`, `make_shortcut.ps1`, `start.ps1`), shortcut creation.
3. **Nirandyr profile guesses** (R-WP14-2, R-WP10-4): Blender path and scripts, `zone_gen.sh` options, golden file paths, `Assets/StreamingAssets/Voice/index.json` shape, real `docs/JOB_BOARD.md` layout, test commands. Wrong guesses fail closed (task waits or fails with a reason). Fix by editing `profiles/nirandyr.yaml` after one look.
4. **GitHub**: real PR creation and the job-board claim PR against github.com with a fine-grained token (R-WP10-5, R-WP11-2).
5. Voice lane (WP13): `python` must resolve to the voice venv (R-WP13-1); Kokoro model files and venv are untracked in the main tree (R-WP13-2); the real shape of `Assets/StreamingAssets/Voice/index.json` and the resume behaviour of `tools/voice/render_fixed.py` are unverified (R-WP13-3); art-vs-voice order: Nirandyr rule 49 is contradictory, so voice is lowest priority (`queue_bias: 500`) until Micah confirms (R-WP13-4).

## Not built / by design
- Live reviewer (cheap model, diff-only): API-billed tasks stop at "waiting for your review" (R-WP10-3).
- Correction runs and manual runs do not re-pass reservation admission (R-WP10-2, R-WP7-1, R-WP12-3).
- Scheduler backoff and last tick report are in memory (R-WP7-2).
- Backups unencrypted next to the DB (R-WP9-1); tasks in TESTING/REVIEWING/CORRECTING are not reconciled after a crash beyond the generic rules (R-WP9-2); open-step detection is a heuristic (R-WP9-3).
- Merge check uses the base as last fetched (R-WP10-1); a claim of a cancelled task stays until released (R-WP11-3).
- `required_tools` checked for existence only, no hash (R-WP12-1); devices not wrapped with `devlock` by the framework (R-WP12-2); FBX header-checked only (R-WP14-3).
- Single user, single PC, no multi-user auth, no remote access, no HTTPS (loopback).
- `dungeon_gen.sh` has no lane yet; Nirandyr lanes are assumptions until the first PC run.
