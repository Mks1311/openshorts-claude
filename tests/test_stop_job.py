"""Stopping a job from the dashboard (self-host): POST /api/jobs/{id}/stop.

What must hold: main.py and the processes it started are killed, the job ends
failed without an auto-retry, a queued job never starts, and the cloud does not
expose the endpoint (a stopped job there would refund minutes for kept clips).
"""
import asyncio
import sys
import time

import httpx
import pytest

app_module = pytest.importorskip("app")
psutil = pytest.importorskip("psutil")


@pytest.fixture()
def jobs(monkeypatch):
    table = {}
    monkeypatch.setattr(app_module, "jobs", table)
    monkeypatch.setattr(app_module, "BILLING_ENABLED", False)
    return table


def _job(status):
    return {"status": status, "logs": app_module._TimedLog([]), "cmd": ["main.py"]}


def _post(job_id):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(f"/api/jobs/{job_id}/stop")
    return asyncio.run(_do())


def test_stop_marks_a_running_job(jobs):
    jobs["j"] = _job("processing")
    resp = _post("j")
    assert resp.status_code == 200
    assert jobs["j"]["stop_requested"] is True
    assert "Stopped" in jobs["j"]["logs"][-1]


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_a_finished_job_cannot_be_stopped(jobs, status):
    jobs["j"] = _job(status)
    assert _post("j").status_code == 409


def test_unknown_job_is_404(jobs):
    assert _post("nope").status_code == 404


def test_not_offered_with_billing(jobs, monkeypatch):
    monkeypatch.setattr(app_module, "BILLING_ENABLED", True)
    jobs["j"] = _job("processing")
    assert _post("j").status_code == 404
    assert "stop_requested" not in jobs["j"]


def test_a_stopped_job_is_not_retried(jobs, monkeypatch):
    job = _job("failed")
    job["logs"].append("Connection reset by peer")
    monkeypatch.setattr(app_module, "is_transient_failure", lambda logs: True)
    assert app_module._should_auto_retry(job)
    job["stop_requested"] = True
    assert not app_module._should_auto_retry(job)


def test_a_queued_stopped_job_never_starts(jobs, tmp_path):
    jobs["j"] = {**_job("queued"), "stop_requested": True, "env": {},
                 "output_dir": str(tmp_path)}
    asyncio.run(app_module.run_job("j", jobs["j"]))
    assert jobs["j"]["status"] == "failed"
    assert "Job started by worker." not in list(jobs["j"]["logs"])


def test_run_main_kills_the_process_and_its_children(jobs):
    jobs["j"] = _job("processing")
    # A parent that starts a long-sleeping child, like main.py starting ffmpeg.
    script = ("import subprocess, sys, time; "
              "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
              "print('ready', flush=True); time.sleep(60)")

    async def _run():
        task = asyncio.create_task(app_module._run_main("j", [sys.executable, "-c", script], None))
        for _ in range(50):
            await asyncio.sleep(0.1)
            if any("ready" in str(line) for line in jobs["j"]["logs"]):
                break
        parent = next(p for p in psutil.process_iter(["cmdline"])
                      if p.info["cmdline"] and script in p.info["cmdline"])
        child = parent.children()[0]
        jobs["j"]["stop_requested"] = True
        started = time.monotonic()
        code = await asyncio.wait_for(task, timeout=10)
        return code, child, time.monotonic() - started

    code, child, took = asyncio.run(_run())
    assert code != 0
    assert took < 5
    child.wait(timeout=5)  # raises if the child outlived the stop
    assert not child.is_running()
