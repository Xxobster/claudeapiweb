from sqlalchemy.orm import Session

from cpm.db.models import Run, UsageEvent


def test_dashboard_counts_and_spend_are_real(client, auth, project_id):
    empty = client.get("/api/dashboard", headers=auth).json()
    assert empty["active_runs"] == [] and empty["projects"][0]["task_counts"] == {}
    for i in range(2):
        client.post(f"/api/projects/{project_id}/tasks", json={"title": f"t{i}"}, headers=auth)
    t = client.post(f"/api/projects/{project_id}/tasks", json={"title": "r"}, headers=auth).json()
    client.post(f"/api/tasks/{t['id']}/transition", json={"to": "READY"}, headers=auth)
    with Session(client.app.state.engine) as s:
        run = Run(task_id=t["id"], billing_mode="mock")
        s.add(run)
        s.flush()
        for key, rec, cost in (("a", False, 1500), ("b", True, 2500)):
            s.add(
                UsageEvent(
                    run_id=run.id,
                    task_id=t["id"],
                    project_id=project_id,
                    dedup_key=key,
                    model="m",
                    cost_microusd=cost,
                    reconciled=rec,
                )
            )
        s.commit()
    d = client.get("/api/dashboard", headers=auth).json()
    p = d["projects"][0]
    assert p["task_counts"] == {"BACKLOG": 2, "READY": 1}
    assert p["spend_microusd"] == {"estimated": 1500, "reconciled": 2500}
    assert d["active_runs"] == []  # a DB row without a live process is not "running"
