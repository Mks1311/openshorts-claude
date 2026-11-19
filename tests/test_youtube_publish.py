"""Direct YouTube Shorts publishing: /api/youtube/*.

What must hold: the OAuth round trip stores the tokens in output/.youtube.json
and refuses an unknown state; an expired access token is refreshed and a dead
refresh token says "reconnect"; an upload is resumable, carries #Shorts, is
private while scheduled and is recorded in the job's .youtube.json; a used-up
quota says so with 429. Google is an
httpx.MockTransport: no test reaches the network.
"""
import asyncio
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

app_module = pytest.importorskip("app")
import youtube_publish as yt  # noqa: E402

JOB = "12345678-1234-1234-1234-123456789abc"


class Google:
    """A fake of the endpoints youtube_publish calls; records every request."""

    def __init__(self):
        self.requests = []
        self.refresh_ok = True
        self.insert_error = None
        self.privacy_back = None

    def __call__(self, request):
        self.requests.append(request)
        url = str(request.url)
        if url.startswith(yt.TOKEN_URL):
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json={"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600})
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            return httpx.Response(200, json={"access_token": "at2", "expires_in": 3600})
        if url.startswith(yt.CHANNELS_URL):
            return httpx.Response(403, json={"error": {"code": 403, "message": "Insufficient Permission",
                                                       "errors": [{"reason": "insufficientPermissions"}]}})
        if url.startswith(yt.UPLOAD_URL) and request.method == "POST":
            if self.insert_error:
                return httpx.Response(403, json={"error": {"code": 403, "message": "quota",
                                                           "errors": [{"reason": self.insert_error}]}})
            return httpx.Response(200, headers={"Location": "https://upload.example/session/1"})
        if url == "https://upload.example/session/1":
            body = json.loads(self.requests[-2].content)
            return httpx.Response(200, json={
                "id": "dQw4w9WgXcQ",
                "snippet": {"channelId": "UC1", "channelTitle": "My Channel"},
                "status": {"privacyStatus": self.privacy_back or body["status"]["privacyStatus"]}})
        if url.startswith(yt.REVOKE_URL):
            return httpx.Response(200)
        return httpx.Response(404)


@pytest.fixture()
def google(tmp_path, monkeypatch):
    g = Google()
    monkeypatch.setattr(yt, "_transport", httpx.MockTransport(g))
    monkeypatch.setattr(yt, "STATE_FILE", str(tmp_path / ".youtube.json"))
    monkeypatch.setattr(yt, "_pending", {})
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "secret")
    return g


@pytest.fixture()
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(tmp_path))
    d = tmp_path / JOB
    d.mkdir()
    (d / "v_clip_1.mp4").write_bytes(b"\0" * 2048)
    table = {JOB: {"status": "completed", "output_dir": str(d), "logs": app_module._TimedLog([]),
                   "result": {"clips": [{"video_url": f"/videos/{JOB}/v_clip_1.mp4",
                                         "video_title_for_youtube_short": "A <great> moment",
                                         "video_description_for_tiktok": "so good"}]}}}
    monkeypatch.setattr(app_module, "jobs", table)
    return d


def _call(method, path, **kw):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kw)
    return asyncio.run(_do())


def _connect(google):
    resp = _call("GET", "/api/youtube/connect", headers={"referer": "http://localhost:5173/settings"})
    assert resp.status_code == 302
    q = parse_qs(urlparse(resp.headers["location"]).query)
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"] and q["scope"] == [yt.SCOPE]
    back = _call("GET", "/api/youtube/callback", params={"code": "c", "state": q["state"][0]})
    assert back.status_code == 302 and back.headers["location"] == "http://localhost:5173/?youtube=connected"


def test_connect_stores_tokens_and_status_reports_them(google):
    _connect(google)
    saved = json.loads(open(yt.STATE_FILE).read())
    assert saved["tokens"]["refresh_token"] == "rt1"
    st = _call("GET", "/api/youtube/status").json()
    assert st["connected"] and not st["reconnect_needed"] and st["channel"] is None


def test_callback_refuses_an_unknown_state(google):
    resp = _call("GET", "/api/youtube/callback", params={"code": "c", "state": "forged"})
    assert resp.status_code == 400 and not yt._read().get("tokens")


def test_expired_access_token_is_refreshed(google):
    _connect(google)
    data = yt._read()
    data["tokens"]["expires_at"] = time.time() - 10
    yt._write(data)
    assert _call("GET", "/api/youtube/status").json()["connected"]
    assert yt._read()["tokens"]["access_token"] == "at2"


def test_dead_refresh_token_asks_to_reconnect(google, job):
    _connect(google)
    data = yt._read()
    data["tokens"]["expires_at"] = 0
    yt._write(data)
    google.refresh_ok = False
    assert _call("GET", "/api/youtube/status").json()["reconnect_needed"]
    resp = _call("POST", "/api/youtube/upload", json={"job_id": JOB, "clip_index": 0})
    assert resp.status_code == 409 and "reconnect" in resp.json()["detail"]


def test_upload_is_resumable_tagged_and_recorded(google, job):
    _connect(google)
    google.privacy_back = "private"  # unaudited project: Google forces private
    resp = _call("POST", "/api/youtube/upload", json={"job_id": JOB, "clip_index": 0, "privacy": "public"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["video_id"] == "dQw4w9WgXcQ" and out["privacy"] == "private" and out["requested_privacy"] == "public"
    init = next(r for r in google.requests if str(r.url).startswith(yt.UPLOAD_URL))
    assert init.url.params["uploadType"] == "resumable" and init.headers["x-upload-content-length"] == "2048"
    body = json.loads(init.content)
    assert body["snippet"]["title"] == "A great moment" and "#Shorts" in body["snippet"]["description"]
    assert body["snippet"]["categoryId"] == "22"
    put = google.requests[-1]
    assert put.method == "PUT" and len(put.content) == 2048
    status = _call("GET", f"/api/status/{JOB}").json()
    assert status["youtube"]["0"]["url"] == "https://youtube.com/shorts/dQw4w9WgXcQ"
    assert yt._read()["channel"] == {"id": "UC1", "title": "My Channel"}


def test_scheduled_upload_goes_private_with_publish_at(google, job):
    _connect(google)
    resp = _call("POST", "/api/youtube/upload", json={"job_id": JOB, "clip_index": 0, "privacy": "public",
                                                      "publish_at": "2099-01-01T10:00:00+01:00"})
    assert resp.status_code == 200
    body = json.loads(next(r for r in google.requests if str(r.url).startswith(yt.UPLOAD_URL)).content)
    assert body["status"] == {"privacyStatus": "private", "publishAt": "2099-01-01T09:00:00Z"}
    past = _call("POST", "/api/youtube/upload", json={"job_id": JOB, "clip_index": 0,
                                                      "publish_at": "2001-01-01T00:00:00Z"})
    assert past.status_code == 400


def test_quota_exceeded_is_a_clear_429(google, job):
    _connect(google)
    google.insert_error = "quotaExceeded"
    resp = _call("POST", "/api/youtube/upload", json={"job_id": JOB, "clip_index": 0})
    assert resp.status_code == 429 and "quota" in resp.json()["detail"]
    assert yt.posted(str(job)) == {}


def test_upload_needs_a_connection(google, job):
    resp = _call("POST", "/api/youtube/upload", json={"job_id": JOB, "clip_index": 0})
    assert resp.status_code == 409
    assert _call("POST", "/api/youtube/upload", json={"job_id": JOB, "clip_index": 5}).status_code == 404


def test_sidecar_is_data(job):
    (job / ".youtube.json").write_text(json.dumps({
        "0": {"video_id": "dQw4w9WgXcQ", "privacy": "public"},
        "1": {"video_id": "../../x\"><script>"}, "x": {"video_id": "dQw4w9WgXcQ"}}))
    assert list(yt.posted(str(job))) == ["0"]


def test_auto_upload_posts_the_top_clips(google, job):
    _connect(google)
    clips = app_module.jobs[JOB]["result"]["clips"]
    clips[0]["predicted_score"] = 10
    clips.append({**clips[0], "predicted_score": 90})
    yt.save_auto_settings(True, 1, "unlisted", 0)
    asyncio.run(app_module._youtube_auto_upload(JOB, yt.auto_settings()))
    assert list(yt.posted(str(job))) == ["1"]
    assert "uploaded (unlisted)" in app_module.jobs[JOB]["logs"][-1]


def test_disconnect_forgets_tokens_keeps_settings(google):
    _connect(google)
    yt.save_auto_settings(True, 2, "private", 24)
    st = _call("POST", "/api/youtube/disconnect").json()
    assert not st["connected"] and st["auto"]["top_n"] == 2
    assert any(str(r.url).startswith(yt.REVOKE_URL) for r in google.requests)


