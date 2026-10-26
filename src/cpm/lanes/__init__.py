"""Lane framework (WP12): a lane is a recipe for one kind of work (voice, 3D, map, Unity checks).

A lane is declared in `profiles/<project>.yaml` (`lanes:`), parsed by `spec.LaneSpec`, and enforced
by CPM, never by the worker's good will: its command allowlist is added to the tool permission
policy, its resources go through the normal reservations (heavy ones included), its output
validators and artifact policy run inside the acceptance gate, and its report format is checked.
WP13/WP14 add lane *types* (`registry.register_validator`) and profile entries; they do not touch
the engine.
"""
