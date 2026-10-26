"""Lane configuration schema (strict: unknown keys are errors; loaded with yaml.safe_load).

Everything in a lane spec is trusted configuration written by the person who owns the profile.
Task text and repository files are never mixed into it. The spec still refuses the obvious
footguns (a shell, a network tool or git as an allowed program, an interpreter that may run any
script) so a typo in a profile cannot silently open the sandbox.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cpm.locks.normalize import NAMED_TYPES
from cpm.security.paths import PathPolicyError, validate_relative

LANE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PROGRAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_FLAG = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_EXT = re.compile(r"^\.[a-z0-9]{1,12}$")
_SECTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _/&.-]{0,59}$")

# Never allowed as a lane program: shells and remote/network tools (the worker has no network),
# deletion, and git (it has its own, stricter policy in `workers.permissions`).
FORBIDDEN_PROGRAMS = frozenset(
    {"sh", "bash", "zsh", "fish", "dash", "cmd", "powershell", "pwsh", "wsl", "env", "sudo",
     "su", "curl", "wget", "ssh", "scp", "sftp", "ftp", "nc", "ncat", "telnet", "rm", "rmdir",
     "del", "erase", "rd", "format", "git", "gh", "xargs", "eval", "exec", "start", "call"}
)  # fmt: skip
# Interpreters run whatever script they are given: only with a fixed script and nothing else.
INTERPRETERS = frozenset(
    {"python", "python3", "py", "node", "deno", "bun", "ruby", "perl", "php", "lua", "java"}
)

FlagKind = Literal["none", "value", "path", "out_path"]
PositionalKind = Literal["none", "path", "out_path"]
BinaryPolicy = Literal["ask", "lfs_only", "deny"]
DEFAULT_VALUE_PATTERN = r"^[A-Za-z0-9_.:=,+@/-]{1,200}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResourceSpec(Strict):
    """A machine resource the lane's work needs while it runs (reservation + heavy rules)."""

    type: str
    name: str | None = None

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in NAMED_TYPES or v == "git_branch":
            raise ValueError(f"unknown lane resource type {v!r}")
        return v


class CommandRule(Strict):
    """One command a lane worker may run through Bash: exact program, exact prefix, listed flags."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    program: str
    prefix: list[str] = []  # tokens that must follow the program, exactly
    flags: dict[str, FlagKind] = {}
    positional: PositionalKind = "none"
    max_positional: int = Field(default=0, ge=0, le=16)
    value_pattern: str = DEFAULT_VALUE_PATTERN
    needs_trust: bool = False  # runs repository code: only in a trusted workspace
    description: str = Field(default="", max_length=300)

    @field_validator("program")
    @classmethod
    def _program(cls, v: str) -> str:
        if not _PROGRAM.match(v):
            raise ValueError("program must be a plain command name, not a path or a pattern")
        if v.casefold().removesuffix(".exe") in FORBIDDEN_PROGRAMS:
            raise ValueError(f"program {v!r} can never be allowed in a lane")
        return v

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, v: list[str]) -> list[str]:
        for tok in v:
            if not tok or len(tok) > 200 or re.search(r"[\s;&|`$<>(){}\\\x00-\x1f]", tok):
                raise ValueError("prefix tokens must be simple words or relative paths")
            if tok.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", tok) or ".." in tok.split("/"):
                raise ValueError("prefix tokens must stay relative to the worktree")
        return v

    @field_validator("flags")
    @classmethod
    def _flags(cls, v: dict[str, FlagKind]) -> dict[str, FlagKind]:
        for flag in v:
            if not _FLAG.match(flag):
                raise ValueError(f"invalid flag {flag!r}")
        return v

    @field_validator("value_pattern")
    @classmethod
    def _pattern(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError("value_pattern is not a valid regular expression") from exc
        return v

    @model_validator(mode="after")
    def _interpreter(self) -> CommandRule:
        base = self.program.casefold().removesuffix(".exe")
        if base in INTERPRETERS and (not self.prefix or self.positional != "none" or self.flags):
            raise ValueError(
                f"{self.program} is an interpreter: allow it only with a fixed script in "
                "`prefix` and no flags or positional arguments"
            )
        if self.positional == "none" and self.max_positional:
            raise ValueError("max_positional needs positional: path or out_path")
        return self


class TaskTemplate(Strict):
    """Starting point for a task of this lane (`POST /api/projects/{id}/lanes/{lane}/tasks`)."""

    title_prefix: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=4000)
    required_inputs: list[str] = []  # names the person must fill (e.g. "script_file")
    default_scope: list[str] = []  # empty = the artifact policy's asset paths
    default_acceptance: list[str] = []

    @field_validator("required_inputs")
    @classmethod
    def _inputs(cls, v: list[str]) -> list[str]:
        for name in v:
            if not re.match(r"^[a-z][a-z0-9_]{0,31}$", name):
                raise ValueError(f"invalid input name {name!r}")
        return v

    @field_validator("default_scope")
    @classmethod
    def _scope(cls, v: list[str]) -> list[str]:
        return [_relative(p) for p in v]


class ArtifactPolicy(Strict):
    """Where a lane's outputs may land and what kind of file may be committed."""

    asset_paths: list[str] = []  # project-relative; a trailing '/' or no extension = a folder
    extensions: list[str] = []  # allowed output extensions (lower case, with the dot); empty = any
    binary_extensions: list[str] = []  # files needing the binary policy (audio, models, textures)
    max_file_bytes: int = Field(default=5_000_000, ge=1, le=2_000_000_000)
    # What to do with a binary: "ask" = stop and ask the person (Nirandyr rules on binaries),
    # "lfs_only" = allowed when .gitattributes routes it to Git LFS, "deny" = never.
    binary_policy: BinaryPolicy = "ask"

    @field_validator("asset_paths")
    @classmethod
    def _paths(cls, v: list[str]) -> list[str]:
        return [_relative(p) for p in v]

    @field_validator("extensions", "binary_extensions")
    @classmethod
    def _exts(cls, v: list[str]) -> list[str]:
        out = [e.casefold() for e in v]
        for e in out:
            if not _EXT.match(e):
                raise ValueError(f"invalid extension {e!r} (write it like '.wav')")
        return out


class ValidatorSpec(Strict):
    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    params: dict[str, Any] = {}
    severity: Literal["error", "warn"] = "error"


class ReportFormat(Strict):
    """Headings the worker's final answer must contain (checked, not trusted)."""

    required_sections: list[str] = []
    max_chars: int = Field(default=20_000, ge=100, le=200_000)

    @field_validator("required_sections")
    @classmethod
    def _sections(cls, v: list[str]) -> list[str]:
        for s in v:
            if not _SECTION.match(s):
                raise ValueError(f"invalid report section {s!r}")
        return v


class Limits(Strict):
    max_runtime_s: float | None = Field(default=None, gt=0, le=86_400)
    hang_timeout_s: float | None = Field(default=None, gt=0, le=3_600)


class LaneSpec(Strict):
    """One lane. `type` selects registered extra behaviour (WP13/14); `generic` has none."""

    type: str = Field(default="generic", pattern=r"^[a-z][a-z0-9_]{0,31}$")
    title: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=1000)
    enabled: bool = True
    task_template: TaskTemplate = TaskTemplate()
    # Instructions appended to the worker prompt. Trusted text from the profile; `{asset_paths}`,
    # `{commands}` and `{report_sections}` are replaced (plain substitution, nothing evaluated).
    prompt: str = Field(default="", max_length=6000)
    required_tools: dict[str, str] = {}  # label -> absolute path of the executable on the PC
    required_paths: dict[str, str] = {}  # label -> absolute file/folder that must exist
    resources: list[ResourceSpec] = []
    commands: list[CommandRule] = []
    validators: list[ValidatorSpec] = []
    artifacts: ArtifactPolicy = ArtifactPolicy()
    report: ReportFormat = ReportFormat()
    limits: Limits = Limits()

    @field_validator("required_tools", "required_paths")
    @classmethod
    def _abs(cls, v: dict[str, str]) -> dict[str, str]:
        for label, path in v.items():
            if not re.match(r"^[A-Za-z0-9_. -]{1,64}$", label):
                raise ValueError(f"invalid label {label!r}")
            absolute = path.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", path))
            if not absolute or "\x00" in path or ".." in re.split(r"[\\/]", path):
                raise ValueError(f"{label}: must be an absolute path without '..'")
        return v

    @model_validator(mode="after")
    def _unique(self) -> LaneSpec:
        names = [c.name for c in self.commands]
        if len(names) != len(set(names)):
            raise ValueError("command names must be unique within a lane")
        res = [(r.type, r.name) for r in self.resources]
        if len(res) != len(set(res)):
            raise ValueError("duplicate resource")
        return self

    def assets(self) -> list[str]:
        """Folders/files outputs may go to: the artifact policy, else the template scope."""
        return self.artifacts.asset_paths or self.task_template.default_scope


def _relative(raw: str) -> str:
    try:
        return validate_relative(raw)
    except PathPolicyError as exc:
        raise ValueError(f"path {raw!r} refused: {exc}") from exc
