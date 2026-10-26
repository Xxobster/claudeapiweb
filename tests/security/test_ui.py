"""WP15: static UI shell and one-time WebSocket tickets (docs/THREATS.md T-27, T-39)."""

import re

import pytest
from starlette.websockets import WebSocketDisconnect

from cpm.api.ui import WEB_DIR
from cpm.security.tickets import TicketStore

ORIGIN = {"Origin": "http://127.0.0.1:8765"}


def test_shell_is_public_but_host_checked_and_has_csp(client):
    r = client.get("/")
    assert r.status_code == 200 and "Claude Project Manager" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"Origin": "http://evil.example"}).status_code == 403


def test_only_static_paths_are_public(client):
    for path in ("/api/dashboard", "/api/projects", "/api/session", "/api/wizard/status"):
        assert client.get(path).status_code == 401
    assert client.post("/api/ws-ticket", json={"run_id": 1}).status_code == 401


def test_static_traversal_refused(client):
    for path in ("/ui/../pyproject.toml", "/ui/%2e%2e/api/app.py", "/ui/..%2f..%2fconfig.py"):
        r = client.get(path)
        assert r.status_code in (400, 404) or "[project]" not in r.text


def test_ticket_needs_token_and_csrf(client, auth):
    assert (
        client.post(
            "/api/ws-ticket", json={"run_id": 1}, headers={"X-CPM-Token": auth["X-CPM-Token"]}
        ).status_code
        == 403
    )
    r = client.post("/api/ws-ticket", json={"run_id": 7}, headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "/api/runs/7/stream" and len(body["ticket"]) >= 24
    assert client.post("/api/ws-ticket", json={"run_id": 0}, headers=auth).status_code == 422


def test_websocket_with_ticket_is_one_time_and_path_bound(client, auth):
    def ticket(rid):
        return client.post("/api/ws-ticket", json={"run_id": rid}, headers=auth).json()["ticket"]

    # Valid ticket passes the guard (run 999 does not exist, so the app then closes the socket).
    t = ticket(999)
    with pytest.raises(WebSocketDisconnect) as ok:
        with client.websocket_connect(f"/api/runs/999/stream?ticket={t}", headers=ORIGIN):
            pass
    assert ok.value.code == 1008
    # Replay of the same ticket fails at the guard, as does a ticket for another run.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/runs/999/stream?ticket={t}", headers=ORIGIN):
            pass
    t2 = ticket(5)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/runs/6/stream?ticket={t2}", headers=ORIGIN):
            pass
    # ...and the wrong-path attempt burned it.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/runs/5/stream?ticket={t2}", headers=ORIGIN):
            pass


def test_ticket_does_not_replace_origin_and_token_in_query_is_ignored(client, auth):
    t = client.post("/api/ws-ticket", json={"run_id": 3}, headers=auth).json()["ticket"]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/runs/3/stream?ticket={t}"):  # no Origin
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/api/runs/3/stream?ticket={t}", headers={"Origin": "http://evil.example"}
        ):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/api/runs/3/stream?token={auth['X-CPM-Token']}", headers=ORIGIN
        ):
            pass


def test_ticket_store_expiry_and_cap():
    now = [0.0]
    store = TicketStore(ttl_s=10, clock=lambda: now[0])
    t = store.issue("/a")
    assert store.consume(t, "/a") and not store.consume(t, "/a")
    t = store.issue("/a")
    now[0] = 11
    assert not store.consume(t, "/a")
    assert not store.consume(None, "/a") and not store.consume("nope", "/a")
    issued = [store.issue("/a") for _ in range(64)]
    assert all(issued) and store.issue("/a") is None
    now[0] = 30
    assert store.issue("/a") is not None  # expired ones are purged


def test_ui_code_has_no_dangerous_sinks_or_inline_script():
    banned = re.compile(
        r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function"
        r"|sessionStorage|document\.cookie"
    )
    files = [p for p in WEB_DIR.iterdir() if p.suffix in {".js", ".html", ".css"}]
    assert {p.name for p in files} >= {"index.html", "app.js", "api.js", "app.css"}
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert not banned.search(text), f.name
        assert not re.search(r"https?://", text), f"{f.name}: remote URL"  # no remote asset
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\son\w+\s*=", html) and "style=" not in html
