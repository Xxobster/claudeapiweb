"""3D lane validators (WP14): judge `.glb`/`.gltf`/`.fbx` files and Blender's JSON reports.

Read-only and pure Python: nothing here starts Blender. The heavy work (export, stats) is done by
the lane's fixed, reviewed in-repo Blender script, which may write `<asset>.report.json`; these
validators read that report or parse the glTF binary directly. File contents are untrusted: sizes
are capped and every access is bounds-checked, so a hostile file produces a finding, not a crash.
"""

from __future__ import annotations

import fnmatch
import json
import re
import struct
from dataclasses import dataclass, field
from typing import Any

from cpm.lanes.registry import Finding, ValidationContext, register_validator
from cpm.lanes.validators import _safe_file

MAX_GLB_BYTES = 200_000_000
MAX_JSON_CHUNK = 20_000_000
MAX_REPORT_BYTES = 1_000_000
GLB_MAGIC = 0x46546C67  # "glTF"
FBX_BINARY_MAGIC = b"Kaydara FBX Binary  \x00"


def _triangles(mode: Any, n: int) -> int:
    if mode == 4:
        return n // 3
    return max(n - 2, 0) if mode in (5, 6) else 0


@dataclass
class AssetStats:
    triangles: int = 0
    has_uv: bool = True
    uv_missing: list[str] = field(default_factory=list)
    extent: tuple[float, float, float] | None = None  # bounding box size in scene units
    scales: list[tuple[float, float, float]] = field(default_factory=list)
    names: list[str] = field(default_factory=list)  # node + mesh names


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def read_glb_json(raw: bytes) -> dict[str, Any]:
    """The JSON chunk of a binary glTF; ValueError when the container is malformed."""
    if len(raw) < 20:
        raise ValueError("file too short for a GLB")
    magic, version, total = struct.unpack_from("<III", raw, 0)
    if magic != GLB_MAGIC:
        raise ValueError("not a GLB (bad magic)")
    if version != 2:
        raise ValueError(f"unsupported glTF version {version}")
    if total != len(raw):
        raise ValueError("GLB length field does not match the file size")
    clen, ctype = struct.unpack_from("<II", raw, 12)
    if ctype != 0x4E4F534A or clen > MAX_JSON_CHUNK or 20 + clen > len(raw):
        raise ValueError("first chunk is not a valid JSON chunk")
    try:
        data = json.loads(raw[20 : 20 + clen].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("JSON chunk is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON chunk is not an object")
    return data


def gltf_stats(doc: dict[str, Any]) -> AssetStats:
    acc: list[Any] = doc["accessors"] if isinstance(doc.get("accessors"), list) else []

    def count(i: Any) -> int:
        if isinstance(i, int) and 0 <= i < len(acc) and isinstance(acc[i], dict):
            c = acc[i].get("count")
            return c if isinstance(c, int) and c >= 0 else 0
        return 0

    st = AssetStats()
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    meshes: list[Any] = doc["meshes"] if isinstance(doc.get("meshes"), list) else []
    for mi, mesh in enumerate(meshes):
        if not isinstance(mesh, dict):
            continue
        if isinstance(mesh.get("name"), str):
            st.names.append(mesh["name"])
        prims: list[Any] = mesh["primitives"] if isinstance(mesh.get("primitives"), list) else []
        for prim in prims:
            if not isinstance(prim, dict):
                continue
            attrs: dict[str, Any] = (
                prim["attributes"] if isinstance(prim.get("attributes"), dict) else {}
            )
            n = count(prim["indices"]) if "indices" in prim else count(attrs.get("POSITION"))
            st.triangles += _triangles(prim.get("mode", 4), n)
            if "TEXCOORD_0" not in attrs:
                st.has_uv = False
                st.uv_missing.append(str(mesh.get("name", f"mesh {mi}")))
            pos = attrs.get("POSITION")
            a = acc[pos] if isinstance(pos, int) and 0 <= pos < len(acc) else None
            if isinstance(a, dict):
                mn, mx = a.get("min"), a.get("max")
                if isinstance(mn, list) and isinstance(mx, list) and len(mn) == len(mx) == 3:
                    for k in range(3):
                        a0, a1 = _num(mn[k]), _num(mx[k])
                        if a0 is not None and a1 is not None:
                            lo[k], hi[k] = min(lo[k], a0), max(hi[k], a1)
    if lo[0] != float("inf") and all(h >= m for m, h in zip(lo, hi, strict=True)):
        st.extent = (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])
    nodes: list[Any] = doc["nodes"] if isinstance(doc.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("name"), str):
            st.names.append(node["name"])
        sc = node.get("scale")
        if isinstance(sc, list) and len(sc) == 3 and all(_num(x) is not None for x in sc):
            st.scales.append((float(sc[0]), float(sc[1]), float(sc[2])))
    return st


def report_stats(data: dict[str, Any]) -> AssetStats:
    """Stats from a Blender report: `{triangles, has_uv, dimensions:[x,y,z], scale:[..], names}`."""
    st = AssetStats()
    t = data.get("triangles", data.get("tris"))
    st.triangles = int(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else -1
    st.has_uv = data.get("has_uv") is True
    d = data.get("dimensions")
    if isinstance(d, list) and len(d) == 3 and all(_num(x) is not None for x in d):
        st.extent = (float(d[0]), float(d[1]), float(d[2]))
    s = data.get("scale")
    if isinstance(s, list) and len(s) == 3 and all(_num(x) is not None for x in s):
        st.scales = [(float(s[0]), float(s[1]), float(s[2]))]
    names = data.get("names")
    st.names = [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
    return st


def _limits(st: AssetStats, params: dict[str, Any], path: str) -> list[Finding]:
    out: list[Finding] = []
    mx = params.get("max_triangles")
    if mx is not None and st.triangles > int(mx):
        out.append(Finding("too_many_triangles", f"{st.triangles} triangles, max {mx}", path))
    mn = params.get("min_triangles")
    if mn is not None and st.triangles < int(mn):
        out.append(Finding("too_few_triangles", f"{st.triangles} triangles, min {mn}", path))
    if params.get("require_uv", True) and not st.has_uv:
        where = f" ({', '.join(st.uv_missing[:5])})" if st.uv_missing else ""
        out.append(Finding("missing_uv", f"mesh without UV coordinates{where}", path))
    lo, hi = _num(params.get("scale_min", 0.99)), _num(params.get("scale_max", 1.01))
    if lo is not None and hi is not None and params.get("require_unit_scale", True):
        if any(not (lo <= c <= hi) for sc in st.scales for c in sc):
            out.append(Finding("bad_scale", f"object scale outside [{lo}, {hi}]", path))
    me = _num(params.get("max_extent"))
    if me is not None and st.extent is not None and max(st.extent) > me:
        out.append(Finding("too_large", f"largest dimension {max(st.extent):.2f} > {me}", path))
    mi = _num(params.get("min_extent"))
    if mi is not None and st.extent is not None and max(st.extent) < mi:
        out.append(Finding("too_small", f"largest dimension {max(st.extent):.4f} < {mi}", path))
    rx = params.get("name_regex")
    if isinstance(rx, str):
        pat = re.compile(rx)
        out += [
            Finding("bad_object_name", f"{n!r} does not match {rx}", path)
            for n in st.names
            if not pat.search(n)
        ][:20]
    return out


def _read(ctx: ValidationContext, rel: str, cap: int) -> bytes | None:
    f = _safe_file(ctx, rel)
    if f is None or f.stat().st_size > cap:
        return None
    try:
        return f.read_bytes()
    except OSError:
        return None


def _matches(ctx: ValidationContext, glob: object) -> list[str]:
    if not isinstance(glob, str) or not glob:
        raise ValueError("a glob parameter is required")
    return [c.path for c in ctx.present if fnmatch.fnmatchcase(c.path, glob)]


def model_opens(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{glob: "Assets/Art/**/*.glb"}`: each file is a well-formed GLB/glTF/FBX container."""
    out: list[Finding] = []
    for rel in _matches(ctx, params.get("glob", "*.glb")):
        ext = rel.rsplit(".", 1)[-1].casefold()
        raw = _read(ctx, rel, MAX_GLB_BYTES)
        if raw is None:
            out.append(Finding("model_unreadable", "missing, linked out, or too big", rel))
        elif ext == "glb":
            try:
                gltf_stats(read_glb_json(raw))
            except ValueError as exc:
                out.append(Finding("model_corrupt", str(exc), rel))
        elif ext == "gltf":
            try:
                doc = json.loads(raw.decode("utf-8"))
                if not isinstance(doc, dict) or not isinstance(doc.get("asset"), dict):
                    raise ValueError("no 'asset' block")
            except (UnicodeDecodeError, ValueError) as exc:
                out.append(Finding("model_corrupt", f"not a valid glTF: {exc}", rel))
        elif ext == "fbx":
            if not (raw.startswith(FBX_BINARY_MAGIC) or raw.lstrip()[:5] == b"; FBX"):
                out.append(Finding("model_corrupt", "not an FBX file (bad header)", rel))
        else:
            out.append(Finding("model_unknown_format", f"unsupported extension .{ext}", rel))
    return out


def glb_limits(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{glob, max_triangles, min_triangles, require_uv, require_unit_scale, scale_min,
    scale_max, max_extent, min_extent, name_regex}` for each `.glb`."""
    out: list[Finding] = []
    for rel in _matches(ctx, params.get("glob", "*.glb")):
        raw = _read(ctx, rel, MAX_GLB_BYTES)
        if raw is None:
            out.append(Finding("model_unreadable", "missing, linked out, or too big", rel))
            continue
        try:
            st = gltf_stats(read_glb_json(raw))
        except ValueError as exc:
            out.append(Finding("model_corrupt", str(exc), rel))
            continue
        out += _limits(st, params, rel)
    return out


def blender_report(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """Same limits as `glb_limits`, from the JSON report the Blender batch script wrote
    (`{glob: "Assets/Art/**/*.report.json", ...}`). An unreadable report is an error."""
    out: list[Finding] = []
    for rel in _matches(ctx, params.get("glob", "*.report.json")):
        raw = _read(ctx, rel, MAX_REPORT_BYTES)
        try:
            data = json.loads(raw.decode("utf-8")) if raw is not None else None
        except (UnicodeDecodeError, ValueError):
            data = None
        if not isinstance(data, dict):
            out.append(
                Finding("report_invalid", "Blender report missing or not a JSON object", rel)
            )
            continue
        st = report_stats(data)
        if st.triangles < 0:
            out.append(Finding("report_invalid", "report has no triangle count", rel))
            continue
        out += _limits(st, params, rel)
    return out


for _kind, _fn in {
    "model_opens": model_opens,
    "glb_limits": glb_limits,
    "blender_report": blender_report,
}.items():
    register_validator(_kind, _fn)
