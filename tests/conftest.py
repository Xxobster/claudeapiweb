import pytest
from fastapi.testclient import TestClient

from cpm.api.app import create_app
from cpm.config import Settings
from cpm.security.secrets_store import MemorySecretStore
from cpm.security.tokens import csrf_for, load_or_create_token


@pytest.fixture
def settings(tmp_path):
    return Settings(host="127.0.0.1", port=8765, data_dir=tmp_path / "data", log_level="INFO")


@pytest.fixture
def store():
    return MemorySecretStore()


@pytest.fixture
def client(settings, store):
    """Unauthenticated client with a valid Host header (what a browser tab would send)."""
    with TestClient(
        create_app(settings, secret_store=store), base_url="http://127.0.0.1:8765"
    ) as c:
        yield c


@pytest.fixture
def auth(settings, client):
    token = load_or_create_token(settings.data_dir)
    return {"X-CPM-Token": token, "X-CPM-CSRF": csrf_for(token)}
