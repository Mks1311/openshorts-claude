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


def test_a_queued_job_leaves_the_queue_at_once(jobs, monkeypatch):
    queue = asyncio.PriorityQueue()
    monkeypatch.setattr(app_module, "job_queue", queue)
    monkeypatch.setattr(app_module, "_clear_resume_manifest", lambda jid: None)

    async def _no_webhook(jid):
        return None
    monkeypatch.setattr(app_module, "_notify_job_webhook", _no_webhook)
    jobs["a"] = _job("queued")
    jobs["b"] = _job("queued")
    queue.put_nowait((2, 1.0, "a"))
    queue.put_nowait((2, 2.0, "b"))
    resp = _post("a")
    assert resp.json() == {"status": "removed"}
    assert jobs["a"]["status"] == "failed"
    assert [e[2] for e in queue._queue] == ["b"]


def _job_files(tmp_path, monkeypatch, job_id):
    out, up = tmp_path / "output", tmp_path / "uploads"
    (out / job_id).mkdir(parents=True)
    up.mkdir(exist_ok=True)
    (out / job_id / "partial.mp4").write_bytes(b"x")
    (up / f"{job_id}_talk.mp4").write_bytes(b"x")
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(up))
    return out, up


def test_a_removed_queued_job_leaves_no_files(jobs, monkeypatch, tmp_path):
    out, up = _job_files(tmp_path, monkeypatch, "a")
    queue = asyncio.PriorityQueue()
    monkeypatch.setattr(app_module, "job_queue", queue)

    async def _no_webhook(jid):
        return None
    monkeypatch.setattr(app_module, "_notify_job_webhook", _no_webhook)
    jobs["a"] = _job("queued")
    queue.put_nowait((2, 1.0, "a"))
    assert _post("a").json() == {"status": "removed"}
    assert not (out / "a").exists() and not list(up.iterdir())


def test_startup_clears_jobs_that_ended_without_clips(monkeypatch, tmp_path):
    out, up = _job_files(tmp_path, monkeypatch, "failed")
    (out / "done").mkdir()
    (out / "done" / "x_metadata.json").write_text("{}")
    (out / "resuming").mkdir()
    (out / "resuming" / ".resume.json").write_text("{}")
    (out / "thumbnails").mkdir()
    (out / ".geoip").mkdir()
    monkeypatch.setattr(app_module, "THUMBNAILS_DIR", str(out / "thumbnails"))
    app_module._clear_unfinished_job_dirs()
    assert sorted(p.name for p in out.iterdir()) == [".geoip", "done", "resuming", "thumbnails"]
    assert not list(up.iterdir())


def _delete(job_id):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.delete(f"/api/jobs/{job_id}")
    return asyncio.run(_do())


def test_deleting_a_finished_job_removes_it_and_its_files(jobs, monkeypatch, tmp_path):
    out, up = _job_files(tmp_path, monkeypatch, "j")
    jobs["j"] = _job("completed")
    assert _delete("j").json() == {"status": "deleted"}
    assert "j" not in jobs and not (out / "j").exists() and not list(up.iterdir())


def test_a_running_job_must_be_stopped_before_deleting(jobs, monkeypatch, tmp_path):
    out, _ = _job_files(tmp_path, monkeypatch, "j")
    jobs["j"] = _job("processing")
    assert _delete("j").status_code == 409
    assert "j" in jobs and (out / "j").exists()
    assert _delete("nope").status_code == 404
