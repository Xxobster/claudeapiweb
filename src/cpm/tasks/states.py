"""Task statuses and the allowed-transition table (MASTER_PROMPT section 6, PLAN.md WP4).

This table is the single source of truth. Everything that changes `tasks.status` goes through
`TaskService.transition`, which checks it. Changing it affects scheduler, UI and gate tests
(docs/IMPACT_MAP.md).
"""

from __future__ import annotations

from typing import Final

STATUSES: Final = (
    "BACKLOG", "READY", "BLOCKED", "RESERVED", "RUNNING", "TESTING", "REVIEWING",
    "CORRECTING", "PAUSED", "PAUSED_BUDGET", "FAILED", "COMPLETED", "INTEGRATED", "CANCELLED",
)  # fmt: skip

TERMINAL: Final = frozenset({"INTEGRATED", "CANCELLED"})
# A task in one of these states holds (or is about to hold) reservations and a worker.
ACTIVE: Final = frozenset({"RESERVED", "RUNNING", "TESTING", "REVIEWING", "CORRECTING"})
# Dependencies are satisfied only when the upstream task passed its gate.
DONE: Final = frozenset({"COMPLETED", "INTEGRATED"})

_PAUSE_OR_STOP = {"PAUSED", "PAUSED_BUDGET", "FAILED", "CANCELLED"}

TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "BACKLOG": frozenset({"READY", "BLOCKED", "CANCELLED"}),
    "READY": frozenset({"BACKLOG", "BLOCKED", "RESERVED", "PAUSED", "CANCELLED"}),
    "BLOCKED": frozenset({"BACKLOG", "READY", "CANCELLED"}),
    "RESERVED": frozenset({"READY", "RUNNING"} | _PAUSE_OR_STOP),
    "RUNNING": frozenset({"TESTING"} | _PAUSE_OR_STOP),
    "TESTING": frozenset({"REVIEWING", "CORRECTING"} | _PAUSE_OR_STOP),
    "REVIEWING": frozenset({"COMPLETED", "CORRECTING", "TESTING"} | _PAUSE_OR_STOP),
    "CORRECTING": frozenset({"TESTING", "RUNNING"} | _PAUSE_OR_STOP),
    "PAUSED": frozenset({"READY", "RUNNING", "CANCELLED"}),
    "PAUSED_BUDGET": frozenset({"READY", "RUNNING", "CANCELLED"}),
    "FAILED": frozenset({"BACKLOG", "READY", "CANCELLED"}),
    "COMPLETED": frozenset({"INTEGRATED", "CORRECTING"}),
    "INTEGRATED": frozenset(),
    "CANCELLED": frozenset(),
}

# Moves a human may request through the API. Everything else (RESERVED, RUNNING, TESTING,
# REVIEWING, CORRECTING, PAUSED_BUDGET, COMPLETED, INTEGRATED) is driven by the scheduler,
# worker or acceptance gate, so no one can mark work finished by hand ("no task silently
# completed", scenario 20).
USER_TARGETS: Final = frozenset({"BACKLOG", "READY", "BLOCKED", "PAUSED", "CANCELLED"})
# From these the API's "retry/resume" also works: FAILED/PAUSED/PAUSED_BUDGET -> READY.

assert set(TRANSITIONS) == set(STATUSES)  # noqa: S101


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, frozenset())
