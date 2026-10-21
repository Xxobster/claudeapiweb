import pytest
from fastapi.testclient import TestClient

from cpm.api.app import create_app
from cpm.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(host="127.0.0.1", port=8765, data_dir=tmp_path / "data", log_level="INFO")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c
