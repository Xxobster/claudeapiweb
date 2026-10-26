"""GitHub pull-request creation (WP10; docs/THREATS.md T-31).

The only network call CPM makes about a task result: `POST /repos/{owner}/{repo}/pulls` on the
fixed host api.github.com. The token comes from the secret store, travels only in the
Authorization header and is never logged, stored or returned. Owner and repo are validated
against the registered remote, never taken from request input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from cpm.security.redaction import redact

API = "https://api.github.com"
GITHUB_TOKEN = "github_token"  # noqa: S105 (secret-store entry name, not a secret)
_OWNER = r"([A-Za-z0-9._-]{1,100})"
_REPO = r"([A-Za-z0-9._-]{1,100})"
_HTTPS = re.compile(rf"^https://github\.com/{_OWNER}/{_REPO}/?$")
_SCP = re.compile(rf"^git@github\.com:{_OWNER}/{_REPO}$")
_TOKEN_RE = re.compile(r"^(github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})$")
MAX_TITLE = 250
MAX_BODY = 60_000


class GitHubError(RuntimeError):
    """The pull request could not be created. The message is redacted and short."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str


def parse_remote(url: str | None) -> tuple[str, str] | None:
    """(owner, repo) of a github.com remote; None for any other host or shape."""
    if not url:
        return None
    m = _HTTPS.match(url) or _SCP.match(url)
    if not m or {m.group(1), m.group(2)} & {".", ".."}:
        return None
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    return (owner, repo) if repo else None


def is_token_format(value: str) -> bool:
    return bool(_TOKEN_RE.match(value))


class GitHubClient(Protocol):
    def create_pull_request(
        self, owner: str, repo: str, *, head: str, base: str, title: str, body: str, token: str
    ) -> PullRequest: ...


class HttpGitHubClient:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def create_pull_request(
        self, owner: str, repo: str, *, head: str, base: str, title: str, body: str, token: str
    ) -> PullRequest:
        client = self._client or httpx.Client(timeout=20.0, follow_redirects=False)
        try:
            resp = client.post(
                f"{API}/repos/{owner}/{repo}/pulls",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "ClaudeProjectManager",
                },
                json={
                    "title": title[:MAX_TITLE],
                    "head": head,
                    "base": base,
                    "body": body[:MAX_BODY],
                    "maintainer_can_modify": False,
                    "draft": False,
                },
            )
        except httpx.HTTPError as exc:
            raise GitHubError(f"GitHub is unreachable ({type(exc).__name__})") from exc
        if resp.status_code != 201:
            raise GitHubError(_explain(resp))
        data: Any = resp.json()
        number, url = data.get("number"), data.get("html_url")
        if not isinstance(number, int) or not isinstance(url, str):
            raise GitHubError("GitHub answered without a pull request number")
        if not url.startswith(f"https://github.com/{owner}/{repo}/pull/"):
            raise GitHubError("GitHub answered with an unexpected pull request address")
        return PullRequest(number, url)


def _explain(resp: httpx.Response) -> str:
    hint = {
        401: "the token was refused (expired or wrong)",
        403: "the token may not open pull requests on this repository",
        404: "repository not found, or the token cannot see it",
        422: "GitHub refused the pull request (it may already exist, or the branch has no diff)",
    }.get(resp.status_code, "unexpected answer")
    detail = ""
    try:
        msg = resp.json().get("message")
        if isinstance(msg, str):
            detail = f": {redact(msg)[:200]}"
    except ValueError:
        pass
    return f"GitHub answered {resp.status_code} - {hint}{detail}"
