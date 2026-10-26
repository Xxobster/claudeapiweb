import os

import pytest

from cpm.locks import normalize as n
from cpm.security.paths import PathPolicyError


def key(root, raw):
    return n.normalize_scope(root, raw)[0]


@pytest.mark.parametrize(
    "a,b",
    [
        ("Assets/Game/UI/", "assets\\game\\ui\\"),
        ("Assets/Game/UI/", "./Assets//Game/./UI/"),
        ("Assets/Game/UI/**", "assets/game/ui/"),
        ("Assets/Game/UI/*", "assets/game/ui/"),
        ("Assets/Game/Hero.prefab", "ASSETS\\GAME\\HERO.PREFAB"),
        ("Assets/Game/Hero.prefab", "Assets/Game/Hero.prefab.meta"),
        ("caf\u00e9/a.txt", "cafe\u0301/a.txt"),
    ],
)
def test_spellings_give_one_key(tmp_path, a, b):
    assert key(tmp_path, a) == key(tmp_path, b)


def test_dir_and_file_forms_differ_but_overlap(tmp_path):
    d, f = key(tmp_path, "Assets/Foo/"), key(tmp_path, "Assets/Foo.meta")
    assert d.endswith("/") and not f.endswith("/")
    assert n.overlaps(d, f)  # a folder's .meta is the folder itself


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("src/", "src/a.py", True),
        ("src/a.py", "src/", True),
        ("src/", "srcx/a.py", False),
        ("src/a.py", "src/b.py", False),
        ("src/a.py", "src/a.py", True),
        ("src/", "src/deep/er/x", True),
        ("/", "anything/at/all", True),
        ("src/a/", "src/b/", False),
        ("src/a", "src/a/", True),
    ],
)
def test_prefix_semantics(a, b, expected):
    assert n.overlaps(a, b) is expected
    assert n.overlaps(b, a) is expected


@pytest.mark.parametrize(
    "raw",
    ["../x", "a/../../x", "/etc/passwd", "C:\\x", "\\\\srv\\share", "CON", "a:b", "PROGRA~1/x", ""],
)
def test_unsafe_scopes_rejected(tmp_path, raw):
    with pytest.raises(PathPolicyError):
        n.normalize_scope(tmp_path, raw)


@pytest.mark.skipif(os.name == "nt", reason="needs symlink privilege")
def test_symlink_alias_inside_project_resolves_to_target(tmp_path):
    (tmp_path / "real" / "sub").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    assert key(tmp_path, "alias/sub/f.txt") == key(tmp_path, "real/sub/f.txt")


@pytest.mark.skipif(os.name == "nt", reason="needs symlink privilege")
def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathPolicyError):
        n.normalize_scope(proj, "link/x.txt")


def test_whole_project_scope(tmp_path):
    assert key(tmp_path, "./") == "/"


def test_named_resources():
    assert n.normalize_name("unity_editor") == "unity_editor"
    assert n.normalize_name("device", "Emulator-5554") == "emulator-5554"
    assert n.normalize_name("git_branch", "claude/cpm-x") == "claude/cpm-x"
    for kind, name in [
        ("git_branch", "a..b"),
        ("git_branch", "x/"),
        ("device", "a b"),
        ("nope", None),
    ]:
        with pytest.raises(PathPolicyError):
            n.normalize_name(kind, name)
