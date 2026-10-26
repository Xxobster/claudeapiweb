import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_api_reference_matches_the_code():
    spec = importlib.util.spec_from_file_location(
        "gen_api_doc", ROOT / "scripts" / "gen_api_doc.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (ROOT / "docs" / "API.md").read_text(encoding="utf-8") == mod.render(), (
        "docs/API.md is stale: run `python scripts/gen_api_doc.py`"
    )
