"""Findings of the WP1/WP2 security review (docs/THREATS.md T-30..T-33)."""

import pytest
from test_wizard import body

from cpm.security.paths import PathPolicyError, resolve_within, validate_root
from cpm.security.tokens import TOKEN_FILE, load_or_create_token


def test_response_hardening_headers(client, auth):
    for r in (
        client.get("/health"),
        client.get("/api/session", headers=auth),
        client.get("/api/session"),
    ):
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_no_api_docs(client, auth, path):
    assert client.get(path, headers=auth).status_code == 404


@pytest.mark.parametrize("name", ["COM¹", "lpt².txt", "CONIN$", "conout$"])
def test_extra_reserved_names(tmp_path, name):
    with pytest.raises(PathPolicyError):
        resolve_within(tmp_path, name)


def test_filesystem_root_is_not_an_approved_root(tmp_path):
    import os

    with pytest.raises(PathPolicyError):
        validate_root(os.path.abspath(os.sep))


@pytest.mark.parametrize(
    "url",
    [
        "https://user:ghp_secret1234@github.com/x/y.git",
        "https://token@github.com/x/y.git",
        "ssh://git:pw@host/x.git",
    ],
)
def test_git_url_with_credentials_rejected(client, auth, tmp_path, url):
    r = client.put("/api/wizard/config", json=body(tmp_path, git_url=url), headers=auth)
    assert r.status_code == 422
    assert "ghp_secret1234" not in r.text


def test_git_url_plain_ssh_user_ok(client, auth, tmp_path):
    r = client.put(
        "/api/wizard/config",
        json=body(tmp_path, git_url="ssh://git@github.com/x/y.git"),
        headers=auth,
    )
    assert r.status_code == 200, r.text


def test_corrupt_token_file_is_replaced(tmp_path):
    (tmp_path / TOKEN_FILE).write_bytes(b"\xff\xfe\x00bad")
    assert len(load_or_create_token(tmp_path)) >= 32
