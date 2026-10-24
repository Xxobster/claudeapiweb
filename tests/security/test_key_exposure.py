"""API key never reaches responses, logs, DB or files (THREATS T-01)."""

import logging
import sqlite3

import pytest

from cpm.security.redaction import redact
from cpm.security.secrets_store import ANTHROPIC_KEY

KEY = "sk-ant-api03-" + "Zq9Xw8Vu" * 4


def put(client, auth, key=KEY, **kw):
    return client.put("/api/credentials/anthropic", json={"api_key": key, **kw}, headers=auth)


def test_save_status_never_returns_key(client, auth, store):
    r = put(client, auth)
    assert r.status_code == 200 and r.json()["configured"] is True
    assert store.get(ANTHROPIC_KEY) == KEY
    for path in ("/api/credentials/anthropic", "/api/wizard/status"):
        assert KEY not in client.get(path, headers=auth).text
    assert KEY not in r.text
    assert KEY[:15] not in r.text


def test_validation_error_does_not_echo_input(client, auth):
    r = client.put(
        "/api/credentials/anthropic", json={"api_key": 12345, "extra": KEY}, headers=auth
    )
    assert r.status_code == 422 and "12345" not in r.text
    r = client.put(
        "/api/credentials/anthropic", json={"api_key": KEY, "live": "maybe-" + KEY}, headers=auth
    )
    assert r.status_code == 422 and KEY not in r.text


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "hello",
        "sk-ant-short",
        "sk-ant-admin01-" + "a" * 30,
        "sk-ant-api03-" + "a" * 20 + " b",
        "sk-ant-api03-invalid" + "a" * 20,
    ],
)
def test_bad_keys_rejected_and_not_stored(client, auth, store, bad):
    r = put(client, auth, bad)
    assert r.status_code == 422
    assert store.get(ANTHROPIC_KEY) is None
    assert bad not in r.text or bad == ""


def test_not_in_logs_db_or_data_dir(client, auth, settings, caplog):
    caplog.set_level(logging.DEBUG)
    put(client, auth)
    client.post("/api/credentials/anthropic/test", json={}, headers=auth)
    assert KEY not in caplog.text
    for f in settings.data_dir.rglob("*"):
        if f.is_file():
            assert KEY.encode() not in f.read_bytes(), f
    con = sqlite3.connect(settings.db_path)
    dump = "\n".join(con.iterdump())
    assert KEY not in dump


def test_test_replace_remove(client, auth, store):
    assert client.post("/api/credentials/anthropic/test", json={}, headers=auth).status_code == 404
    put(client, auth)
    r = client.post("/api/credentials/anthropic/test", json={}, headers=auth)
    assert r.json()["ok"] is True and "no network" in r.json()["message"]
    new = "sk-ant-api03-" + "Mm7Nn6Oo" * 4
    put(client, auth, new)
    assert store.get(ANTHROPIC_KEY) == new
    r = client.delete("/api/credentials/anthropic", headers=auth)
    assert r.json()["configured"] is False and store.get(ANTHROPIC_KEY) is None


def test_live_test_disabled_by_default(client, auth):
    assert put(client, auth, live=True).status_code == 403
    put(client, auth)
    r = client.post("/api/credentials/anthropic/test", json={"live": True}, headers=auth)
    assert r.status_code == 403


def test_redaction_catches_key_forms():
    for s in (
        f"key={KEY}",
        f"Authorization: Bearer {KEY}",
        f'{{"api_key": "{KEY}"}}',
        f"x-api-key: {KEY}",
    ):
        assert KEY not in redact(s)


def test_install_token_redacted_in_logs(client, auth, capsys):
    import logging as lg

    from cpm.logging_setup import configure_logging

    configure_logging("INFO")
    lg.getLogger("cpm").info("token is %s", auth["X-CPM-Token"])
    err = capsys.readouterr().err
    assert "token is" in err and auth["X-CPM-Token"] not in err
