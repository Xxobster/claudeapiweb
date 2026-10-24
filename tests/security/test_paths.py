"""Path validator (THREATS T-03, T-26): traversal, junction/symlink escape, spaces, Unicode."""

import os

import pytest

from cpm.security.paths import PathPolicyError, resolve_in_any, resolve_within, validate_root


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "Projet été 20 - PROJECTS"
    (r / "sub dir").mkdir(parents=True)
    (r / "sub dir" / "fichier é.txt").write_text("x")
    return r


def test_inside_ok_with_spaces_and_unicode(root):
    p = resolve_within(root, "sub dir/fichier é.txt", must_exist=True)
    assert p.name == "fichier é.txt"
    assert resolve_within(root, "sub dir\\nouveau.txt").name == "nouveau.txt"
    assert resolve_within(root, str(root / "sub dir")).name == "sub dir"


@pytest.mark.parametrize(
    "bad",
    [
        "../outside",
        "sub dir/../../outside",
        "..\\outside",
        "a/../../b",
        "..",
        "\\\\server\\share\\x",
        "//server/share",
        "\\\\?\\C:\\x",
        "\\\\.\\PhysicalDrive0",
        "con",
        "nul.txt",
        "sub/COM1",
        "lpt3.log",
        "aux ",
        "file.",
        "dir /x",
        "a:b",
        "x:stream",
        "a\x00b",
        "a|b",
        "a?b",
        "a*b",
        "",
        " lead",
        "trail ",
    ],
)
def test_rejected(root, bad):
    with pytest.raises(PathPolicyError):
        resolve_within(root, bad)


def test_absolute_outside_rejected(root, tmp_path):
    with pytest.raises(PathPolicyError):
        resolve_within(root, str(tmp_path))
    with pytest.raises(PathPolicyError):
        resolve_within(root, str(tmp_path / (root.name + "-evil") / "x"))  # shared prefix


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symlink_escape_rejected(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathPolicyError):
        resolve_within(root, "link/secret.txt")
    with pytest.raises(PathPolicyError):
        resolve_within(root, "link/new.txt")
    (root / "inner").symlink_to(root / "sub dir", target_is_directory=True)
    assert resolve_within(root, "inner/fichier é.txt").parent.name == "sub dir"


def test_symlinked_root_is_resolved(root, tmp_path):
    if os.name == "nt":
        pytest.skip("symlink")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    assert resolve_within(alias, "sub dir").parent == root.resolve()


def test_validate_root(root, tmp_path):
    assert validate_root(root) == root.resolve()
    for bad in (
        "relative/path",
        str(tmp_path / "missing"),
        str(root / "sub dir" / "fichier é.txt"),
        "\\\\host\\share",
    ):
        with pytest.raises(PathPolicyError):
            validate_root(bad)


def test_resolve_in_any(root, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    assert resolve_in_any([other, root], str(root / "sub dir")).name == "sub dir"
    with pytest.raises(PathPolicyError):
        resolve_in_any([other], str(root))
    with pytest.raises(PathPolicyError):
        resolve_in_any([], "x")
