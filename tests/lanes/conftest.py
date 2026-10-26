"""Fixtures for lane tests: a lane-aware profile injected through `LaneService(loader=...)`."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from cpm.db.models import Project
from cpm.lanes.service import LaneService
from cpm.profiles import Profile
from cpm.review.gate import GateService
from cpm.review.reviewer import MockReviewer, ReviewerSource
from tests.review.conftest import FakeRunner
from tests.workers.conftest import env as worker_env  # noqa: F401  (fixture reused)

LANE = {
    "title": "Test lane",
    "prompt": "Write into {asset_paths}. Commands:\n{commands}",
    "resources": [{"type": "gpu"}],
    "commands": [
        {
            "name": "render",
            "program": "renderer",
            "flags": {"--seed": "value", "--out": "out_path", "--verbose": "none"},
            "positional": "path",
            "max_positional": 1,
            "description": "render a scene",
        }
    ],
    "validators": [{"kind": "files_exist", "params": {"glob": "assets/out/*.txt", "min_count": 1}}],
    "artifacts": {
        "asset_paths": ["assets/out/"],
        "extensions": [".txt", ".wav"],
        "binary_extensions": [".wav"],
        "max_file_bytes": 20000,
        "binary_policy": "ask",
    },
    "report": {"required_sections": ["Summary", "Checks"]},
    "limits": {"max_runtime_s": 120, "hang_timeout_s": 30},
    "task_template": {
        "title_prefix": "[lane] ",
        "description": "Make an output.",
        "required_inputs": ["scene"],
        "default_acceptance": ["output exists"],
    },
}
GOOD_REPORT = "## Summary\nDone\n## Checks\nAll good"


def make_profile(lane: dict | None = None, **extra) -> Profile:
    lane = LANE if lane is None else lane
    return Profile.model_validate({"id": "t", "lanes": {"test": lane}, **extra})


@pytest.fixture
def lenv(worker_env, monkeypatch):  # noqa: F811
    e = worker_env
    profile = make_profile()
    e.profile = profile
    monkeypatch.setattr("cpm.profiles.load_profile", lambda _name: e.profile)
    svc = LaneService(e.engine, loader=lambda _name: e.profile)
    e.sup.lanes = svc
    e.lanes = svc
    with Session(e.engine) as s:
        s.get(Project, e.pid).profile = "t"
        s.commit()
    e.runner = FakeRunner()
    e.gate = GateService(
        e.engine,
        e.sup,
        e.sup.worktrees,
        ReviewerSource(mock=MockReviewer()),
        e.runner,
        lanes=svc,
    )

    def lane_task(files, report=GOOD_REPORT, **kw):
        """Create a READY mock lane task, run `files` through the worker, return id in TESTING."""
        t = e.tasks.create(
            e.pid,
            "Make output",
            billing_mode="mock",
            acceptance=["output exists"],
            declared_scope=kw.pop("scope", ["assets/out/"]),
            lane="test",
            **kw,
        )
        e.tasks.mark_ready(t.id, actor="test")
        run = e.sup.start(
            t.id,
            scenario="files",
            scenario_args={"files": files, "report": report},
            heartbeat_s=0.2,
        )
        e.sup.wait(run.id, 30)
        return t.id

    e.lane_task = lane_task
    return e
