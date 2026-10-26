import pytest
from pydantic import ValidationError

from cpm.lanes.spec import CommandRule, LaneSpec
from cpm.profiles import Profile, load_profile


def rule(**kw):
    return CommandRule.model_validate({"name": "x", "program": "tool", **kw})


@pytest.mark.parametrize(
    "program", ["bash", "sh", "powershell", "curl", "git", "ssh", "rm", "cmd.exe", "BASH"]
)
def test_dangerous_programs_can_never_be_allowed(program):
    with pytest.raises(ValidationError):
        rule(program=program)


@pytest.mark.parametrize("program", ["/usr/bin/tool", "tool;x", "a b", "..", "C:\\x.exe", ""])
def test_program_must_be_a_plain_name(program):
    with pytest.raises(ValidationError):
        rule(program=program)


def test_interpreter_only_with_a_fixed_script_and_nothing_else():
    rule(program="python", prefix=["tools/voice/run.py"])
    for bad in (
        {},
        {"prefix": ["run.py"], "positional": "path", "max_positional": 1},
        {"prefix": ["run.py"], "flags": {"-c": "value"}},
    ):
        with pytest.raises(ValidationError):
            rule(program="python", **bad)


@pytest.mark.parametrize("tok", ["/etc/passwd", "~/x", "C:\\x", "../x", "a;b", "a b"])
def test_prefix_tokens_stay_simple_and_relative(tok):
    with pytest.raises(ValidationError):
        rule(prefix=[tok])


def test_unknown_keys_and_bad_values_are_rejected():
    with pytest.raises(ValidationError):
        LaneSpec.model_validate({"nonsense": 1})
    with pytest.raises(ValidationError):
        LaneSpec.model_validate({"resources": [{"type": "git_branch"}]})
    with pytest.raises(ValidationError):
        LaneSpec.model_validate({"resources": [{"type": "toaster"}]})
    with pytest.raises(ValidationError):
        LaneSpec.model_validate({"required_tools": {"blender": "blender.exe"}})
    with pytest.raises(ValidationError):
        LaneSpec.model_validate({"required_tools": {"b": "D:\\x\\..\\y.exe"}})
    with pytest.raises(ValidationError):
        LaneSpec.model_validate({"artifacts": {"asset_paths": ["../escape"]}})
    with pytest.raises(ValidationError):
        LaneSpec.model_validate({"artifacts": {"extensions": ["wav"]}})
    with pytest.raises(ValidationError):
        LaneSpec.model_validate(
            {"commands": [{"name": "a", "program": "t"}, {"name": "a", "program": "u"}]}
        )
    with pytest.raises(ValidationError):
        LaneSpec.model_validate(
            {"commands": [{"name": "a", "program": "t", "value_pattern": "(unclosed"}]}
        )


def test_windows_absolute_tool_paths_are_accepted():
    s = LaneSpec.model_validate(
        {"required_tools": {"unity": "D:\\Unity\\Editors\\6000.3.25f1\\Editor\\Unity.exe"}}
    )
    assert "unity" in s.required_tools


def test_profile_lane_ids_are_validated_and_nirandyr_still_loads():
    with pytest.raises(ValidationError):
        Profile.model_validate({"id": "p", "lanes": {"Bad Name": {}}})
    assert load_profile("nirandyr").lanes is not None
