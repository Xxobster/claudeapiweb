"""Static web UI (WP15): the single-page app shell, plus the one-time WebSocket ticket endpoint.

The shell is plain HTML/CSS/JS with no build step and no third-party code. It holds no data and no
token; the page gets the install token from the URL fragment printed by `scripts/start.ps1` (a
fragment is never sent to the server), keeps it in sessionStorage and sends it as `X-CPM-Token`.
The Content-Security-Policy below forbids inline script, remote scripts, framing and any
connection except to this origin (docs/THREATS.md T-39).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

router = APIRouter()

# Only streams the UI needs; a ticket for any other path is refused.
_STREAM_PREFIX = "/api/runs/"


class TicketIn(BaseModel):
    run_id: int = Field(ge=1)


@router.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(
        WEB_DIR / "index.html", media_type="text/html", headers={"Content-Security-Policy": CSP}
    )


@router.post("/api/ws-ticket")
def ws_ticket(body: TicketIn, request: Request) -> dict[str, object]:
    """A one-time, 30-second ticket for `/api/runs/{run_id}/stream` (see security/tickets.py)."""
    path = f"{_STREAM_PREFIX}{body.run_id}/stream"
    ticket = request.app.state.tickets.issue(path)
    if ticket is None:
        raise HTTPException(429, "too many outstanding stream tickets")
    return {"ticket": ticket, "path": path, "expires_in_s": 30}


def mount_static(app: object) -> None:
    app.mount("/ui", StaticFiles(directory=WEB_DIR), name="ui")  # type: ignore[attr-defined]
