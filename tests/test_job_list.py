"""The dashboard's queue page (self-host): GET /api/jobs.

What must hold: every held job is listed newest first with what was asked
(source, range, settings) and where it stands (place in line, log while it
runs, why it failed), the settings and label survive a resume, and the cloud
does not expose the endpoint (it would list every user's jobs).
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
                        lambda jid: {"position": 2, "eta_seconds": 600}
                        if table[jid]["status"] == "queued" else None)
    return table


def _get():
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/api/jobs")
    return asyncio.run(_do())


def _log(*lines):
    return app_module._TimedLog(list(lines))


def test_lists_jobs_newest_first_with_their_details(jobs):
    settings = {"format": "vertical", "clips": 5, "min_seconds": None, "max_seconds": None,
                "layouts": [], "hook": "pill", "captions": True}
    jobs["old"] = {"status": "completed", "submitted_at": 1.0, "label": "a.mp4",
                   "result": {"clips": [{}, {}]}, "logs": _log("done"), "output_dir": "/nowhere"}
    jobs["new"] = {"status": "queued", "submitted_at": 2.0, "label": "https://youtu.be/x",
                   "settings": settings, "logs": _log("queued"),
                   "partial": {"start_seconds": 60, "end_seconds": 3600}}
    resp = _get()
    assert resp.status_code == 200
    listed = resp.json()["jobs"]
    assert [j["job_id"] for j in listed] == ["new", "old"]
    new, old = listed
    assert new["position"] == 2 and new["eta_seconds"] == 600
    assert new["settings"] == settings
    assert new["range"] == {"start": 60, "end": 3600}
    assert new["logs"] is None and new["finished_at"] is None
    assert old["clips"] == 2 and old["position"] is None and old["range"] is None
    assert old["finished_at"] is not None


def test_running_job_carries_its_log(jobs):
    jobs["j"] = {"status": "processing", "logs": _log("Transcribing video..."), "started_at": 5.0}
    row = _get().json()["jobs"][0]
    assert row["logs"] == ["Transcribing video..."] and row["started_at"] == 5.0


def test_a_stopped_job_says_so(jobs):
    jobs["j"] = {"status": "failed", "stop_requested": True, "logs": _log("Traceback: boom")}
    assert _get().json()["jobs"][0]["error"] == "Stopped before it finished."


def test_not_offered_with_billing(jobs, monkeypatch):
    monkeypatch.setattr(app_module, "BILLING_ENABLED", True)
    assert _get().status_code == 404


def test_label_and_settings_travel_in_the_resume_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(tmp_path))
    (tmp_path / "j").mkdir()
    app_module._write_resume_manifest("j", ["main.py"], 2, None, None, False,
                                      label="https://youtu.be/x", settings={"clips": 5})
    m = app_module._read_manifest("j")
    assert m["label"] == "https://youtu.be/x" and m["settings"] == {"clips": 5}


def _call(method, path, **kw):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kw)
    return asyncio.run(_do())


def test_models_can_be_picked_per_task(jobs):
    resp = _call("PUT", "/api/settings/models", json={"models": {"layout": "opus"}})
    assert resp.status_code == 200
    layout = next(t for t in resp.json()["tasks"] if t["id"] == "layout")
    assert layout["model"] == "opus" and layout["picked"] == "opus" and layout["default"] == "claude-sonnet-5-5"
    reset = _call("PUT", "/api/settings/models", json={"models": {"layout": None}})
    assert next(t for t in reset.json()["tasks"] if t["id"] == "layout")["model"] == "claude-sonnet-5-5"
    assert _call("PUT", "/api/settings/models", json={"models": {"layout": "x; rm -rf"}}).status_code == 400
    assert _call("PUT", "/api/settings/models", json={"models": {"nope": "haiku"}}).status_code == 400


def test_a_job_shows_its_token_usage(jobs, tmp_path):
    (tmp_path / ".usage.jsonl").write_text(
        '{"at": 1, "task": "clips", "model": "sonnet", "input_tokens": 1000, "output_tokens": 50, "seconds": 2}\n'
        '{"at": 2, "task": "layout", "model": "haiku", "input_tokens": "9", "output_tokens": 1, "seconds": 1}\n'
        'not json\n')
    jobs["j"] = {"status": "completed", "logs": _log("done"), "output_dir": str(tmp_path)}
    usage = _get().json()["jobs"][0]["usage"]
    assert usage["calls"] == 2 and usage["input_tokens"] == 1009
    assert usage["tasks"]["clips"]["models"] == {"sonnet": 1}


def test_usage_endpoint_sums_the_server_log(jobs):
    import claude_cli, time as _t
    claude_cli.record_usage({"at": _t.time(), "task": "studio", "model": "sonnet",
                             "input_tokens": 10, "output_tokens": 5, "seconds": 1, "ok": True})
    body = _call("GET", "/api/usage?days=1").json()
    assert body["calls"] == 1 and body["tasks"]["studio"]["input_tokens"] == 10 and len(body["days"]) == 1
