"""WP6 security checks: guard coverage, no repo code execution, no raw lock content echoed."""

import ast
import pathlib

import pytest

ROUTES = [
    ("get", "/api/projects/1/reservations"),
    ("get", "/api/tasks/1/conflicts"),
    ("post", "/api/tasks/1/reservations/release"),
    ("get", "/api/projects/1/desktop-claims"),
    ("post", "/api/projects/1/desktop-claims"),
    ("delete", "/api/desktop-claims/1"),
    ("get", "/api/projects/1/locks"),
    ("post", "/api/projects/1/external-scan"),
]


@pytest.mark.parametrize("method,url", ROUTES)
def test_lock_routes_need_token_and_csrf(client, auth, method, url):
    send = getattr(client, method)
    kw = {"json": {"path": "a/"}} if method == "post" and url.endswith("claims") else {}
    assert send(url, **kw).status_code == 401
    if method != "get":
        only_token = {"X-CPM-Token": auth["X-CPM-Token"]}
        assert send(url, headers=only_token, **kw).status_code == 403


def test_cross_origin_claim_is_refused(client, auth):
    h = {**auth, "Origin": "http://evil.example"}
    r = client.post("/api/projects/1/desktop-claims", json={"path": "a/"}, headers=h)
    assert r.status_code == 403


def test_locks_package_never_runs_a_subprocess_or_shell():
    """Adapters only read; git goes through the hardened `gitinfo._git`."""
    for path in pathlib.Path("src/cpm/locks").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            assert not {"subprocess", "os.system"} & set(names), path
            if isinstance(node, ast.Attribute) and node.attr in ("system", "popen", "Popen"):
                raise AssertionError(f"{path}: {node.attr}")
