"""Recovery and backups (WP9): the last restart report, a manual reconcile and DB backups.

Restoring a backup is deliberately not an API: it replaces the database CPM is using, so it is a
command run while CPM is stopped (`python -m cpm.recovery restore FILE`). Behind the usual
token/Host/Origin/CSRF guard; backup file names are CPM-generated and never taken from input.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from cpm.db.settings_repo import get_setting
from cpm.recovery.backup import create_backup, list_backups
from cpm.recovery.reconcile import REPORT_KEY, Recovery

router = APIRouter(prefix="/api/recovery", tags=["recovery"])


@router.get("")
def status(request: Request) -> dict[str, Any]:
    s = request.app.state.settings
    return {
        "last_report": get_setting(request.app.state.engine, REPORT_KEY),
        "backups": [p.name for p in list_backups(s.data_dir)],
    }


@router.post("/reconcile")
def reconcile(request: Request) -> dict[str, Any]:
    rec: Recovery = request.app.state.recovery
    return rec.reconcile().as_dict()


@router.post("/backup")
def backup(request: Request) -> dict[str, str]:
    s = request.app.state.settings
    return {"file": create_backup(s.db_path, s.data_dir).name}
