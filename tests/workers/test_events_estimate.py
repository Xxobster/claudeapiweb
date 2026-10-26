from cpm.workers.estimate import estimate_microusd
from cpm.workers.events import MAX_FIELD_CHARS, RunLog


def test_log_redacts_truncates_and_resumes_seq(tmp_path):
    log = RunLog(tmp_path / "r" / "events.jsonl")
    log.append(
        "text",
        text="token sk-ant-api03-" + "q" * 30,
        nested={"Authorization": "Bearer abcdefghij1234"},
    )
    log.append("text", text="x" * 10_000)
    ev = log.read()
    assert [e["seq"] for e in ev] == [1, 2]
    assert "sk-ant" not in str(ev[0]) and "abcdefghij1234" not in str(ev[0])
    assert len(ev[1]["text"]) < MAX_FIELD_CHARS + 20
    assert log.read(since=1)[0]["seq"] == 2
    assert RunLog(log.path).append("text", text="again")["seq"] == 3  # survives a restart


def test_log_is_size_capped(tmp_path):
    log = RunLog(tmp_path / "e.jsonl", max_bytes=600)
    for i in range(50):
        log.append("text", text=f"line {i} " + "y" * 40)
    ev = log.read(limit=1000)
    assert ev[-1]["type"] == "log_full" and log.path.stat().st_size < 1000


def test_estimate_is_integer_and_never_undercounts_unknown_models():
    # 1000 in + 500 out on Sonnet 5.5: 1000*2 + 500*10 = 7000 micro-USD
    assert estimate_microusd("claude-sonnet-5-5", 1000, 500) == 7000
    assert estimate_microusd("claude-haiku-5-5", 10, 0) == 1
    assert estimate_microusd("future-model", 1000, 0) == estimate_microusd(
        "claude-fable-5-1", 1000, 0
    )
    assert isinstance(estimate_microusd("claude-opus-5-5", 3, 3, 3, 3), int)
