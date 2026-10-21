import logging

from cpm.logging_setup import configure_logging
from cpm.security.redaction import redact, register_secret

FAKE_KEY = "sk-ant-api03-FAKEFAKEFAKEFAKE1234567890"


def test_fake_key_never_in_log_file(tmp_path):
    configure_logging("DEBUG", tmp_path)
    log = logging.getLogger("cpm.test")
    log.info("key is %s", FAKE_KEY)
    log.info("header Authorization: Bearer abcdefghijklmnop1234")
    log.info(f"api_key={FAKE_KEY}")
    try:
        raise RuntimeError(f"boom {FAKE_KEY}")
    except RuntimeError:
        log.exception("failed")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "cpm.log").read_text(encoding="utf-8")
    assert "FAKEFAKE" not in text
    assert "abcdefghijklmnop1234" not in text
    assert "[REDACTED]" in text


def test_registered_secret_redacted():
    register_secret("install-token-xyz-987")
    assert "install-token-xyz-987" not in redact("token install-token-xyz-987 here")


def test_health_does_not_leak(client):
    assert "sk-ant" not in client.get("/health").text
