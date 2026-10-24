"""Token, Host, Origin, CSRF and DNS-rebinding checks (THREATS T-02)."""

import pytest
from starlette.websockets import WebSocketDisconnect

KEY = "sk-ant-api03-" + "A1b2C3d4" * 4


def test_health_public_but_host_checked(client):
    assert client.get("/health").status_code == 200
    r = client.get("/health", headers={"Host": "evil.example:8765"})
    assert r.status_code == 403 and r.json() == {"error": "bad_host"}


@pytest.mark.parametrize(
    "host", ["evil.com", "127.0.0.1", "127.0.0.1:9999", "localhost.evil.com:8765", ""]
)
def test_dns_rebinding_hosts_rejected(client, auth, host):
    r = client.get("/api/credentials/anthropic", headers={**auth, "Host": host})
    assert r.status_code == 403


def test_token_required(client):
    assert client.get("/api/credentials/anthropic").status_code == 401
    assert (
        client.get("/api/credentials/anthropic", headers={"X-CPM-Token": "wrong"}).status_code
        == 401
    )
    assert client.get("/openapi.json").status_code == 401  # not public


def test_unknown_path_still_needs_token(client):
    assert client.get("/nope").status_code == 401


def test_csrf_required_on_mutations(client, auth):
    only_token = {"X-CPM-Token": auth["X-CPM-Token"]}
    r = client.put("/api/credentials/anthropic", json={"api_key": KEY}, headers=only_token)
    assert r.status_code == 403 and r.json() == {"error": "csrf_required"}
    r = client.put(
        "/api/credentials/anthropic",
        json={"api_key": KEY},
        headers={**only_token, "X-CPM-CSRF": "x"},
    )
    assert r.status_code == 403


def test_csrf_endpoint(client, auth):
    r = client.get("/api/session", headers={"X-CPM-Token": auth["X-CPM-Token"]})
    assert r.json()["csrf"] == auth["X-CPM-CSRF"]


@pytest.mark.parametrize(
    "origin", ["http://evil.com", "null", "http://127.0.0.1:8766", "https://127.0.0.1:8765"]
)
def test_foreign_origin_rejected_even_with_token(client, auth, origin):
    r = client.get("/api/credentials/anthropic", headers={**auth, "Origin": origin})
    assert r.status_code == 403 and r.json() == {"error": "bad_origin"}
    r = client.delete("/api/credentials/anthropic", headers={**auth, "Origin": origin})
    assert r.status_code == 403


def test_same_origin_allowed(client, auth):
    r = client.get(
        "/api/credentials/anthropic", headers={**auth, "Origin": "http://localhost:8765"}
    )
    assert r.status_code == 200


def test_no_cors_headers(client, auth):
    r = client.options(
        "/api/credentials/anthropic",
        headers={"Origin": "http://evil.com", "Access-Control-Request-Method": "PUT"},
    )
    assert r.status_code == 403
    r = client.get("/api/credentials/anthropic", headers=auth)
    assert not any(h.lower().startswith("access-control") for h in r.headers)


def test_websocket_needs_origin_and_token(client, auth):
    for headers in ({}, {"Origin": "http://evil.com", **auth}, {"Origin": "http://127.0.0.1:8765"}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/anything", headers=headers):
                pass


def test_token_file_private_and_stable(settings, client):
    from cpm.security.tokens import load_or_create_token

    path = settings.data_dir / "install_token"
    assert path.exists()
    assert load_or_create_token(settings.data_dir) == path.read_text().strip()
    assert len(path.read_text().strip()) >= 40
    if path.stat().st_mode & 0o777:  # POSIX only
        assert path.stat().st_mode & 0o077 == 0
