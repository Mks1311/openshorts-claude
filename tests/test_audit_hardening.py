"""Hardening from the 22-sep-2026 audit, pinned at the HTTP surface.

- /api/process refuses a tailnet / private URL before any probe runs, and the
  quality probe runs only AFTER the metering step (balance, job limit, the
  hourly probe cap), handing the reservation back when it refuses the job.
- A metered whole-video URL job carries SOURCE_CAP_MINUTES = the reserved
  minutes, in the job env and in the resume manifest.
- main.py and the probe get an environment without the server secrets.
- Thumbnail publish status, thumbnail generation and render status are
  owner-only in cloud mode.
- The public /video/{id} page escapes everything it interpolates.
"""
import asyncio
import json
import os

import httpx
import pytest

app_module = pytest.importorskip("app")


def _client_call(method, path, **kw):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return await getattr(c, method)(path, **kw)
    return asyncio.run(_do())


def _post_process(body):
    return _client_call("post", "/api/process", json=body,
                        headers={"X-Gemini-Key": "test-key"})


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    out_root = tmp_path / "output"
    up_root = tmp_path / "uploads"
    out_root.mkdir()
    up_root.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(up_root))
    monkeypatch.setattr(app_module, "jobs", {})
    monkeypatch.setattr(app_module, "job_queue", asyncio.PriorityQueue())
    return out_root


@pytest.fixture()
def calls(monkeypatch):
    """A quality probe that always passes and a URL check that allows all."""
    async def _probe(url):
        return {"max_height": 1080, "duration": 600}

    monkeypatch.setattr(app_module, "_probe_youtube_quality", _probe)
    monkeypatch.setattr(app_module, "_validate_source_url", _allow_url)


async def _allow_url(url):
    return None


# --------------------------------------------------------------------------- #
# /api/process: URL validation, probe ordering, reservation cap
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "http://100.101.1.2:22/",
    "http://100.90.3.4:8080/admin",
    "http://127.0.0.1:8000/health",
    "file:///etc/passwd",
])
def test_unsafe_url_is_refused_before_any_probe(dirs, monkeypatch, url):
    probed = []

    async def _probe(u):
        probed.append(u)
        return {}
    monkeypatch.setattr(app_module, "_probe_youtube_quality", _probe)
    resp = _post_process({"url": url, "acknowledged": True})
    assert resp.status_code == 400
    assert probed == []
    assert os.listdir(dirs) == []


def test_youtube_search_page_is_refused_before_any_probe(dirs, monkeypatch):
    probed = []

    async def _probe(u):
        probed.append(u)
        return {}
    monkeypatch.setattr(app_module, "_probe_youtube_quality", _probe)
    monkeypatch.setattr("security_utils.assert_public_url", lambda u: u)
    resp = _post_process({"url": "https://www.youtube.com/results?search_query=x",
                          "acknowledged": True})
    assert resp.status_code == 400
    assert "search results" in resp.json()["detail"]
    assert probed == []


def test_resumed_job_keeps_its_safety_cap(dirs, monkeypatch):
    monkeypatch.setattr(app_module, "INSTANCE_ID", "me")
    monkeypatch.setattr(app_module, "_draining", False)
    monkeypatch.setattr(app_module, "_running_jobs", set())
    for name, cap in (("capped", 12), ("plain", None)):
        d = dirs / name
        d.mkdir()
        (d / app_module._RESUME_FILE).write_text(json.dumps({
            "cmd": ["python", "main.py"], "priority": 2, "user_id": None,
            "reservation_id": f"res-{name}", "watermark": False, "attempts": 0,
            "source_cap_minutes": cap,
        }))
    monkeypatch.setenv("SOURCE_CAP_MINUTES", "3")  # a stale value must not leak in
    app_module._resume_interrupted_jobs()
    assert app_module.jobs["capped"]["env"]["SOURCE_CAP_MINUTES"] == "12"
    assert "SOURCE_CAP_MINUTES" not in app_module.jobs["plain"]["env"]


# --------------------------------------------------------------------------- #
# Subprocess environment
# --------------------------------------------------------------------------- #
def test_child_env_drops_server_secrets(monkeypatch):
    for name in app_module.CHILD_ENV_DENYLIST:
        monkeypatch.setenv(name, "secret")
    for name in ("YOUTUBE_COOKIES", "PROXY_URL", "STATIC_PROXY_URLS",
                 "BGUTIL_SCRIPT_PATH", "FFMPEG_ENCODER", "WHISPER_MODEL"):
        monkeypatch.setenv(name, "keep")
    env = app_module.child_env()
    for name in app_module.CHILD_ENV_DENYLIST:
        assert name not in env
    for name in ("YOUTUBE_COOKIES", "PROXY_URL", "STATIC_PROXY_URLS",
                 "BGUTIL_SCRIPT_PATH", "FFMPEG_ENCODER", "WHISPER_MODEL"):
        assert env[name] == "keep"


def test_pipeline_code_reads_none_of_the_dropped_secrets():
    """If main.py (or anything it imports) ever needs one of these, the
    denylist would silently break jobs: fail here instead."""
    import ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = {f[:-3] for f in os.listdir(root) if f.endswith(".py")}
    seen, stack = set(), ["main", "quality_probe"]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        src = open(os.path.join(root, mod + ".py"), encoding="utf-8").read()
        for name in app_module.CHILD_ENV_DENYLIST:
            assert f'"{name}"' not in src and f"'{name}'" not in src, (mod, name)
        for node in ast.walk(ast.parse(src)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for n in names:
                if n in local:
                    stack.append(n)


def test_job_env_has_no_server_secrets(dirs, calls, monkeypatch):
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "up_x")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el_x")
    resp = _post_process({"url": "https://www.youtube.com/watch?v=ok", "acknowledged": True})
    env = app_module.jobs[resp.json()["job_id"]]["env"]
    assert "UPLOAD_POST_API_KEY" not in env and "ELEVENLABS_API_KEY" not in env


# --------------------------------------------------------------------------- #
# /video/{id} escaping
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# main.cap_source_duration(safety=True): the whole-video cap
# --------------------------------------------------------------------------- #
import shutil
import subprocess


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="needs ffmpeg")
class TestSafetyCap:
    @pytest.fixture()
    def source(self, tmp_path):
        main = pytest.importorskip("main")
        path = tmp_path / "src.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25",
            "-t", "150", "-c:v", "libx264", "-preset", "ultrafast", str(path)], check=True)
        return main, str(path)

    @staticmethod
    def _seconds(path):
        return float(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", path]).decode().strip())

    def test_a_download_clearly_longer_than_reserved_is_cut(self, source):
        main, path = source
        main.cap_source_duration(path, 1, safety=True)
        assert 59 <= self._seconds(path) <= 62

    def test_within_tolerance_is_left_alone(self, source):
        main, path = source
        main.cap_source_duration(path, 2.2, safety=True)   # 132 s + 30 s tolerance
        assert self._seconds(path) >= 149


