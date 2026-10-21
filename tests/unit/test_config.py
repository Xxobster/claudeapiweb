import pytest

from cpm.config import ConfigError, load_settings


def test_defaults(tmp_path):
    s = load_settings({"CPM_DATA_DIR": str(tmp_path)})
    assert s.host == "127.0.0.1" and s.port == 8765


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",  # noqa: S104
        "192.168.1.5",
        "example.com",
        "::",
    ],
)
def test_rejects_non_loopback(host, tmp_path):
    with pytest.raises(ConfigError):
        load_settings({"CPM_HOST": host, "CPM_DATA_DIR": str(tmp_path)})


def test_rejects_onedrive_data_dir(tmp_path):
    with pytest.raises(ConfigError):
        load_settings({"CPM_DATA_DIR": str(tmp_path / "OneDrive" / "x")})


def test_bad_port(tmp_path):
    with pytest.raises(ConfigError):
        load_settings({"CPM_PORT": "99999", "CPM_DATA_DIR": str(tmp_path)})
