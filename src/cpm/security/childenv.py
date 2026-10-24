"""Explicit child-process environment (docs/THREATS.md T-12, SDK_NOTES section 2).

Workers never inherit the parent environment. API-billed mode gets exactly one credential
variable; every other Anthropic/cloud credential variable is dropped so billing cannot switch
silently.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

BILLING_API = "api"
_PASSTHROUGH = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SystemDrive", "COMSPEC", "WINDIR", "TEMP", "TMP",
    "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "LANG", "LC_ALL", "TERM",
)  # fmt: skip
CREDENTIAL_VARS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "GH_TOKEN", "GITHUB_TOKEN",
)  # fmt: skip


def build_child_env(
    billing_mode: str,
    api_key: str | None = None,
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if billing_mode != BILLING_API:
        raise ValueError("only API-billed mode exists in v1 (DECISIONS Q9)")
    if not api_key:
        raise ValueError("API-billed mode needs an API key")
    src = os.environ if parent is None else parent
    wanted = {n.upper() for n in _PASSTHROUGH}
    env = {k: v for k, v in src.items() if k.upper() in wanted}
    env["ANTHROPIC_API_KEY"] = api_key
    return env


def inherited_credential_vars(parent: Mapping[str, str] | None = None) -> list[str]:
    """Names (never values) of credential variables present in the CPM process environment."""
    src = os.environ if parent is None else parent
    return [n for n in CREDENTIAL_VARS if src.get(n)]
