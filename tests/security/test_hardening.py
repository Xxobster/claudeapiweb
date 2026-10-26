"""WP16 hardening sweep: static checks that guard the whole code base against regressions.

These are cheap source-level tripwires on top of the behavioural tests in this folder. They do not
replace code review; they make the most dangerous patterns fail CI the moment they appear.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "cpm"
WEB = SRC / "web"


SET_ITEM = r"(localStorage|sessionStorage)\.setItem\([^)]*\)"


def _py_files():
    return sorted(p for p in SRC.rglob("*.py"))


def _calls(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            yield node


def _name(node: ast.Call) -> str:
    f = node.func
    parts = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


def test_no_shell_true_and_no_dynamic_code_execution():
    bad = []
    for path in _py_files():
        for call in _calls(path):
            name = _name(call)
            if name in {"eval", "exec", "os.system", "os.popen", "pickle.loads", "pickle.load"}:
                bad.append(f"{path.relative_to(ROOT)}:{call.lineno} {name}")
            for kw in call.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value:
                    bad.append(f"{path.relative_to(ROOT)}:{call.lineno} shell=True")
    assert not bad, bad


def test_subprocess_calls_use_argument_lists_not_strings():
    bad = []
    for path in _py_files():
        for call in _calls(path):
            if _name(call) in {"subprocess.run", "subprocess.Popen", "subprocess.check_output"}:
                first = call.args[0] if call.args else None
                if isinstance(first, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                    bad.append(f"{path.relative_to(ROOT)}:{call.lineno}")
    assert not bad, bad


def test_yaml_is_always_loaded_safely():
    for path in _py_files():
        for call in _calls(path):
            assert _name(call) not in {"yaml.load", "yaml.unsafe_load"}, path


def test_money_is_never_a_float_in_the_budget_code():
    for path in (SRC / "budget").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\bfloat\(", text), f"{path.name}: float() in money code"
        assert not re.search(r":\s*float\b", text), f"{path.name}: float annotation in money code"


def test_frontend_has_no_key_material():
    key_like = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")
    for path in WEB.iterdir():
        text = path.read_text(encoding="utf-8")
        assert not key_like.search(text), path.name
        assert "innerHTML" not in text and "insertAdjacentHTML" not in text, path.name
        assert "eval(" not in text and "new Function" not in text, path.name
    storage = [
        m.group(0)
        for p in WEB.glob("*.js")
        for m in re.finditer(
            r"(localStorage|sessionStorage)\.setItem\([^)]*\)", p.read_text("utf-8")
        )
    ]
    assert all("KEY" in s for s in storage), storage  # only the install token is ever stored


def test_no_remote_resources_in_the_page():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"(src|href)=[\"']https?://", html)


def test_secrets_never_enter_the_repo():
    tracked_text = [ROOT / "profiles", ROOT / "scripts", ROOT / "docs"]
    key_like = re.compile(r"sk-ant-(api|admin)\d*-[A-Za-z0-9_-]{20,}")
    for base in tracked_text:
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".yaml", ".yml", ".ps1", ".json"}:
                assert not key_like.search(path.read_text(encoding="utf-8", errors="ignore")), path


def test_scripts_do_not_download_or_evaluate_code():
    forbidden = re.compile(
        r"Invoke-Expression|\biex\b|DownloadString|Invoke-WebRequest|-ExecutionPolicy\s+Bypass",
        re.I,
    )
    for path in (ROOT / "scripts").glob("*.ps1"):
        assert not forbidden.search(path.read_text(encoding="utf-8")), path.name


def test_installer_scripts_exist_and_uninstall_keeps_user_data():
    for name in ("install.ps1", "make_shortcut.ps1", "uninstall.ps1", "setup.ps1", "start.ps1"):
        assert (ROOT / "scripts" / name).is_file(), name
    uninstall = (ROOT / "scripts" / "uninstall.ps1").read_text(encoding="utf-8")
    removes = re.findall(r"Remove-Item[^\n]*", uninstall)
    assert removes and all("data" not in r.lower() and "worktree" not in r.lower() for r in removes)
    assert "OneDrive" in (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")


PUBLIC = {"/health", "/", "/static", "/app.js", "/api.js", "/app.css"}


def _routes(client):
    """(path, methods) of every route, read from the generated schema (works for nested routers)."""
    for path, ops in client.app.openapi()["paths"].items():
        methods = sorted(
            m.upper() for m in ops if m.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        )
        yield path, methods


def test_every_api_route_rejects_a_caller_without_the_token(client):
    checked = 0
    for path, methods in _routes(client):
        if not path.startswith("/api"):
            continue
        url = re.sub(r"\{[^}]+\}", "1", path)
        for method in methods:
            resp = client.request(method, url)
            assert resp.status_code in {401, 403}, (method, path, resp.status_code)
            checked += 1
    assert checked > 50  # the sweep really walked the API


def test_every_mutating_route_rejects_a_foreign_origin_even_with_the_token(client, auth):
    for path, methods in _routes(client):
        if not path.startswith("/api"):
            continue
        url = re.sub(r"\{[^}]+\}", "1", path)
        for method in methods:
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                resp = client.request(
                    method, url, headers={**auth, "Origin": "http://evil.example"}
                )
                assert resp.status_code == 403, (method, path, resp.status_code)


def test_non_api_routes_expose_no_data(client):
    for path, _ in _routes(client):
        if not path.startswith("/api") and path not in PUBLIC:
            body = client.get(re.sub(r"\{[^}]+\}", "x", path))
            assert body.status_code in {401, 403, 404, 405}, path
