import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail

from cpm.security.secrets_store import KeyringSecretStore, SecretStoreUnavailable


class Dict(KeyringBackend):
    priority = 5

    def __init__(self):
        self.d = {}

    def get_password(self, s, u):
        return self.d.get((s, u))

    def set_password(self, s, u, p):
        self.d[(s, u)] = p

    def delete_password(self, s, u):
        if (s, u) not in self.d:
            raise keyring.errors.PasswordDeleteError("missing")
        del self.d[(s, u)]


@pytest.fixture
def restore():
    old = keyring.get_keyring()
    yield
    keyring.set_keyring(old)


def test_roundtrip(restore):
    keyring.set_keyring(Dict())
    s = KeyringSecretStore("cpm-test")
    assert s.get("k") is None
    s.set("k", "v" * 30)
    assert s.get("k") == "v" * 30
    s.delete("k")
    s.delete("k")  # idempotent
    assert s.get("k") is None


def test_fails_closed_without_backend(restore):
    keyring.set_keyring(fail.Keyring())
    with pytest.raises(SecretStoreUnavailable):
        KeyringSecretStore().set("k", "v" * 30)


def test_api_503_when_storage_unavailable(settings, restore):
    from fastapi.testclient import TestClient

    from cpm.api.app import create_app
    from cpm.security.tokens import csrf_for, load_or_create_token

    keyring.set_keyring(fail.Keyring())
    t = load_or_create_token(settings.data_dir)
    h = {"X-CPM-Token": t, "X-CPM-CSRF": csrf_for(t)}
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8765") as c:
        assert c.get("/api/credentials/anthropic", headers=h).json()["storage_available"] is False
        r = c.put(
            "/api/credentials/anthropic", json={"api_key": "sk-ant-api03-" + "a" * 30}, headers=h
        )
        assert r.status_code == 503
