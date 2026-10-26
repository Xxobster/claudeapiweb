"""WP13: voice validators, the Nirandyr voice lane profile entry and the queue bias."""

import json
import math
import struct
import wave
from pathlib import Path

import pytest

from cpm.lanes import registry
from cpm.lanes.commands import make_hook
from cpm.lanes.registry import ChangedFile, Unavailable, ValidationContext, get_validator
from cpm.lanes.voice import AudioError, parse_line_ids, read_wav
from cpm.profiles import load_profile
from cpm.workers.permissions import ToolContext, decide
from tests.fixtures.repos import write
from tests.lanes.conftest import LANE as LANE_BASE
from tests.lanes.conftest import make_profile

RATE = 16000


def tone(seconds, amp=0.3, freq=220.0):
    return [amp * math.sin(2 * math.pi * freq * i / RATE) for i in range(int(seconds * RATE))]


def wav(path: Path, samples, rate=RATE, channels=1, bits=16):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        full = 2 ** (bits - 1) - 1
        frames = b"".join(struct.pack("<h", int(max(-1, min(1, s)) * full)) for s in samples)
        w.writeframes(frames if channels == 1 else frames * channels)


def ctx_for(wt, *rels):
    changed = [ChangedFile(r, "A", (wt / r).stat().st_size) for r in rels]
    return ValidationContext(wt, changed, "voice")


def run(kind, wt, rels, **params):
    return get_validator(kind)(ctx_for(wt, *rels), params)


def codes(findings):
    return sorted(f.code for f in findings)


@pytest.fixture
def wt(tmp_path):
    d = tmp_path / "wt"
    d.mkdir()
    return d


def test_validators_are_registered():
    for k in ("audio_format", "audio_loudness", "audio_silence", "voice_line_list"):
        assert k in registry.validator_kinds()


def test_read_wav_measures_rate_duration_and_level(wt):
    wav(wt / "a.wav", tone(1.0, 0.5))
    info = read_wav(wt / "a.wav")
    assert (info.sample_rate, info.channels, info.bits) == (RATE, 1, 16)
    assert info.duration_s == pytest.approx(1.0, abs=0.01)
    assert info.peak_db == pytest.approx(20 * math.log10(0.5), abs=0.2)
    assert info.gated_rms_db(-50) == pytest.approx(20 * math.log10(0.5 / math.sqrt(2)), abs=0.3)


@pytest.mark.parametrize(
    "blob", [b"", b"RIFF", b"RIFF\0\0\0\0WAVEfmt ", b"not a wav at all, really not"]
)
def test_garbage_is_an_error_not_a_crash(wt, blob):
    (wt / "x.wav").write_bytes(blob)
    with pytest.raises(AudioError):
        read_wav(wt / "x.wav")
    out = run("audio_format", wt, ["x.wav"], glob="*.wav")
    assert codes(out) == ["audio_unreadable"]


def test_lying_data_length_and_huge_files_are_contained(wt):
    wav(wt / "a.wav", tone(0.5))
    raw = bytearray((wt / "a.wav").read_bytes())
    i = raw.index(b"data")
    raw[i + 4 : i + 8] = struct.pack("<I", 0xFFFFFFFF)  # claims 4 GB
    (wt / "a.wav").write_bytes(raw)
    assert read_wav(wt / "a.wav").duration_s == pytest.approx(0.5, abs=0.01)
    out = run("audio_format", wt, ["a.wav"], glob="*.wav", max_bytes=1000)
    assert codes(out) == ["audio_unreadable"] and "limit" in out[0].message


def test_float_and_24_bit_wav(wt):
    n = RATE // 2
    body = struct.pack(f"<{n}f", *tone(0.5, 0.25))
    hdr = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(body), b"WAVE", b"fmt ", 16, 3, 1,
                      RATE, RATE * 4, 4, 32, b"data", len(body))  # fmt: skip
    (wt / "f.wav").write_bytes(hdr + body)
    assert read_wav(wt / "f.wav").peak_db == pytest.approx(20 * math.log10(0.25), abs=0.2)
    pcm = b"".join(int(s * 8388607).to_bytes(3, "little", signed=True) for s in tone(0.2, 0.5))
    hdr = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1,
                      RATE, RATE * 3, 3, 24, b"data", len(pcm))  # fmt: skip
    (wt / "g.wav").write_bytes(hdr + pcm)
    assert read_wav(wt / "g.wav").bits == 24


def test_format_checks(wt):
    wav(wt / "ok.wav", tone(1.0))
    wav(wt / "short.wav", tone(0.05))
    wav(wt / "long.wav", tone(3.0))
    wav(wt / "rate.wav", tone(1.0), rate=8000)
    wav(wt / "st.wav", tone(1.0), channels=2)
    files = ["ok.wav", "short.wav", "long.wav", "rate.wav", "st.wav"]
    out = run("audio_format", wt, files, glob="*.wav", sample_rates=[RATE], channels=[1],
              bit_depths=[16], min_duration_s=0.2, max_duration_s=2)  # fmt: skip
    got = {(f.path, f.code) for f in out}
    assert got == {
        ("short.wav", "too_short"), ("long.wav", "too_long"),
        ("rate.wav", "sample_rate"), ("st.wav", "channels"),
    }  # fmt: skip


def test_loudness_clipping_quiet_and_loud(wt):
    wav(wt / "good.wav", tone(1.0, 0.2))
    wav(wt / "clip.wav", tone(1.0, 1.0))
    wav(wt / "quiet.wav", tone(1.0, 0.008))
    out = run("audio_loudness", wt, ["good.wav", "clip.wav", "quiet.wav"], glob="*.wav",
              min_rms_dbfs=-40, max_rms_dbfs=-8)  # fmt: skip
    assert {(f.path, f.code) for f in out} == {
        ("clip.wav", "clipping"), ("clip.wav", "too_loud"), ("quiet.wav", "too_quiet"),
    }  # fmt: skip


def test_silence_checks(wt):
    wav(wt / "all.wav", [0.0] * RATE)
    wav(wt / "ok.wav", tone(1.0))
    wav(wt / "lead.wav", [0.0] * (RATE * 3) + tone(1.0))
    wav(wt / "trail.wav", tone(1.0) + [0.0] * (RATE * 3))
    files = ["all.wav", "ok.wav", "lead.wav", "trail.wav"]
    out = run("audio_silence", wt, files, glob="*.wav", max_silence_ratio=0.9)
    assert {(f.path, f.code) for f in out} == {
        ("all.wav", "silent"), ("lead.wav", "leading_silence"), ("trail.wav", "trailing_silence"),
    }  # fmt: skip
    out = run("audio_silence", wt, ["lead.wav"], glob="*.wav", max_leading_s=10)
    assert codes(out) == ["too_much_silence"]  # 75% silent


def test_only_changed_wavs_matching_the_glob_are_read(wt):
    wav(wt / "keep/a.wav", tone(1.0))
    wav(wt / "other/b.wav", tone(0.01))
    out = run("audio_format", wt, ["keep/a.wav", "other/b.wav"], glob="keep/*.wav")
    assert out == []
    (wt / "c.rva").write_bytes(b"\x00opaque")
    assert run("audio_format", wt, ["c.rva"], glob="*") == []  # not a WAV: not judged here


def test_symlink_out_of_the_worktree_is_not_followed(wt, tmp_path):
    wav(tmp_path / "secret.wav", tone(1.0))
    (wt / "link.wav").symlink_to(tmp_path / "secret.wav")
    out = run("audio_format", wt, ["link.wav"], glob="*.wav")
    assert codes(out) == ["audio_unreadable"]


def test_file_count_is_bounded(wt):
    for i in range(3):
        wav(wt / f"{i}.wav", tone(0.3))
    out = run("audio_format", wt, ["0.wav", "1.wav", "2.wav"], glob="*.wav", max_files=2)
    assert codes(out) == ["audio_check_limit"]


@pytest.mark.parametrize(
    "kind,params",
    [
        ("audio_format", {"min_duration_s": "long"}),
        ("audio_format", {"sample_rates": 48000}),
        ("audio_loudness", {"max_peak_dbfs": 3}),
        ("audio_silence", {"max_silence_ratio": 2}),
    ],
)
def test_bad_parameters_raise_so_the_gate_fails_closed(wt, kind, params):
    wav(wt / "a.wav", tone(0.5))
    with pytest.raises(ValueError):
        run(kind, wt, ["a.wav"], glob="*.wav", **params)


# ------------------------------------------------------------------ line list
def lines_setup(wt, ids=("a1", "a2", "b1"), have=("a1", "a2", "b1"), ext=".rva"):
    write(wt, "docs/lines.json", json.dumps([{"id": i, "text": "x"} for i in ids]))
    for h in have:
        write(wt, f"out/lines/{h}{ext}", "audio")
    return dict(lines_dir="out/lines", ext=ext, expected="docs/lines.json")


def test_line_list_complete(wt):
    assert run("voice_line_list", wt, [], **lines_setup(wt)) == []


def test_line_list_reports_missing_empty_and_extra(wt):
    p = lines_setup(wt, have=("a1", "zz"))
    write(wt, "out/lines/a2.rva", "")  # zero bytes counts as missing
    out = run("voice_line_list", wt, [], **p)
    assert {(f.code, f.path) for f in out} == {
        ("missing_line", "out/lines/a2.rva"), ("missing_line", "out/lines/b1.rva"),
        ("extra_line", "out/lines/zz.rva"),
    }  # fmt: skip
    assert next(f for f in out if f.code == "extra_line").severity == "warn"
    p["extra"] = "ignore"
    assert "extra_line" not in codes(run("voice_line_list", wt, [], **p))
    p["extra"] = "error"
    assert (
        next(f for f in run("voice_line_list", wt, [], **p) if f.code == "extra_line").severity
        == "error"
    )


def test_line_ids_cannot_escape_the_folder(wt):
    p = lines_setup(wt, ids=("../../etc/passwd", "ok1", "a/b", "a1"), have=("ok1", "a1"))
    out = run("voice_line_list", wt, [], **p)
    assert codes(out) == ["bad_line_id", "bad_line_id"]  # and none became a file lookup


def test_line_list_from_index_and_formats(wt):
    write(wt, "idx.json", json.dumps({"version": 2, "lines": {"a1": {"hash": "x"}, "b1": {}}}))
    write(wt, "out/lines/a1.rva", "x")
    out = run("voice_line_list", wt, [], lines_dir="out/lines", ext=".rva", index="idx.json")
    assert [(f.code, f.path) for f in out] == [("missing_line", "out/lines/b1.rva")]
    assert parse_line_ids("l.csv", "id,text\na1,hello\nb2,yo\n") == ["a1", "b2"]
    assert parse_line_ids("l.txt", "# c\na1|hello\n\nb2\tyo\nc3\n") == ["a1", "b2", "c3"]
    assert parse_line_ids("l.json", '{"lines": ["x", {"id": "y"}]}') == ["x", "y"]


@pytest.mark.parametrize(
    "name,body",
    [("l.json", "{"), ("l.json", '{"a": 1}'), ("l.json", "[1, 2]"), ("l.csv", "name\nx\n")],
)
def test_unknown_list_shapes_make_the_gate_wait(name, body):
    with pytest.raises(Unavailable):
        parse_line_ids(name, body)


def test_missing_list_waits_and_empty_list_fails(wt):
    with pytest.raises(Unavailable):
        run("voice_line_list", wt, [], lines_dir="o", index="nope.json", ext=".rva")
    write(wt, "e.json", "[]")
    out = run("voice_line_list", wt, [], lines_dir="o", index="e.json", ext=".rva")
    assert codes(out) == ["empty_line_list"]


def test_path_params_are_confined(wt):
    for bad in ("../x", "/etc", "C:/x"):
        with pytest.raises(ValueError):
            run("voice_line_list", wt, [], lines_dir=bad, index="i.json", ext=".rva")


# ------------------------------------------------------------------ the Nirandyr profile entry
@pytest.fixture(scope="module")
def voice():
    return load_profile("nirandyr").lanes["voice"]


def test_profile_voice_lane_is_gpu_heavy_and_lowest_priority(voice):
    assert [r.type for r in voice.resources] == ["gpu"]
    assert voice.queue_bias >= 500
    assert voice.artifacts.binary_policy == "ask"  # a person decides about audio files
    kinds = {v.kind for v in voice.validators}
    assert kinds <= set(registry.validator_kinds()) and "voice_line_list" in kinds


@pytest.fixture
def vctx(tmp_path, voice):
    wt = tmp_path / "wt"
    write(wt, "tools/voice/render_fixed.py", "")
    (wt / "Assets/StreamingAssets/Voice/lines").mkdir(parents=True)
    return ToolContext(wt, (), True, make_hook(voice.commands, voice.assets()))


def test_voice_commands_need_a_trusted_workspace(voice, tmp_path):
    ctx = ToolContext(tmp_path, (), False, make_hook(voice.commands, voice.assets()))
    res = decide("Bash", {"command": "python tools/voice/render_fixed.py --check"}, ctx)
    assert not res.allow and "trusted" in res.reason


@pytest.mark.parametrize(
    "cmd,ok",
    [
        ("python tools/voice/render_fixed.py", True),
        ("python tools/voice/render_fixed.py --check", True),
        ("python.exe tools/voice/render_fixed.py", True),
        ("python tools/voice/render_fixed.py --out x", False),
        ("python tools/voice/render_fixed.py --check --force", False),
        ("python tools/voice/fetch_kokoro.sh", False),  # downloads models: not allowed
        ("python tools/voice/story_qc.py", False),
        ("python -c print(1)", False),
        ("python tools/voice/render_fixed.py && curl x", False),
        ("bash tools/voice/fetch_kokoro.sh", False),
        ("curl https://example.com", False),
        ("git push origin main", False),
    ],
)
def test_voice_commands_are_exactly_the_reviewed_script(vctx, cmd, ok):
    assert decide("Bash", {"command": cmd}, vctx).allow is ok, cmd


# ------------------------------------------------------------------ queue bias (rule 49)
def test_queue_bias_is_bounded():
    from pydantic import ValidationError

    from cpm.lanes.spec import LaneSpec

    assert LaneSpec(queue_bias=500).queue_bias == 500
    for bad in (-1, 5001):
        with pytest.raises(ValidationError):
            LaneSpec(queue_bias=bad)


def test_biased_lane_task_queues_behind_a_later_plain_task(lenv):
    from cpm.locks.admission import Admission
    from cpm.locks.reservations import ReservationService
    from cpm.scheduler.core import Scheduler

    lane = dict(LANE_BASE, queue_bias=500)
    lenv.profile = make_profile(lane)
    first = lenv.tasks.create(lenv.pid, "voice", billing_mode="mock", lane="test")
    second = lenv.tasks.create(lenv.pid, "plain", billing_mode="mock")
    for t in (first, second):
        lenv.tasks.mark_ready(t.id, actor="test")
    res = ReservationService(lenv.engine)
    kwargs = dict(resources_for=lenv.lanes.resources)

    def order(**extra):
        sched = Scheduler(
            lenv.engine, lenv.sup, Admission(lenv.engine, res), res, **kwargs, **extra
        )
        return [c.task_id for c in sched._candidates()[0]]

    assert order() == [first.id, second.id]  # without the hook: creation order
    assert order(priority_bias_for=lenv.lanes.priority_bias) == [second.id, first.id]
