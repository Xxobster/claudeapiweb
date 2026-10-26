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
