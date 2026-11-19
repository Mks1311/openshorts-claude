"""Direct YouTube Shorts publishing (YouTube Data API v3), self-host only.

The operator connects one channel with OAuth (YOUTUBE_CLIENT_ID /
YOUTUBE_CLIENT_SECRET); tokens and the auto-upload settings live in
``output/.youtube.json``, which no sweep deletes. What was posted is recorded
per job in ``<job>/.youtube.json`` ({clip index: video}).

Every Google call here is blocking (httpx.Client): callers in the API run them
in a thread. Tests swap ``_transport`` for an httpx.MockTransport.
"""
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from jsonfile import write_json_atomic

OUTPUT_DIR = "output"
STATE_FILE = os.path.join(OUTPUT_DIR, ".youtube.json")
SIDECAR = ".youtube.json"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
PRIVACY = ("private", "unlisted", "public")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
# Testing-mode OAuth apps get refresh tokens that die after 7 days.
TESTING_TOKEN_DAYS = 7
AUTO_DEFAULTS = {"enabled": False, "top_n": 3, "privacy": "private", "spacing_hours": 0}

_transport = None  # tests: httpx.MockTransport
_lock = threading.Lock()
_pending = {}  # OAuth state -> (created_at, dashboard origin)


class YouTubeError(Exception):
    status = 502


class NotConnected(YouTubeError):
    status = 409


class ReconnectNeeded(YouTubeError):
    status = 409


class QuotaExceeded(YouTubeError):
    status = 429


def _client():
    return httpx.Client(transport=_transport, timeout=httpx.Timeout(30.0, write=600.0))


def client_config():
    cid, secret = os.environ.get("YOUTUBE_CLIENT_ID", ""), os.environ.get("YOUTUBE_CLIENT_SECRET", "")
    redirect = os.environ.get("YOUTUBE_REDIRECT_URI", "http://localhost:8000/api/youtube/callback")
    return (cid, secret, redirect) if cid and secret else None


def _read():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    os.makedirs(os.path.dirname(os.path.abspath(STATE_FILE)), exist_ok=True)
    write_json_atomic(STATE_FILE, data)
    os.chmod(STATE_FILE, 0o600)  # holds a refresh token


def _google_error(resp):
    """(reason, message) from a Google error answer."""
    try:
        err = resp.json().get("error")
    except ValueError:
        return "", resp.text[:300]
    if isinstance(err, str):  # OAuth endpoints: {"error": "invalid_grant", ...}
        return err, str(resp.json().get("error_description") or err)[:300]
    if not isinstance(err, dict):
        return "", resp.text[:300]
    reasons = [e.get("reason") for e in err.get("errors") or [] if isinstance(e, dict)]
    return (reasons[0] if reasons else str(err.get("status") or "")), str(err.get("message") or "")[:300]


# ---------------------------------------------------------------- OAuth


def auth_url(origin=None):
    cfg = client_config()
    if cfg is None:
        raise NotConnected("Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env first.")
    now = time.time()
    for k in [k for k, (at, _) in _pending.items() if now - at > 600]:
        _pending.pop(k, None)
    state = secrets.token_urlsafe(24)
    _pending[state] = (now, origin)
    return AUTH_URL + "?" + urlencode({
        "client_id": cfg[0], "redirect_uri": cfg[2], "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": state,
    })


def finish_auth(code, state):
    """Exchange the callback's code. Returns the dashboard origin to go back to."""
    started = _pending.pop(state or "", None)
    if started is None or time.time() - started[0] > 600:
        raise YouTubeError("This sign-in link expired or was already used: connect again.")
    cid, secret, redirect = client_config() or ("", "", "")
    with _client() as c:
        resp = c.post(TOKEN_URL, data={"code": code, "client_id": cid, "client_secret": secret,
                                       "redirect_uri": redirect, "grant_type": "authorization_code"})
        if resp.status_code != 200:
            raise YouTubeError(f"Google refused the sign-in: {_google_error(resp)[1]}")
        tok = resp.json()
        if not tok.get("refresh_token"):
            raise YouTubeError("Google sent no refresh token: remove the app's access in your Google account and connect again.")
        channel = _channel(c, tok["access_token"])
    with _lock:
        data = _read()
        data["tokens"] = {"access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
                          "expires_at": time.time() + float(tok.get("expires_in") or 3600)}
        data["connected_at"] = time.time()
        data["channel"] = channel
        data.pop("reconnect_needed", None)
        _write(data)
    return started[1]


def _channel(c, token):
    """{id, title} of the signed-in channel, or None: channels.list needs a
    read scope the upload-only grant may lack; the first upload fills it in."""
    try:
        r = c.get(CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                  headers={"Authorization": f"Bearer {token}"})
        item = (r.json().get("items") or [None])[0] if r.status_code == 200 else None
        return {"id": item["id"], "title": item["snippet"]["title"]} if item else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def access_token(c, force=False):
    """A live access token, refreshed when it is (about to be) expired."""
    with _lock:
        data = _read()
        tok = data.get("tokens")
        if not tok:
            raise NotConnected("YouTube is not connected: connect it in Settings.")
        if data.get("reconnect_needed"):
            raise ReconnectNeeded(_reconnect_text())
        if not force and float(tok.get("expires_at") or 0) > time.time() + 60:
            return tok["access_token"]
        cid, secret, _ = client_config() or ("", "", "")
        resp = c.post(TOKEN_URL, data={"client_id": cid, "client_secret": secret,
                                       "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        if resp.status_code != 200:
            reason, msg = _google_error(resp)
            if reason in ("invalid_grant", "unauthorized_client", "invalid_client"):
                data["reconnect_needed"] = True
                _write(data)
                raise ReconnectNeeded(_reconnect_text())
            raise YouTubeError(f"Could not refresh the YouTube token: {msg}")
        new = resp.json()
        tok["access_token"] = new["access_token"]
        tok["expires_at"] = time.time() + float(new.get("expires_in") or 3600)
        _write(data)
        return tok["access_token"]


def _reconnect_text():
    return ("YouTube access expired (apps in Google's Testing mode must reconnect every "
            f"{TESTING_TOKEN_DAYS} days): reconnect it in Settings.")


def status():
    """What Settings shows. Refreshes an expired access token to find out
    whether the refresh token still works."""
    data = _read()
    out = {"configured": client_config() is not None, "connected": bool(data.get("tokens")),
           "channel": data.get("channel"), "connected_at": data.get("connected_at"),
           "reconnect_needed": bool(data.get("reconnect_needed")), "error": None,
           "auto": auto_settings(data)}
    if out["connected"] and not out["reconnect_needed"]:
        try:
            with _client() as c:
                access_token(c)
        except ReconnectNeeded:
            out["reconnect_needed"] = True
        except (YouTubeError, httpx.HTTPError) as e:
            out["error"] = str(e)[:300]
    return out


def disconnect():
    with _lock:
        data = _read()
        tok = data.pop("tokens", None)
        for k in ("channel", "connected_at", "reconnect_needed"):
            data.pop(k, None)
        _write(data)
    if tok:
        try:
            with _client() as c:
                c.post(REVOKE_URL, data={"token": tok.get("refresh_token") or ""})
        except httpx.HTTPError:
            pass  # forgotten here either way; Google drops unused grants later


# ---------------------------------------------------------------- settings


def auto_settings(data=None):
    saved = (data if data is not None else _read()).get("auto")
    saved = saved if isinstance(saved, dict) else {}
    try:
        return {"enabled": saved.get("enabled") is True,
                "top_n": min(20, max(1, int(saved.get("top_n", 3)))),
                "privacy": saved["privacy"] if saved.get("privacy") in PRIVACY else "private",
                "spacing_hours": min(168, max(0, int(saved.get("spacing_hours", 0))))}
    except (TypeError, ValueError, OverflowError):
        return dict(AUTO_DEFAULTS)


def save_auto_settings(enabled, top_n, privacy, spacing_hours):
    if privacy not in PRIVACY or not 1 <= top_n <= 20 or not 0 <= spacing_hours <= 168:
        raise ValueError("privacy is private|unlisted|public, top_n 1-20, spacing_hours 0-168")
    with _lock:
        data = _read()
        data["auto"] = {"enabled": bool(enabled), "top_n": int(top_n), "privacy": privacy,
                        "spacing_hours": int(spacing_hours)}
        _write(data)


# ---------------------------------------------------------------- upload


def _clean(text, limit):
    """YouTube refuses < and > in titles and descriptions."""
    return re.sub(r"[<>]", "", str(text or "")).strip()[:limit]


def parse_publish_at(value):
    """ISO time -> RFC 3339 UTC string, in the future. ValueError otherwise."""
    when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if when.tzinfo is None:
        raise ValueError("publish_at needs a timezone")
    if when <= datetime.now(timezone.utc) + timedelta(minutes=1):
        raise ValueError("publish_at must be in the future")
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def video_body(title, description, privacy, publish_at=None, category_id="22"):
    title = _clean(title, 100) or "Short"
    description = _clean(description, 4900)
    if "#shorts" not in (title + " " + description).lower():
        description = (description + "\n\n#Shorts").strip()
    status = {"privacyStatus": "private" if publish_at else privacy}
    if publish_at:
        status["publishAt"] = publish_at  # YouTube publishes it (public) at that time
    return {"snippet": {"title": title, "description": description, "categoryId": category_id},
            "status": status}


def _raise_for(resp):
    reason, msg = _google_error(resp)
    if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
        raise QuotaExceeded("The YouTube API quota for today is used up (about 6 uploads a day on the "
                            "default 10,000 units): it resets at midnight Pacific time.")
    if reason == "uploadLimitExceeded":
        raise QuotaExceeded("This channel hit YouTube's daily upload limit: try again tomorrow.")
    raise YouTubeError(f"YouTube refused the upload ({resp.status_code}): {msg or reason}")


def _chunks(path, size=1 << 20):
    with open(path, "rb") as f:
        while chunk := f.read(size):
            yield chunk


def upload(path, body):
    """Resumable videos.insert of ``path``. Returns the video resource.

    shortcut: the file goes in one PUT and a broken connection restarts the
    upload; resume from the received offset if clips ever get large."""
    size = os.path.getsize(path)
    with _client() as c:
        for attempt in (0, 1):
            token = access_token(c, force=attempt == 1)
            init = c.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                          headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
                                   "X-Upload-Content-Length": str(size)}, json=body)
            if init.status_code != 401:
                break
        if init.status_code != 200 or not init.headers.get("location"):
            _raise_for(init)
        put = c.put(init.headers["location"], content=_chunks(path),
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4",
                             "Content-Length": str(size)})
        if put.status_code not in (200, 201):
            _raise_for(put)
        video = put.json()
    snippet = video.get("snippet") or {}
    if snippet.get("channelId"):
        with _lock:
            data = _read()
            if data.get("tokens"):
                data["channel"] = {"id": snippet["channelId"], "title": snippet.get("channelTitle") or ""}
                _write(data)
    return video


def posted(job_dir):
    """{clip index (str): record} of a job's sidecar. Data from disk: only
    well-formed video ids survive, and the URL is rebuilt from the id."""
    try:
        with open(os.path.join(job_dir, SIDECAR)) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for k, rec in (raw.items() if isinstance(raw, dict) else []):
        vid = rec.get("video_id") if isinstance(rec, dict) else None
        if not (str(k).isdigit() and isinstance(vid, str) and VIDEO_ID_RE.match(vid)):
            continue
        out[str(k)] = {"video_id": vid, "url": f"https://youtube.com/shorts/{vid}",
                       "privacy": rec.get("privacy") if rec.get("privacy") in PRIVACY else None,
                       "publish_at": rec.get("publish_at") if isinstance(rec.get("publish_at"), str) else None,
                       "at": rec.get("at") if isinstance(rec.get("at"), (int, float)) else None}
    return out


def publish_clip(job_dir, clip_index, path, title, description, privacy, publish_at=None):
    """Upload one clip and record it in the job's sidecar. Returns the record."""
    body = video_body(title, description, privacy, publish_at)
    video = upload(path, body)
    vid = video.get("id")
    if not isinstance(vid, str) or not VIDEO_ID_RE.match(vid):
        raise YouTubeError("YouTube answered without a video id.")
    got = (video.get("status") or {}).get("privacyStatus")
    rec = {"video_id": vid, "privacy": got if got in PRIVACY else body["status"]["privacyStatus"],
           "requested_privacy": privacy, "publish_at": publish_at, "at": time.time(),
           "title": body["snippet"]["title"]}
    with _lock:
        side = os.path.join(job_dir, SIDECAR)
        try:
            with open(side) as f:
                records = json.load(f)
            records = records if isinstance(records, dict) else {}
        except (OSError, ValueError):
            records = {}
        records[str(clip_index)] = rec
        write_json_atomic(side, records)
    return {**posted(job_dir)[str(clip_index)], "requested_privacy": privacy}


def next_quota_reset(now=None):
    """Seconds until the API quota resets (midnight Pacific time), + 5 min."""
    pt = ZoneInfo("America/Los_Angeles")
    now = now or datetime.now(pt)
    midnight = (now.astimezone(pt) + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return (midnight - now).total_seconds()
