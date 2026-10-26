# Claude Project Manager (CPM)

Local-first Windows 11 web app (Python 3.12, FastAPI, SQLite) that runs several Claude workers in parallel with your main Claude Desktop work, each on its own git worktree, under budgets, file reservations and an acceptance gate. It listens on `127.0.0.1:8765` only. Nothing is spent and no network call is made until you allow live runs.

## Install and start (Windows 11, PowerShell 5.1)
```powershell
git clone https://github.com/Xxobster/claudeapiweb D:\projects\ClaudeProjectManager   # never inside OneDrive
cd D:\projects\ClaudeProjectManager
powershell -File scripts\install.ps1      # venv, hash-locked deps, database, Desktop + Start Menu shortcut
powershell -File scripts\start.ps1        # or use the "Claude Project Manager" shortcut
```
The browser opens with the install token in the URL fragment. First run: the setup wizard (Settings). Remove with `scripts\uninstall.ps1` (keeps `data\` and worktrees).

## Documents (read in this order)
| File | What it is |
|---|---|
| `CLAUDE.md` | Standing rules for every session working on CPM. |
| `PLAN.md` | Work packages WP0 to WP16, in order. |
| `docs/USER_GUIDE.md` | How to use each screen and feature. |
| `docs/SECURITY.md` | Security model, what is enforced, how to report. |
| `docs/ARCHITECTURE.md` | Modules and data flow. |
| `docs/API.md` | Every HTTP route (generated, checked by a test). |
| `docs/THREATS.md` | Threat list and residual risks (T-nn, R-nn). |
| `docs/IMPACT_MAP.md` | What each module touches; change log. |
| `docs/DECISIONS.md` | Micah's answers and implementation choices. |
| `docs/KNOWN_LIMITATIONS.md` | What does not exist yet and what needs the PC. |
| `docs/TEST_REPORT.md` | What was tested and how, scenario by scenario. |
| `docs/SDK_NOTES.md` | Verified Agent SDK / pricing facts with dates. |

## Develop
```bash
pip install --require-hashes -r requirements.lock && pip install --no-deps -e .
ruff check . && mypy --strict src && pytest -m "not windows"
node tests/ui/e2e.mjs      # Playwright UI tests (needs Chromium)
python scripts/gen_api_doc.py   # after changing a route
```
