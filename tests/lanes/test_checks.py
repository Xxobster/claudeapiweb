"""Artifact policy, built-in validators and report format (pure file/text checks)."""

import pytest

from cpm.lanes.artifacts import check_artifacts, lfs_patterns
from cpm.lanes.registry import (
    ChangedFile,
    Finding,
    Unavailable,
    ValidationContext,
    get_validator,
    register_validator,
    validator_kinds,
)
from cpm.lanes.report import check_report
from cpm.lanes.spec import ArtifactPolicy, ReportFormat
from tests.fixtures.repos import write

ASSETS = ["assets/out/"]


def cf(path, size=10, status="A"):
    return ChangedFile(path, status, size)


def codes(findings):
    return sorted(f.code for f in findings)


def test_outputs_must_stay_under_the_asset_paths(tmp_path):
    rep = check_artifacts(
        ArtifactPolicy(), ASSETS, tmp_path, [cf("assets/out/a.txt"), cf("src/x.py")]
    )
    assert codes(rep.findings) == ["outside_assets"]
    assert rep.findings[0].path == "src/x.py"
    # deleted files are not outputs; no asset paths = no location rule
    assert not check_artifacts(
        ArtifactPolicy(), ASSETS, tmp_path, [cf("src/x", status="D")]
    ).findings
    assert not check_artifacts(ArtifactPolicy(), [], tmp_path, [cf("anywhere.bin")]).findings


def test_extension_and_size_limits(tmp_path):
    pol = ArtifactPolicy(extensions=[".txt"], max_file_bytes=100)
    rep = check_artifacts(
        pol,
        ASSETS,
        tmp_path,
        [cf("assets/out/a.exe"), cf("assets/out/b.txt", 101), cf("assets/out/c.txt", 100)],
    )
    assert codes(rep.findings) == ["extension", "too_large"]


def test_binary_policy_ask_deny_and_lfs(tmp_path):
    wav = cf("assets/out/v/line1.wav", 50)
    ask = ArtifactPolicy(binary_extensions=[".wav"], binary_policy="ask")
    rep = check_artifacts(ask, ASSETS, tmp_path, [wav])
    assert not rep.findings and rep.needs_decision == [wav.path]

    deny = ArtifactPolicy(binary_extensions=[".wav"], binary_policy="deny")
    assert codes(check_artifacts(deny, ASSETS, tmp_path, [wav]).findings) == ["binary_denied"]

    lfs = ArtifactPolicy(binary_extensions=[".wav"], binary_policy="lfs_only")
    assert codes(check_artifacts(lfs, ASSETS, tmp_path, [wav]).findings) == ["binary_not_lfs"]
    write(tmp_path, ".gitattributes", "# c\n*.wav filter=lfs diff=lfs merge=lfs -text\n")
    assert lfs_patterns(tmp_path) == ["*.wav"]
    assert not check_artifacts(lfs, ASSETS, tmp_path, [wav]).findings
    write(tmp_path, ".gitattributes", "assets/other/** filter=lfs\n")
    assert codes(check_artifacts(lfs, ASSETS, tmp_path, [wav]).findings) == ["binary_not_lfs"]


def test_gitattributes_symlink_is_ignored(tmp_path):
    real = tmp_path / "real"
    real.write_text("*.wav filter=lfs\n")
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / ".gitattributes").symlink_to(real)
    assert lfs_patterns(tmp_path / "wt") == []


def ctx_for(tmp_path, *changed):
    return ValidationContext(tmp_path, list(changed), "t", ASSETS)


def run(kind, ctx, **params):
    return get_validator(kind)(ctx, params)


def test_files_exist_and_counts(tmp_path):
    ctx = ctx_for(tmp_path, cf("assets/out/a.txt"), cf("assets/out/b.txt"), cf("x", status="D"))
    assert not run("files_exist", ctx, glob="assets/out/*.txt", min_count=2)
    assert codes(run("files_exist", ctx, glob="assets/out/*.txt", min_count=3)) == ["too_few_files"]
    assert codes(run("files_exist", ctx, glob="assets/out/*.txt", max_count=1)) == [
        "too_many_files"
    ]
    assert codes(run("files_exist", ctx, glob="x")) == ["too_few_files"]  # deleted does not count
    with pytest.raises(ValueError):
        run("files_exist", ctx)


def test_file_size_and_name_pattern(tmp_path):
    ctx = ctx_for(tmp_path, cf("assets/out/a.wav", 5), cf("assets/out/B.wav", 5000))
    assert codes(run("file_size", ctx, glob="*.wav", min_bytes=100)) == ["file_too_small"]
    assert codes(run("file_size", ctx, glob="*.wav", min_bytes=1, max_bytes=100)) == [
        "file_too_big"
    ]
    bad = run("name_pattern", ctx, glob="assets/**", regex="^[a-z0-9_/.]+$")
    assert [f.path for f in bad] == ["assets/out/B.wav"]


def test_json_keys_reads_only_inside_the_worktree(tmp_path):
    wt = tmp_path / "wt"
    write(wt, "assets/out/ok.json", '{"id": 1, "seed": 2}')
    write(wt, "assets/out/bad.json", "not json")
    write(wt, "assets/out/list.json", "[1]")
    write(wt, "assets/out/missing.json", '{"id": 1}')
    (tmp_path / "secret.json").write_text('{"id": 1, "seed": 1}')
    (wt / "assets/out/link.json").symlink_to(tmp_path / "secret.json")
    names = ["ok", "bad", "list", "missing", "link"]
    ctx = ValidationContext(wt, [cf(f"assets/out/{n}.json") for n in names], "t", ASSETS)
    got = run("json_keys", ctx, glob="assets/out/*.json", keys=["id", "seed"])
    by_path = {f.path.split("/")[-1]: f.code for f in got}
    assert by_path == {
        "bad.json": "json_invalid",
        "list.json": "json_shape",
        "missing.json": "json_key",
        "link.json": "json_unreadable",
    }


def test_registry_is_extensible_and_refuses_clashes():
    def mine(_ctx, _params):
        return [Finding("x", "y")]

    register_validator("unit_test_kind", mine)
    register_validator("unit_test_kind", mine)  # same function: idempotent
    assert "unit_test_kind" in validator_kinds()
    with pytest.raises(ValueError):
        register_validator("unit_test_kind", lambda c, p: [])
    assert issubclass(Unavailable, Exception)


def test_report_format():
    fmt = ReportFormat(required_sections=["Summary", "Checks run"], max_chars=200)
    assert not check_report(fmt, "## Summary\nok\n**Checks run**\nnone")
    assert not check_report(fmt, "Summary: ok\nChecks run: all")
    miss = check_report(fmt, "Summary\nfine")
    assert codes(miss) == ["report_section"] and "Checks run" in miss[0].message
    assert codes(check_report(fmt, None)) == ["report_missing"]
    assert codes(check_report(fmt, "## Summary\n## Checks run\n" + "x" * 300)) == ["report_long"]
    assert not check_report(ReportFormat(), None)  # no format = nothing to check
    # a heading mentioned in prose is not a section
    assert codes(check_report(fmt, "I wrote a Summary and Checks run today")) != []
