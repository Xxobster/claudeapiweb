import pytest

KEY = "sk-ant-api03-" + "Zq9Xw8Vu" * 4


def body(tmp_path, **over):
    d = tmp_path / "proj é"
    d.mkdir(exist_ok=True)
    b = {
        "default_project_name": "Nirandyr",
        "project_dir": str(d),
        "task_budget_usd": "0.50",
        "project_budget_usd": "5",
        "global_budget_usd": "20.00",
        "max_parallel_agents": 3,
    }
    b.update(over)
    return b


def test_initial_status(client, auth):
    s = client.get("/api/wizard/status", headers=auth).json()
    assert (
        s["completed"] is False and s["live_runs_allowed"] is False and s["billing_mode"] == "api"
    )


def test_save_config_money_is_micro_usd(client, auth, tmp_path, settings):
    client.put("/api/credentials/anthropic", json={"api_key": KEY}, headers=auth)
    r = client.put("/api/wizard/config", json=body(tmp_path), headers=auth)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["completed"] and s["live_runs_allowed"]
    assert s["config"]["global_budget_usd"] == "20" or s["config"]["global_budget_usd"] == "20.00"
    from cpm.db.engine import make_engine
    from cpm.db.settings_repo import get_setting

    eng = make_engine(settings.db_path)
    assert get_setting(eng, "wizard.config")["global_budget_microusd"] == 20_000_000
    assert get_setting(eng, "approved_roots") == [s["config"]["project_dir"]]
    assert KEY not in str(get_setting(eng, "wizard.config"))


@pytest.mark.parametrize(
    "over",
    [
        {"global_budget_usd": "0"},
        {"task_budget_usd": "9", "project_budget_usd": "5"},
        {"task_budget_usd": "-1"},
        {"max_parallel_agents": 0},
        {"max_parallel_agents": 99},
        {"default_model": "gpt-5"},
        {"default_effort": "ludicrous"},
        {"git_url": "--upload-pack=evil"},
        {"git_url": "file:///etc"},
        {"git_url": "https://x y"},
        {"global_budget_usd": "1.234"},
        {"default_project_name": ""},
        {"project_dir": "relative"},
        {"project_dir": "../.."},
        {"project_dir": "\\\\srv\\share"},
        {"project_dir": "/definitely/missing"},
    ],
)
def test_invalid_config_rejected(client, auth, tmp_path, over):
    assert (
        client.put("/api/wizard/config", json=body(tmp_path, **over), headers=auth).status_code
        == 422
    )


def test_project_dir_cannot_overlap_data_dir(client, auth, settings, tmp_path):
    for p in (settings.data_dir, settings.data_dir.parent):
        r = client.put("/api/wizard/config", json=body(tmp_path, project_dir=str(p)), headers=auth)
        assert r.status_code == 422


def test_git_url_https_ok(client, auth, tmp_path):
    r = client.put(
        "/api/wizard/config",
        json=body(tmp_path, git_url="https://github.com/a/b.git"),
        headers=auth,
    )
    assert r.status_code == 200
