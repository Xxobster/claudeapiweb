"""Voice lane validators (WP13): format, loudness, silence and line-list completeness.

They judge files in the task worktree and run nothing (the render itself is a worker command
under the lane's `gpu` reservation). Audio is parsed here with the standard library only: PCM
8/16/24/32-bit and float 32/64 WAV files. File contents are untrusted: sizes are capped before
decoding, header fields are range-checked, and a file that cannot be parsed is a finding, never
a crash. Parameters come from the profile (trusted) and are range-checked too.

Loudness is the gated RMS level (windows above the silence threshold) and the sample peak, both
in dBFS. It is NOT a LUFS measurement (no K-weighting); use it for "not too quiet, not clipped",
and keep the thresholds loose enough for that.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import struct
import sys
from array import array
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from cpm.lanes.registry import Finding, Unavailable, ValidationContext, register_validator
from cpm.lanes.validators import _glob, _safe_file
from cpm.security.paths import PathPolicyError, validate_relative

WINDOW_S = 0.05
DEFAULT_MAX_AUDIO_BYTES = 32_000_000
DEFAULT_MAX_FILES = 500
MAX_IDS = 20_000
MAX_LIST_BYTES = 5_000_000
MAX_LISTED = 50  # findings of one kind spelled out before they are summarised
FLOOR_DB = -120.0
_ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$"


class AudioError(Exception):
    """The file is not a WAV file this module can read (the message is safe to show)."""


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    channels: int
    bits: int
    frames: int
    peak_db: float
    windows_db: tuple[float, ...]  # RMS level of each 50 ms window (all channels together)

    @property
    def duration_s(self) -> float:
        return self.frames / self.sample_rate

    @property
    def window_s(self) -> float:
        return WINDOW_S

    def gated_rms_db(self, threshold_db: float) -> float | None:
        """Mean power of the windows above the threshold, in dB; None if every window is silent."""
        loud = [w for w in self.windows_db if w > threshold_db]
        if not loud:
            return None
        return 10 * math.log10(sum(10 ** (w / 10) for w in loud) / len(loud))


def _db(power: float) -> float:
    return FLOOR_DB if power <= 0 else max(FLOOR_DB, 10 * math.log10(power))


def _decode(raw: bytes, tag: int, bits: int) -> tuple[Any, float]:
    """(sequence of numbers, full-scale value) for the raw sample bytes."""
    if tag == 1 and bits == 8:
        return [b - 128 for b in raw], 128.0
    if tag == 1 and bits == 16:
        a = array("h")
        a.frombytes(raw[: len(raw) - len(raw) % 2])
        if sys.byteorder == "big":
            a.byteswap()
        return a, 32768.0
    if tag == 1 and bits == 24:
        n = len(raw) // 3
        return [
            int.from_bytes(raw[i * 3 : i * 3 + 3], "little", signed=True) for i in range(n)
        ], 8388608.0
    if tag == 1 and bits == 32:
        a = array("i")
        a.frombytes(raw[: len(raw) - len(raw) % 4])
        if sys.byteorder == "big":
            a.byteswap()
        return a, 2147483648.0
    if tag == 3 and bits in (32, 64):
        a = array("f" if bits == 32 else "d")
        a.frombytes(raw[: len(raw) - len(raw) % (bits // 8)])
        if sys.byteorder == "big":
            a.byteswap()
        return a, 1.0
    raise AudioError(f"unsupported sample format (tag {tag}, {bits} bit)")


def read_wav(path: Path, max_bytes: int = DEFAULT_MAX_AUDIO_BYTES) -> WavInfo:
    size = path.stat().st_size
    if size > max_bytes:
        raise AudioError(f"file is {size} bytes, over the {max_bytes} byte check limit")
    data = path.read_bytes()
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioError("not a RIFF/WAVE file")
    pos, fmt, pcm = 12, None, None
    while pos + 8 <= len(data):
        cid, clen = data[pos : pos + 4], struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = pos + 8
        if cid == b"fmt " and clen >= 16 and body + 16 <= len(data):
            tag, ch, rate, _br, _ba, bits = struct.unpack("<HHIIHH", data[body : body + 16])
            if tag == 0xFFFE and clen >= 26 and body + 26 <= len(data):
                tag = struct.unpack("<H", data[body + 24 : body + 26])[0]
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            pcm = data[body : body + clen]  # a lying length is clamped by the slice
            break
        pos = body + clen + (clen & 1)
    if fmt is None or pcm is None:
        raise AudioError("missing fmt or data chunk")
    tag, ch, rate, bits = fmt
    if not (1 <= ch <= 8) or not (8000 <= rate <= 384_000):
        raise AudioError(f"implausible header ({ch} channel(s), {rate} Hz)")
    samples, full = _decode(pcm, tag, bits)
    total = len(samples) - len(samples) % ch
    if total == 0:
        raise AudioError("no audio samples")
    step = max(1, int(rate * WINDOW_S)) * ch
    peak, windows = 0.0, []
    for i in range(0, total, step):
        chunk = samples[i : min(i + step, total)]
        peak = max(peak, float(max(chunk)), float(-min(chunk)))
        windows.append(_db(sum(float(x) * x for x in chunk) / len(chunk) / (full * full)))
    return WavInfo(rate, ch, bits, total // ch, _db((peak / full) ** 2), tuple(windows))


@lru_cache(maxsize=256)
def _cached(real: str, size: int, mtime_ns: int, max_bytes: int) -> WavInfo | str:
    try:
        return read_wav(Path(real), max_bytes)
    except AudioError as exc:
        return str(exc)
    except (OSError, ValueError, struct.error, OverflowError) as exc:
        return f"unreadable ({type(exc).__name__})"


def _num(params: dict[str, Any], key: str, default: float | None, lo: float, hi: float) -> Any:
    v = params.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int | float) or not lo <= v <= hi:
        raise ValueError(f"parameter {key} must be a number between {lo} and {hi}")
    return v


def _int_list(params: dict[str, Any], key: str) -> list[int] | None:
    v = params.get(key)
    if v is None:
        return None
    if not isinstance(v, list) or not all(
        isinstance(x, int) and not isinstance(x, bool) for x in v
    ):
        raise ValueError(f"parameter {key} must be a list of integers")
    return v


def _wavs(
    ctx: ValidationContext, params: dict[str, Any]
) -> tuple[list[tuple[str, WavInfo]], list[Finding]]:
    """Parsed WAV files matching `glob` (changed ones only) and findings for unreadable ones."""
    paths = [p for p in _glob(ctx, params.get("glob")) if p.casefold().endswith(".wav")]
    limit = int(_num(params, "max_files", DEFAULT_MAX_FILES, 1, 5000))
    max_bytes = int(_num(params, "max_bytes", DEFAULT_MAX_AUDIO_BYTES, 1000, 200_000_000))
    out: list[tuple[str, WavInfo]] = []
    findings: list[Finding] = []
    if len(paths) > limit:
        findings.append(
            Finding(
                "audio_check_limit",
                f"{len(paths)} audio files match; at most {limit} are checked per task, "
                "split the work into smaller tasks",
            )
        )
        paths = paths[:limit]
    for rel in paths:
        f = _safe_file(ctx, rel)
        if f is None:
            findings.append(Finding("audio_unreadable", "missing or linked out", rel))
            continue
        st = f.stat()
        info = _cached(str(f), st.st_size, st.st_mtime_ns, max_bytes)
        if isinstance(info, str):
            findings.append(Finding("audio_unreadable", info, rel))
        else:
            out.append((rel, info))
    return out, findings


def audio_format(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{glob, sample_rates: [48000], channels: [1], bit_depths: [16], min/max_duration_s}`."""
    rates, chans, depths = (
        _int_list(params, "sample_rates"),
        _int_list(params, "channels"),
        _int_list(params, "bit_depths"),
    )
    lo = _num(params, "min_duration_s", 0.1, 0, 3600)
    hi = _num(params, "max_duration_s", None, 0.1, 3600)
    files, out = _wavs(ctx, params)
    for rel, w in files:
        if rates and w.sample_rate not in rates:
            out.append(Finding("sample_rate", f"{w.sample_rate} Hz, expected {rates}", rel))
        if chans and w.channels not in chans:
            out.append(Finding("channels", f"{w.channels} channel(s), expected {chans}", rel))
        if depths and w.bits not in depths:
            out.append(Finding("bit_depth", f"{w.bits} bit, expected {depths}", rel))
        if lo is not None and w.duration_s < lo:
            out.append(Finding("too_short", f"{w.duration_s:.2f} s, minimum {lo} s", rel))
        if hi is not None and w.duration_s > hi:
            out.append(Finding("too_long", f"{w.duration_s:.2f} s, maximum {hi} s", rel))
    return out


def audio_loudness(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{glob, max_peak_dbfs: -0.5, min_rms_dbfs: -35, max_rms_dbfs: -12, silence_dbfs: -50}`."""
    peak_max = _num(params, "max_peak_dbfs", -0.1, -60, 0)
    rms_lo = _num(params, "min_rms_dbfs", -40, -120, 0)
    rms_hi = _num(params, "max_rms_dbfs", -6, -120, 0)
    gate = _num(params, "silence_dbfs", -50, -120, -1)
    files, out = _wavs(ctx, params)
    for rel, w in files:
        if peak_max is not None and w.peak_db > peak_max:
            out.append(Finding("clipping", f"peak {w.peak_db:.1f} dBFS, maximum {peak_max}", rel))
        rms = w.gated_rms_db(gate)
        if rms is None:
            continue  # all silent: audio_silence reports it
        if rms_lo is not None and rms < rms_lo:
            out.append(Finding("too_quiet", f"level {rms:.1f} dBFS RMS, minimum {rms_lo}", rel))
        if rms_hi is not None and rms > rms_hi:
            out.append(Finding("too_loud", f"level {rms:.1f} dBFS RMS, maximum {rms_hi}", rel))
    return out


def audio_silence(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{glob, silence_dbfs: -50, max_silence_ratio, max_leading_s: 1.5, max_trailing_s: 2}`."""
    gate = _num(params, "silence_dbfs", -50, -120, -1)
    ratio = _num(params, "max_silence_ratio", 0.5, 0, 1)
    lead = _num(params, "max_leading_s", 1.5, 0, 3600)
    trail = _num(params, "max_trailing_s", 2.0, 0, 3600)
    files, out = _wavs(ctx, params)
    for rel, w in files:
        quiet = [x <= gate for x in w.windows_db]
        if all(quiet):
            out.append(Finding("silent", "the whole file is silent", rel))
            continue
        if ratio is not None and sum(quiet) / len(quiet) > ratio:
            out.append(
                Finding(
                    "too_much_silence",
                    f"{sum(quiet) / len(quiet):.0%} silent, max {ratio:.0%}",
                    rel,
                )
            )
        head = next(i for i, q in enumerate(quiet) if not q) * WINDOW_S
        tail = next(i for i, q in enumerate(reversed(quiet)) if not q) * WINDOW_S
        if lead is not None and head > lead:
            out.append(Finding("leading_silence", f"{head:.2f} s before speech, max {lead}", rel))
        if trail is not None and tail > trail:
            out.append(Finding("trailing_silence", f"{tail:.2f} s after speech, max {trail}", rel))
    return out


# ------------------------------------------------------------------ line list vs outputs
def _rel_param(params: dict[str, Any], key: str, *, required: bool = True) -> str | None:
    raw = params.get(key)
    if raw is None and not required:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"parameter {key} is required")
    try:
        return validate_relative(raw)
    except PathPolicyError as exc:
        raise ValueError(f"parameter {key}: {exc}") from exc


def _read_text(ctx: ValidationContext, rel: str) -> str | None:
    f = _safe_file(ctx, rel)
    if f is None or f.stat().st_size > MAX_LIST_BYTES:
        return None
    try:
        return f.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def _ids_from_json(data: Any) -> list[str]:
    if isinstance(data, dict) and "lines" in data:
        data = data["lines"]
    if isinstance(data, dict) and data and all(isinstance(v, dict) for v in data.values()):
        return [str(k) for k in data]
    if isinstance(data, list):
        ids: list[str] = []
        for item in data:
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.append(item["id"])
            else:
                raise Unavailable("the line list has an entry that is not an id or {id: ...}")
        return ids
    raise Unavailable("the line list has a shape this validator does not know")


def parse_line_ids(name: str, text: str) -> list[str]:
    """Ids from a JSON, CSV (header with an `id` column) or text file (`id`, `id|text`, tab)."""
    lower = name.casefold()
    if lower.endswith(".json"):
        try:
            return _ids_from_json(json.loads(text))
        except ValueError as exc:
            raise Unavailable("the line list is not valid JSON") from exc
    if lower.endswith(".csv"):
        rows = list(csv.DictReader(io.StringIO(text)))
        if rows and "id" not in rows[0]:
            raise Unavailable("the CSV line list has no 'id' column")
        return [(r.get("id") or "").strip() for r in rows]
    ids = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ids.append(line.replace("\t", "|").split("|", 1)[0].strip())
    return ids


def _listed(code: str, what: str, items: list[str], severity: Any = "error") -> list[Finding]:
    out = [Finding(code, what, i, severity) for i in items[:MAX_LISTED]]
    if len(items) > MAX_LISTED:
        out.append(Finding(code, f"... and {len(items) - MAX_LISTED} more: {what}", None, severity))
    return out


def voice_line_list(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """Every line of the script has an output file (and, optionally, no stray outputs).

    `{lines_dir: "Assets/StreamingAssets/Voice/lines", ext: ".rva",
    expected: "docs/voice/lines.json"
    | index: "Assets/StreamingAssets/Voice/index.json", extra: "warn"|"error"|"ignore"}`.
    The expected ids come from `expected` (the script's line list) or, without it, from `index`.
    """
    import re

    folder = _rel_param(params, "lines_dir")
    source = _rel_param(params, "expected", required=False) or _rel_param(params, "index")
    ext = str(params.get("ext", ".wav")).casefold()
    extra = params.get("extra", "warn")
    if not re.match(r"^\.[a-z0-9]{1,12}$", ext) or extra not in ("warn", "error", "ignore"):
        raise ValueError("parameter ext or extra is invalid")
    if folder is None or source is None:
        raise ValueError("parameters lines_dir and expected/index are required")
    text = _read_text(ctx, source)
    if text is None:
        raise Unavailable(f"cannot read the line list {source} (missing, linked out or too big)")
    ids = parse_line_ids(source, text)
    if not ids:
        return [Finding("empty_line_list", "the line list has no lines", source)]
    if len(ids) > MAX_IDS:
        raise Unavailable(f"the line list has more than {MAX_IDS} lines")
    out: list[Finding] = []
    bad = [i for i in ids if not re.match(_ID, i)]
    out += _listed("bad_line_id", "not a valid line id", bad)
    dup = sorted({i for i in ids if ids.count(i) > 1}) if len(ids) < 5000 else []
    out += _listed("duplicate_line_id", "listed more than once", dup)
    good = [i for i in dict.fromkeys(ids) if i not in bad]
    root = ctx.worktree.resolve()
    missing = []
    for i in good:
        f = _safe_file(ctx, f"{folder}/{i}{ext}")
        if f is None or f.stat().st_size == 0:
            missing.append(f"{folder}/{i}{ext}")
    out += _listed("missing_line", "no output file for this line", missing)
    if extra != "ignore":
        known = {f"{i}{ext}" for i in good}
        stray = []
        d = root / folder
        if d.is_dir() and d.resolve().is_relative_to(root):
            with os.scandir(d) as it:
                stray = sorted(
                    e.name
                    for e in it
                    if e.name.casefold().endswith(ext) and e.name not in known and e.is_file()
                )
        out += _listed(
            "extra_line",
            "output without a line in the list",
            [f"{folder}/{s}" for s in stray],
            extra,
        )
    return out


for _kind, _fn in {
    "audio_format": audio_format,
    "audio_loudness": audio_loudness,
    "audio_silence": audio_silence,
    "voice_line_list": voice_line_list,
}.items():
    register_validator(_kind, _fn)
