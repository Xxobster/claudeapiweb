"""WP14 validators (3D, map, Unity) on synthetic files: pure file checks, no tool, no key."""

import json
import struct

from cpm.lanes.registry import ChangedFile, ValidationContext, get_validator
from tests.fixtures.repos import write


def ctx_for(tmp_path, *paths, status="A"):
    return ValidationContext(
        tmp_path,
        [
            ChangedFile(p, status, (tmp_path / p).stat().st_size if status != "D" else 0)
            for p in paths
        ],
        "t",
        [],
    )


def run(kind, ctx, **params):
    return get_validator(kind)(ctx, params)


def codes(fs):
    return sorted(f.code for f in fs)


def glb(doc, total_extra=0):
    body = json.dumps(doc).encode()
    body += b" " * (-len(body) % 4)
    total = 20 + len(body) + total_extra
    return (
        struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(body), 0x4E4F534A) + body
    )


def good_doc(**kw):
    doc = {
        "asset": {"version": "2.0"},
        "accessors": [
            {"count": 300, "min": [0, 0, 0], "max": [2, 3, 1]},
            {"count": 100},
        ],
        "meshes": [
            {
                "name": "rock",
                "primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 1}],
            }
        ],
        "nodes": [{"name": "rock_node", "scale": [1, 1, 1]}],
    }
    doc.update(kw)
    return doc


def put_glb(tmp_path, name, doc, **kw):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(glb(doc, **kw))
    return name


# ------------------------------------------------------------------ 3D
def test_glb_good_asset_passes(tmp_path):
    n = put_glb(tmp_path, "a/rock.glb", good_doc())
    c = ctx_for(tmp_path, n)
    assert run("model_opens", c, glob="a/*.glb") == []
    assert (
        run(
            "glb_limits",
            c,
            glob="a/*.glb",
            max_triangles=1000,
            max_extent=10,
            name_regex="^[a-z_]+$",
        )
        == []
    )


def test_glb_limits_report_each_problem(tmp_path):
    doc = good_doc(nodes=[{"name": "Bad Name", "scale": [2, 1, 1]}])
    doc["meshes"][0]["primitives"][0]["attributes"].pop("TEXCOORD_0")
    n = put_glb(tmp_path, "rock.glb", doc)
    got = run(
        "glb_limits",
        ctx_for(tmp_path, n),
        glob="*.glb",
        max_triangles=10,
        max_extent=1,
        name_regex="^[a-z_]+$",
    )
    assert set(codes(got)) == {
        "too_many_triangles",
        "missing_uv",
        "bad_scale",
        "too_large",
        "bad_object_name",
    }


def test_corrupt_models_are_errors_not_crashes(tmp_path):
    (tmp_path / "bad.glb").write_bytes(b"glTF" + b"\x00" * 30)
    (tmp_path / "trunc.glb").write_bytes(glb(good_doc(), total_extra=5))
    (tmp_path / "x.fbx").write_bytes(b"nope")
    (tmp_path / "ok.fbx").write_bytes(b"Kaydara FBX Binary  \x00\x1a\x00rest")
    (tmp_path / "e.glb").write_bytes(b"")
    c = ctx_for(tmp_path, "bad.glb", "trunc.glb", "x.fbx", "ok.fbx", "e.glb")
    got = run("model_opens", c, glob="*")
    assert sorted(f.path for f in got) == ["bad.glb", "e.glb", "trunc.glb", "x.fbx"]


def test_hostile_glb_accessors_do_not_crash(tmp_path):
    doc = good_doc(
        accessors=[{"count": "x"}, 5, None],
        meshes=[{"primitives": [{"attributes": {"POSITION": 99}, "indices": -1}, "junk"]}],
        nodes=["z", {"scale": ["a"]}],
    )
    n = put_glb(tmp_path, "h.glb", doc)
    assert codes(run("glb_limits", ctx_for(tmp_path, n), glob="*.glb")) == ["missing_uv"]


def test_symlinked_model_is_unreadable(tmp_path):
    out = tmp_path.parent / "outside.glb"
    out.write_bytes(glb(good_doc()))
    (tmp_path / "l.glb").symlink_to(out)
    assert codes(run("model_opens", ctx_for(tmp_path, "l.glb"), glob="*.glb")) == [
        "model_unreadable"
    ]


def test_blender_report(tmp_path):
    write(
        tmp_path,
        "a.report.json",
        json.dumps(
            {
                "triangles": 900,
                "has_uv": True,
                "dimensions": [1, 2, 3],
                "scale": [1, 1, 1],
                "names": ["ok_name"],
            }
        ),
    )
    write(tmp_path, "b.report.json", json.dumps({"triangles": 9000, "has_uv": False}))
    write(tmp_path, "c.report.json", "[1]")
    c = ctx_for(tmp_path, "a.report.json", "b.report.json", "c.report.json")
    got = run("blender_report", c, glob="*.report.json", max_triangles=1000)
    assert {(f.path, f.code) for f in got} == {
        ("b.report.json", "too_many_triangles"),
        ("b.report.json", "missing_uv"),
        ("c.report.json", "report_invalid"),
    }


# ------------------------------------------------------------------ map
def grid_json(tmp_path, name, tiles, **extra):
    write(tmp_path, name, json.dumps({"tiles": tiles, **extra}))
    return name


def test_map_dimensions_and_connectivity(tmp_path):
    a = grid_json(tmp_path, "m/a.json", [[0, 0, 1], [1, 0, 0]], width=3, height=2)
    b = grid_json(tmp_path, "m/b.json", [[0, 1, 0], [0, 1, 0]], width=9)
    c = grid_json(tmp_path, "m/c.json", [[0, 0], [0]])
    cx = ctx_for(tmp_path, a, b, c)
    got = run("map_dimensions", cx, glob="m/*.json", width=3, height=2)
    assert {(f.path, f.code) for f in got} == {
        ("m/b.json", "dimension_mismatch"),
        ("m/c.json", "map_not_rectangular"),
    }
    got = run("map_connectivity", ctx_for(tmp_path, a, b), glob="m/*.json")
    assert [(f.path, f.code) for f in got] == [("m/b.json", "map_disconnected")]
    assert not run("map_connectivity", ctx_for(tmp_path, b), glob="m/*.json", min_ratio=0.5)


def test_map_unreadable_and_empty(tmp_path):
    write(tmp_path, "n.json", "[1,2]")
    write(tmp_path, "w.json", json.dumps({"tiles": [[1, 1]]}))
    cx = ctx_for(tmp_path, "n.json", "w.json")
    assert codes(run("map_dimensions", cx, glob="*.json")) == ["map_unreadable"]
    assert codes(run("map_connectivity", cx, glob="*.json")) == ["map_no_floor", "map_unreadable"]


def test_seed_reproducibility(tmp_path):
    write(tmp_path, "o/z.run1.json", '{"a":1}')
    write(tmp_path, "o/z.run2.json", '{"a":1}')
    write(tmp_path, "o/y.run1.json", '{"a":1}')
    write(tmp_path, "o/y.run2.json", '{"a":2}')
    write(tmp_path, "o/x.run1.json", "{}")
    cx = ctx_for(tmp_path, "o/z.run1.json", "o/y.run1.json", "o/x.run1.json")
    got = run("map_seed_repro", cx, glob="o/*.run1.json", replace=["run1", "run2"])
    assert {(f.path, f.code) for f in got} == {
        ("o/y.run1.json", "not_reproducible"),
        ("o/x.run1.json", "repro_missing"),
    }


def test_golden_files_must_not_change(tmp_path):
    cx = ValidationContext(
        tmp_path, [ChangedFile("g/golden/a.json", "D", 0), ChangedFile("src/x", "M", 1)], "t"
    )
    assert codes(run("golden_unchanged", cx, globs=["g/golden/**"])) == ["golden_modified"]


# ------------------------------------------------------------------ Unity
NUNIT = (
    '<test-run id="2" total="{t}" passed="{p}" failed="{f}" inconclusive="{i}" '
    'skipped="0" result="{r}"><test-suite><test-case fullname="A.B" result="{cr}"/>'
    "</test-suite></test-run>"
)


def test_unity_results(tmp_path):
    p = "Logs/cpm-validation/results.xml"
    write(tmp_path, p, NUNIT.format(t=3, p=3, f=0, i=0, r="Passed", cr="Passed"))
    assert not run("unity_results", ctx_for(tmp_path), path=p)
    write(tmp_path, p, NUNIT.format(t=3, p=2, f=1, i=0, r="Failed(Child)", cr="Failed"))
    got = run("unity_results", ctx_for(tmp_path), path=p)
    assert codes(got) == ["tests_failed"] and "A.B" in got[0].message
    write(tmp_path, p, NUNIT.format(t=0, p=0, f=0, i=0, r="Passed", cr="Passed"))
    assert codes(run("unity_results", ctx_for(tmp_path), path=p)) == ["too_few_tests"]
    write(tmp_path, p, NUNIT.format(t=2, p=1, f=0, i=1, r="Passed", cr="Passed"))
    assert codes(run("unity_results", ctx_for(tmp_path), path=p)) == ["tests_inconclusive"]


def test_unity_results_missing_invalid_and_entities(tmp_path):
    c = ctx_for(tmp_path)
    assert codes(run("unity_results", c, path="nope.xml")) == ["results_missing"]
    write(tmp_path, "r.xml", "<test-run")
    assert codes(run("unity_results", c, path="r.xml")) == ["results_invalid"]
    write(tmp_path, "r.xml", "<root/>")
    assert codes(run("unity_results", c, path="r.xml")) == ["results_invalid"]
    write(tmp_path, "r.xml", '<!DOCTYPE x [<!ENTITY a "b">]><test-run total="1"/>')
    assert codes(run("unity_results", c, path="r.xml")) == ["results_refused"]


def test_unity_log(tmp_path):
    write(tmp_path, "e.log", "all good\nImporting...\n")
    assert not run("unity_log", ctx_for(tmp_path), path="e.log")
    write(
        tmp_path,
        "e.log",
        "Assets/Scripts/A.cs(12,5): error CS0103: The name 'x' does not exist\n"
        "Scripts have compiler errors\nThe referenced script (Unknown) on this Behaviour is "
        "missing!\nAborting batchmode due to failure:\n",
    )
    got = run("unity_log", ctx_for(tmp_path), path="e.log")
    assert codes(got) == ["compile_error", "fatal", "missing_script"]
    assert got[0].path == "Assets/Scripts/A.cs:12" or any(
        f.path == "Assets/Scripts/A.cs:12" for f in got
    )


def meta(tmp_path, rel, guid):
    write(tmp_path, rel, f"fileFormatVersion: 2\nguid: {guid}\n")


def test_unity_meta_integrity(tmp_path):
    g1, g2 = "a" * 32, "b" * 32
    write(tmp_path, "Assets/ok.cs", "x")
    meta(tmp_path, "Assets/ok.cs.meta", g1)
    write(tmp_path, "Assets/nometa.cs", "x")
    meta(tmp_path, "Assets/orphan.cs.meta", g2)  # asset absent
    write(tmp_path, "Assets/dup.cs", "x")
    meta(tmp_path, "Assets/dup.cs.meta", g1)  # same guid as ok.cs.meta
    write(tmp_path, "Assets/bad.cs", "x")
    write(tmp_path, "Assets/bad.cs.meta", "fileFormatVersion: 2\nguid: nothex\n")
    write(tmp_path, "Assets/.hidden", "x")
    cx = ctx_for(
        tmp_path,
        "Assets/ok.cs",
        "Assets/ok.cs.meta",
        "Assets/nometa.cs",
        "Assets/orphan.cs.meta",
        "Assets/dup.cs",
        "Assets/dup.cs.meta",
        "Assets/bad.cs",
        "Assets/bad.cs.meta",
        "Assets/.hidden",
    )
    got = {(f.path, f.code) for f in run("unity_meta", cx, roots=["Assets/"])}
    assert got == {
        ("Assets/nometa.cs", "missing_meta"),
        ("Assets/orphan.cs.meta", "orphan_meta"),
        ("Assets/bad.cs.meta", "bad_meta"),
        ("Assets/ok.cs.meta", "duplicate_guid"),
        ("Assets/dup.cs.meta", "duplicate_guid"),
    }


def test_unity_meta_deleted_asset_must_drop_its_meta(tmp_path):
    meta(tmp_path, "Assets/gone.cs.meta", "c" * 32)
    cx = ValidationContext(tmp_path, [ChangedFile("Assets/gone.cs", "D", 0)], "t")
    assert codes(run("unity_meta", cx)) == ["orphan_meta"]


def test_unity_stray_files(tmp_path):
    cx = ValidationContext(
        tmp_path, [ChangedFile("Library/x", "A", 1), ChangedFile("Assets/a", "A", 1)], "t"
    )
    assert codes(run("unity_no_stray_files", cx)) == ["generated_file"]
