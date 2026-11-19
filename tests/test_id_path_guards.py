"""Client-supplied ids that become directory names.

Thumbnail session ids and thumbnail URLs are joined onto OUTPUT_DIR. A
"../<other>" value must never attach or read another job's folder.
"""
import asyncio
import uuid

import httpx
import pytest

app_module = pytest.importorskip("app")


def _call(method, path, **kwargs):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await getattr(client, method)(path, **kwargs)
    return asyncio.run(_do())


@pytest.fixture()
def out(tmp_path, monkeypatch):
    out = tmp_path / "output"
    (out / "thumbnails").mkdir(parents=True)
    (tmp_path / "uploads").mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out))
    monkeypatch.setattr(app_module, "THUMBNAILS_DIR", str(out / "thumbnails"))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(app_module, "jobs", {})
    monkeypatch.setattr(app_module, "thumbnail_sessions", {})
    return out


def _other_job(out):
    job_id = str(uuid.uuid4())
    job_dir = out / job_id
    job_dir.mkdir()
    return job_id, job_dir


class TestThumbnailSessionIds:
    def test_manual_title_with_a_traversal_id_gets_a_fresh_session(self, out):
        job_id, _job_dir = _other_job(out)
        resp = _call("post", "/api/thumbnail/titles",
                     json={"title": "x", "session_id": f"../{job_id}"})
        assert resp.status_code == 200
        new_id = resp.json()["session_id"]
        assert app_module._is_uuid_id(new_id) and new_id != f"../{job_id}"

    def test_manual_title_with_a_client_uuid_still_works(self, out):
        sid = str(uuid.uuid4())
        resp = _call("post", "/api/thumbnail/titles", json={"title": "x", "session_id": sid})
        assert resp.status_code == 200
        assert resp.json()["session_id"] == sid


class TestThumbnailPublishPath:
    def _session(self, out, monkeypatch):
        sid = str(uuid.uuid4())
        video = out / "v.mp4"
        video.write_bytes(b"v")
        app_module.thumbnail_sessions[sid] = {"video_path": str(video)}

        async def fake_upload_post(request, api_key):
            return "k", "os_owner"
        monkeypatch.setattr(app_module, "resolve_upload_post", fake_upload_post)
        return sid

    def _publish(self, sid, url):
        return _call("post", "/api/thumbnail/publish", data={
            "session_id": sid, "title": "t", "description": "d", "thumbnail_url": url})

    def test_another_folders_image_is_refused(self, out, monkeypatch):
        sid = self._session(out, monkeypatch)
        other, other_dir = _other_job(out)
        (other_dir / "x.jpg").write_bytes(b"img")
        for url in (f"/thumbnails/../{other}/x.jpg", f"/thumbnails/{sid}/../../{other}/x.jpg",
                    f"/thumbnails/{other}/x.jpg"):
            assert self._publish(sid, url).status_code == 400, url

    def test_a_non_image_in_the_session_is_refused(self, out, monkeypatch):
        sid = self._session(out, monkeypatch)
        (out / "thumbnails" / sid).mkdir()
        (out / "thumbnails" / sid / "notes.txt").write_text("x")
        assert self._publish(sid, f"/thumbnails/{sid}/notes.txt").status_code == 400
