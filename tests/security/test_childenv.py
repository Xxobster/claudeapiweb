import pytest

from cpm.security.childenv import build_child_env, inherited_credential_vars

PARENT = {
    "PATH": "/bin",
    "HOME": "/h",
    "ANTHROPIC_API_KEY": "old",
    "ANTHROPIC_AUTH_TOKEN": "t",
    "CLAUDE_CODE_OAUTH_TOKEN": "o",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "GITHUB_TOKEN": "g",
    "AWS_SECRET_ACCESS_KEY": "aws",
    "SECRET": "s",
}


def test_child_env_is_allowlist_with_single_credential():
    env = build_child_env("api", "sk-ant-new", PARENT)
    assert env == {
        "PATH": "/bin",
        "HOME": "/h",
        "ANTHROPIC_API_KEY": "sk-ant-new",
        "NoDefaultCurrentDirectoryInExePath": "1",
    }


def test_no_key_no_env():
    with pytest.raises(ValueError):
        build_child_env("api", None, PARENT)


def test_subscription_mode_not_available():
    with pytest.raises(ValueError):
        build_child_env("subscription", "k", PARENT)


def test_inherited_vars_names_only():
    assert inherited_credential_vars(PARENT) == [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "GITHUB_TOKEN",
    ]
    assert inherited_credential_vars({}) == []
