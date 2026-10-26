"""The Nirandyr WP14 lanes: profile loads, commands pass the real permission policy, and nothing
outside the fixed forms is allowed."""

import pytest
from pydantic import ValidationError

from cpm.lanes.commands import make_hook
from cpm.lanes.registry import get_validator
from cpm.lanes.spec import CommandRule, LaneSpec
from cpm.profiles import load_profile
from cpm.workers.permissions import ToolContext, decide
from tests.fixtures.repos import write

LANES = load_profile("nirandyr").lanes


def make_ctx(tmp_path, lane, trusted=True):
    wt = tmp_path / "wt"
    spec = LANES[lane]
    for a in spec.assets():
        (wt / a).mkdir(parents=True, exist_ok=True)
    write(wt, "tools/sim/zone_gen.sh", "#!/bin/sh\n")
    write(wt, "Assets/Art/in.blend", "x")
    return ToolContext(wt, (), trusted, make_hook(spec.commands, spec.assets()))


def bash(ctx, cmd):
    return decide("Bash", {"command": cmd}, ctx)


def test_profile_has_the_three_lanes_and_known_validators():
    assert set(LANES) == {"three_d", "maps", "unity_validation"}
    for lane in LANES.values():
        for v in lane.validators:
            assert (
                get_validator(v.kind) is not None
                or v.kind in {"files_exist", "file_size", "name_pattern", "json_keys"}
                or True
            )
    import cpm.lanes.service  # noqa: F401  (registers the WP14 validators)

    for lane in LANES.values():
        for v in lane.validators:
            assert get_validator(v.kind) is not None, v.kind


def test_lanes_use_heavy_resources():
    p = load_profile("nirandyr")
    assert "build" in p.heavy_resources
    assert {r.type for r in LANES["unity_validation"].resources} == {"unity_editor", "gpu"}
    assert {r.type for r in LANES["three_d"].resources} == {"blender", "gpu"}


def test_map_lane_runs_only_the_fixed_verify_command(tmp_path):
    ctx = make_ctx(tmp_path, "maps")
    assert bash(ctx, "tools/sim/zone_gen.sh --world").allow
    assert bash(ctx, "./tools/sim/zone_gen.sh --world").allow
    for cmd in [
        "tools/sim/zone_gen.sh",
        "tools/sim/zone_gen.sh --world --write",
        "tools/sim/zone_gen.sh --dungeon",
        "tools/sim/dungeon_gen.sh --world",
        "bash tools/sim/zone_gen.sh --world",
        "sh tools/sim/zone_gen.sh --world",
        "tools/sim/zone_gen.sh --world; rm -rf .",
        "/opt/zone_gen.sh --world",
        "tools/sim/../sim/zone_gen.sh --world",
    ]:
        assert not bash(ctx, cmd).allow, cmd
    assert not bash(
        make_ctx(tmp_path, "maps", trusted=False), "tools/sim/zone_gen.sh --world"
    ).allow


def test_map_script_must_be_a_real_file_inside_the_worktree(tmp_path):
    ctx = make_ctx(tmp_path, "maps")
    (ctx.worktree / "tools/sim/zone_gen.sh").unlink()
    assert not bash(ctx, "tools/sim/zone_gen.sh --world").allow
    outside = tmp_path / "evil.sh"
    outside.write_text("x")
    (ctx.worktree / "tools/sim/zone_gen.sh").symlink_to(outside)
    assert not bash(ctx, "tools/sim/zone_gen.sh --world").allow


def test_unity_lane_allows_only_edit_mode_batch_tests(tmp_path):
    ctx = make_ctx(tmp_path, "unity_validation")
    base = "Unity -batchmode -nographics -runTests -testPlatform EditMode"
    assert bash(ctx, base).allow
    assert bash(
        ctx,
        f"{base} -projectPath . -testResults Logs/cpm-validation/results.xml "
        "-logFile Logs/cpm-validation/editor.log",
    ).allow
    for cmd in [
        "Unity -batchmode -runTests -testPlatform PlayMode",
        "Unity -batchmode -nographics -runTests -testPlatform EditMode -executeMethod X.Y",
        f"{base} -projectPath ..",
        f"{base} -projectPath /d/projects/game",
        f"{base} -testResults Assets/x.xml",
        f"{base} -logFile ../x.log",
        f"{base} -quit",
        "Unity -batchmode -createProject x",
        "Unity.exe -batchmode -nographics -runTests -testPlatform EditMode -username a",
    ]:
        assert not bash(ctx, cmd).allow, cmd
    assert not bash(make_ctx(tmp_path, "unity_validation", False), base).allow


def test_blender_lane_command_is_fixed(tmp_path):
    ctx = make_ctx(tmp_path, "three_d")
    pre = (
        "blender --background --factory-startup --python-exit-code 1 --python "
        "tools/blender/export_asset.py --"
    )
    out = "Assets/Art/Generated/rock.glb"
    assert bash(ctx, f"{pre} --in Assets/Art/in.blend --out {out}").allow
    for cmd in [
        "blender --python evil.py",
        "blender --background --python tools/blender/export_asset.py",
        f"{pre} --in Assets/Art/in.blend --out Assets/Other/rock.glb",
        f"{pre} --in /etc/passwd",
        f"{pre} --in ../x.blend",
        f"{pre} --evil 1",
        "blender -b --python-expr 'import os'",
    ]:
        assert not bash(ctx, cmd).allow, cmd


def test_script_program_rules_in_the_spec():
    ok = {"name": "g", "program": "tools/sim/zone_gen.sh", "needs_trust": True}
    CommandRule.model_validate(ok)
    for bad in [
        {**ok, "needs_trust": False},
        {**ok, "program": "tools/../x.sh"},
        {**ok, "program": "/abs/x.sh"},
        {**ok, "program": "tools/x.py"},
        {**ok, "program": "~/x.sh"},
        {**ok, "program": "C:/x.sh"},
    ]:
        with pytest.raises(ValidationError):
            CommandRule.model_validate(bad)
    with pytest.raises(ValidationError):  # the script may not sit inside the lane's outputs
        LaneSpec.model_validate({"commands": [ok], "artifacts": {"asset_paths": ["tools/sim/"]}})
