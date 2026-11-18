"""The dashboard's job list (self-host): GET /api/jobs.

What must hold: every held job is listed newest first with its status, label
and clip count, a queued job carries its place in line, and the cloud does not
expose the endpoint (it lists every user's jobs).
"""
import asyncio

import httpx
import pytest

app_module = pytest.importorskip("app")


@pytest.fixture()
def jobs(monkeypatch):
    table = {}
    monkeypatch.setattr(app_module, "jobs", table)
    monkeypatch.setattr(app_module, "BILLING_ENABLED", False)
    monkeypatch.setattr(app_module, "queue_snapshot",
                        lambda jid: {"position": 2} if table[jid]["status"] == "queued" else None)
    return table


def _get():
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/jobs")
    return asyncio.run(_do())


def test_lists_jobs_newest_first(jobs):
    jobs["old"] = {"status": "completed", "submitted_at": 1.0, "label": "a.mp4",
                   "result": {"clips": [{}, {}]}}
    jobs["new"] = {"status": "queued", "submitted_at": 2.0, "label": "https://youtu.be/x"}
    resp = _get()
    assert resp.status_code == 200
    listed = resp.json()["jobs"]
    assert [j["job_id"] for j in listed] == ["new", "old"]
    assert listed[0] == {"job_id": "new", "status": "queued", "label": "https://youtu.be/x",
                         "submitted_at": 2.0, "clips": 0, "position": 2}
    assert listed[1]["clips"] == 2 and listed[1]["position"] is None


def test_not_offered_with_billing(jobs, monkeypatch):
    monkeypatch.setattr(app_module, "BILLING_ENABLED", True)
    assert _get().status_code == 404
