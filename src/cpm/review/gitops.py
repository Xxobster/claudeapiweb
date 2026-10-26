"""Git plumbing for the gate and the integration (docs/THREATS.md T-04, T-05, T-30..T-33).

Reading a task's work never touches the worker's index or working tree: the changed files are
staged into a TEMPORARY index (`GIT_INDEX_FILE`), written as a tree, and compared with the
merge-base. Only `commit_all`, `push_branch` and nothing else changes a repository, and they run
on the task's own worktree/branch. There is no merge, rebase, reset, clean, stash, checkout or
force anywhere in this module (rule 13 of the master prompt, tests/security/test_review_security).
Every call is an argument list with the hardened command line of `discovery.gitinfo`.
"""

from __future__ import annotations

import os
import re
import subprocess  # noqa: S404 (argument lists only, see tests/security/test_injection.py)
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cpm.discovery.gitinfo import _HARDEN, _env, _filter_overrides
from cpm.security.redaction import redact

TIMEOUT_S = 120
PUSH_TIMEOUT_S = 300
MAX_DIFF_BYTES = 400_000  # one diff view / reviewer input; longer output is cut and marked
MAX_FILES = 2000
LARGE_FILE_BYTES = 5_000_000  # larger blobs are "unexpected" (LFS/binary policy of the project)
_IDENT = (
    "-c",
    "user.name=Claude Project Manager",
    "-c",
    "user.email=cpm@localhost.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "tag.gpgsign=false",
)
_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class GitOpError(RuntimeError):
    """A git step failed. The message is redacted and never carries file contents."""


@dataclass(frozen=True)
class Change:
    path: str
    status: str  # A | M | D | T
    mode: str  # new mode, e.g. 100644, 120000 (symlink), 160000 (submodule)
    added: int | None  # None for binary files
    deleted: int | None
    size: int  # new blob size in bytes (0 for deleted files)


@dataclass
class Snapshot:
    head: str
    base: str  # merge-base commit the diff is taken against
    tree: str  # tree of the worker's result (committed + uncommitted + untracked, not ignored)
    changes: list[Change] = field(default_factory=list)
    truncated: bool = False


def clean_path(p: str) -> str:
    return _CTRL.sub("?", p)[:500]


def run(
    root: Path,
    *args: str,
    config: Sequence[str] = (),
    env_extra: Mapping[str, str] | None = None,
    timeout: int = TIMEOUT_S,
    ok_codes: Sequence[int] = (0,),
    stdin: str | None = None,
) -> tuple[int, str]:
    env = _env()
    env.update(env_extra or {})
    cmd = ["git", *_HARDEN, *config, "-C", str(root), *args]
    try:
        done = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
            input=stdin,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitOpError(f"git {args[0]}: {type(exc).__name__}") from exc
    if done.returncode not in ok_codes:
        lines = (done.stderr or "").strip().splitlines()
        detail = redact(lines[-1] if lines else "")[:300]
        raise GitOpError(f"git {args[0]} exited with {done.returncode}: {detail}")
    return done.returncode, done.stdout


def _g(root: Path, *args: str, config: Sequence[str] = (), **kw: Any) -> str:
    return run(root, *args, config=(*_filter_overrides(root), *config), **kw)[1]


def rev_parse(root: Path, ref: str) -> str | None:
    """Full sha of `ref`, or None if it does not exist. `ref` is never option-like."""
    if ref.startswith("-"):
        raise GitOpError("refusing an option-like ref")
    code, out = run(root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", ok_codes=(0, 1))
    return out.strip() or None if code == 0 else None


def resolve_base(project: Path, base_ref: str) -> str:
    sha = rev_parse(project, base_ref)
    if sha is None:
        raise GitOpError(f"base {base_ref!r} does not exist in the project")
    return sha


def default_branch_name(project: Path, base_ref: str) -> str:
    """Branch a pull request targets: `origin/main` -> `main`; `HEAD` -> the project's branch."""
    if base_ref.startswith("origin/"):
        return base_ref.removeprefix("origin/")
    code, out = run(project, "symbolic-ref", "--short", "-q", "HEAD", ok_codes=(0, 1))
    return out.strip() if code == 0 and out.strip() else "main"


def snapshot(worktree: Path, base_sha: str) -> Snapshot:
    """What the task changed relative to `base_sha`, without touching the worktree's index."""
    if not _SHA.match(base_sha):
        raise GitOpError("base must be a full commit sha")
    head = rev_parse(worktree, "HEAD")
    if head is None:
        raise GitOpError("the worktree has no commit")
    with tempfile.TemporaryDirectory(prefix="cpm-index-") as tmp:
        idx = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        _g(worktree, "read-tree", head, env_extra=idx)
        _g(worktree, "add", "-A", "--", ".", env_extra=idx)
        tree = _g(worktree, "write-tree", env_extra=idx).strip()
    fork = _g(worktree, "merge-base", base_sha, head, ok_codes=(0,)).strip()
    snap = Snapshot(head=head, base=fork, tree=tree)
    raw = _g(worktree, "diff-tree", "-r", "-z", "--no-renames", "--no-ext-diff", fork, tree)
    stat = _g(worktree, "diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", fork, tree)
    counts = _numstat(stat)
    items = _raw(raw)
    if len(items) > MAX_FILES:
        snap.truncated = True
        items = items[:MAX_FILES]
    sizes = _sizes(worktree, [sha for _p, _s, _m, sha in items if set(sha) != {"0"}])
    for path, status, mode, sha in items:
        add, dele = counts.get(path, (None, None))
        snap.changes.append(
            Change(path, status, mode, add, dele, 0 if status == "D" else sizes.get(sha, 0))
        )
    return snap


def _raw(out: str) -> list[tuple[str, str, str, str]]:
    """Parse `diff-tree -r -z`: ':old new oldsha newsha S' NUL path NUL."""
    parts = out.split("\0")
    items: list[tuple[str, str, str, str]] = []
    i = 0
    while i + 1 < len(parts):
        meta = parts[i]
        if not meta.startswith(":"):
            i += 1
            continue
        f = meta[1:].split(" ")
        if len(f) >= 5:
            items.append((clean_path(parts[i + 1]), f[4][:1], f[1], f[3]))
        i += 2
    return items


def _numstat(out: str) -> dict[str, tuple[int | None, int | None]]:
    res: dict[str, tuple[int | None, int | None]] = {}
    for rec in out.split("\0"):
        bits = rec.split("\t", 2)
        if len(bits) == 3:
            a, d, path = bits
            res[clean_path(path)] = (
                (int(a), int(d)) if a.isdigit() and d.isdigit() else (None, None)
            )
    return res


def _sizes(worktree: Path, shas: list[str]) -> dict[str, int]:
    if not shas:
        return {}
    _, out = run(
        worktree,
        "cat-file",
        "--batch-check=%(objectname) %(objectsize)",
        stdin="\n".join(shas) + "\n",
    )
    res: dict[str, int] = {}
    for line in out.splitlines():
        name, _, size = line.partition(" ")
        if size.isdigit():
            res[name] = int(size)
    return res


def file_diff(worktree: Path, snap: Snapshot, path: str | None = None) -> tuple[str, bool]:
    """Unified diff (redacted, size-capped). `path` must be one of the snapshot's files."""
    args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "-U3"]
    args += [snap.base, snap.tree]
    if path is not None:
        if path not in {c.path for c in snap.changes}:
            raise GitOpError("file is not part of the change")
        args += ["--", f":(literal){path}"]
    out = _g(worktree, *args)
    cut = len(out.encode("utf-8", "replace")) > MAX_DIFF_BYTES
    if cut:
        out = out.encode("utf-8", "replace")[:MAX_DIFF_BYTES].decode("utf-8", "replace")
    return redact(out), cut


def commit_tree_probe(worktree: Path, snap: Snapshot) -> str:
    """A dangling commit holding the result on top of HEAD, so merge-tree can be asked about it.

    `commit-tree` only writes an object: no ref, index or working tree changes.
    """
    return _g(
        worktree, "commit-tree", snap.tree, "-p", snap.head, "-m", "cpm probe", config=_IDENT
    ).strip()


def merge_conflicts(worktree: Path, base_tip: str, commit: str) -> list[str]:
    """Paths that would conflict when `commit` is merged onto `base_tip` (empty = clean).

    Read-only: `git merge-tree --write-tree` computes the merge in memory (needs git >= 2.38).
    """
    for sha in (base_tip, commit):
        if not _SHA.match(sha):
            raise GitOpError("merge-tree needs full commit shas")
    code, out = run(
        worktree,
        "merge-tree",
        "--write-tree",
        "--name-only",
        "--no-messages",
        "-z",
        base_tip,
        commit,
        ok_codes=(0, 1),
    )
    if code == 0:
        return []
    names = [clean_path(n) for n in out.split("\0")[1:] if n]
    return names or ["(conflict)"]


def commit_all(worktree: Path, branch: str, message: str, expect_tree: str) -> str:
    """Commit the worker's result on its own branch, only if it is exactly the gated tree.

    Stages with `add -A` (the worktree is private to the task), refuses when the staged tree
    differs from `expect_tree` (the work changed after the gate), commits without hooks.
    """
    current = _g(worktree, "symbolic-ref", "--short", "-q", "HEAD").strip()
    if current != branch:
        raise GitOpError("the worktree is not on the task branch")
    _g(worktree, "add", "-A", "--", ".")
    tree = _g(worktree, "write-tree").strip()
    if tree != expect_tree:
        raise GitOpError("the work changed after the acceptance gate; run the gate again")
    head_tree = _g(worktree, "rev-parse", "HEAD^{tree}").strip()
    if tree != head_tree:
        msg = _CTRL.sub(" ", message)[:300]
        _g(worktree, "commit", "--no-verify", "-q", "-m", msg, config=_IDENT)
    return _g(worktree, "rev-parse", "HEAD").strip()


def push_branch(
    project: Path,
    branch: str,
    remote: str,
    token: str | None,
    protected: Sequence[str] = ("main",),
    source: str | None = None,
) -> None:
    """Push `refs/heads/<branch>` to `remote`. Never a force, never a protected branch.

    `source` (a full commit sha) pushes that commit instead of the local branch, so a commit
    built with plumbing needs no local branch (job board claims, WP11).
    """
    guard_push(branch, protected)
    if source is not None and not _SHA.match(source):
        raise GitOpError("push source must be a full commit sha")
    env: dict[str, str] = {}
    cfg: list[str] = ["-c", "credential.helper="]
    if token:
        import base64

        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }  # the header goes through the environment, never through argv or a file
    ref = f"refs/heads/{branch}"
    run(
        project,
        "push",
        "--porcelain",
        "--no-verify",
        "--",
        remote,
        f"{source or ref}:{ref}",
        config=cfg,
        env_extra=env,
        timeout=PUSH_TIMEOUT_S,
    )


_BRANCH = re.compile(r"^claude/[A-Za-z0-9._/-]{1,150}$")


def guard_push(branch: str, protected: Sequence[str]) -> None:
    """Only CPM task branches (`claude/...`) may be pushed, never a protected branch."""
    if not _BRANCH.fullmatch(branch) or ".." in branch or branch.endswith(("/", ".lock", ".")):
        raise GitOpError("only claude/* task branches may be pushed")
    if any(branch == p or branch.rsplit("/", 1)[-1] == p for p in protected):
        raise GitOpError(f"{branch} is a protected branch")


def branch_head(worktree: Path) -> tuple[str, str]:
    branch = _g(worktree, "symbolic-ref", "--short", "-q", "HEAD").strip()
    return branch, _g(worktree, "rev-parse", "HEAD").strip()


def _run_bytes(root: Path, args: Sequence[str], data: bytes | None = None) -> bytes:
    """Like `run`, but binary in and out: no newline translation on Windows."""
    cmd = ["git", *_HARDEN, *_filter_overrides(root), "-C", str(root), *args]
    try:
        done = subprocess.run(  # noqa: S603
            cmd, input=data, capture_output=True, timeout=TIMEOUT_S, env=_env(), check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitOpError(f"git {args[0]}: {type(exc).__name__}") from exc
    if done.returncode != 0:
        lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = redact(lines[-1] if lines else "")[:300]
        raise GitOpError(f"git {args[0]} exited with {done.returncode}: {detail}")
    return done.stdout


def read_blob(project: Path, commit: str, rel: str, max_bytes: int) -> bytes | None:
    """Bytes of `rel` at `commit` (None if absent). Reads the object store only."""
    if not _SHA.match(commit):
        raise GitOpError("a full commit sha is required")
    spec = f"{commit}:{rel}"
    listing = run(project, "ls-tree", commit, "--", f":(literal){rel}")[1]
    if not listing.strip():
        return None
    if listing.split(" ", 1)[0] not in ("100644", "100755"):
        raise GitOpError(f"{rel} is not a regular file at the base commit")
    size = int(run(project, "cat-file", "-s", spec)[1].strip() or 0)
    if size > max_bytes:
        raise GitOpError(f"{rel} is larger than {max_bytes} bytes")
    return _run_bytes(project, ["cat-file", "blob", spec])


def commit_blob(project: Path, base: str, rel: str, data: bytes, message: str) -> str:
    """A commit on top of `base` that replaces ONLY `rel` with `data` (object store only).

    Uses a temporary index and `commit-tree`: no ref, working tree or real index is touched, so
    the managed project's main tree cannot be disturbed. The file mode of `rel` is kept.
    """
    if not _SHA.match(base):
        raise GitOpError("a full commit sha is required")
    code, listing = run(project, "ls-tree", base, "--", f":(literal){rel}", ok_codes=(0,))
    mode = listing.split(" ", 1)[0] if listing.strip() else "100644"
    if mode not in ("100644", "100755"):
        raise GitOpError(f"{rel} is not a regular file at the base commit")
    blob = _run_bytes(project, ["hash-object", "-w", "--no-filters", "--stdin"], data)
    with tempfile.TemporaryDirectory(prefix="cpm-index-") as tmp:
        idx = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        _g(project, "read-tree", base, env_extra=idx)
        _g(
            project, "update-index", "--add", "--cacheinfo",
            f"{mode},{blob.decode().strip()},{rel}", env_extra=idx,
        )  # fmt: skip
        tree = _g(project, "write-tree", env_extra=idx).strip()
    msg = _CTRL.sub(" ", message)[:300]
    return _g(project, "commit-tree", tree, "-p", base, "-m", msg, config=_IDENT).strip()
