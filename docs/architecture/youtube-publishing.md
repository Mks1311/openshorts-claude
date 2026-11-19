# Direct YouTube publishing (`youtube_publish.py`)

Self-host only: every `/api/youtube/*` route is 404 when `BILLING_ENABLED`.
One operator connects one channel; Upload-Post stays as the other way to post.
Instagram and Facebook can follow the same shape (module + `output/.<name>.json`
+ per-job sidecar).

## State

- `output/.youtube.json` (mode 0600, no sweep deletes dot entries):
  `tokens` {access_token, refresh_token, expires_at}, `connected_at`,
  `channel` {id, title}, `reconnect_needed`, `auto` (auto-upload settings).
- `output/<job>/.youtube.json`: `{clip index: {video_id, privacy,
  requested_privacy, publish_at, at, title}}`. Read back through
  `youtube_publish.posted`, which keeps only well-formed ids and rebuilds the
  URL from the id (the file is data). `GET /api/status/{id}` returns it as
  `youtube`; the clip card shows "on youtube ↗".

## OAuth

`GET /api/youtube/connect` → Google (`access_type=offline`, `prompt=consent`,
scope `youtube.upload`, random `state` kept in memory for 10 minutes with the
dashboard origin from the Referer). `GET /api/youtube/callback` exchanges the
code and redirects to `<origin>/?youtube=connected` (the dashboard opens
Settings). `GET /api/youtube/status` refreshes an expired access token to learn
whether the refresh token still works; `invalid_grant` sets
`reconnect_needed` (Testing-mode apps: every 7 days). `POST
/api/youtube/disconnect` revokes and forgets the tokens, keeps `auto`.

The upload-only scope cannot read the channel, so `channels.list` is best
effort; the first upload's answer (`snippet.channelTitle`) fills it in.

Credentials: `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`,
`YOUTUBE_REDIRECT_URI` (default `http://localhost:8000/api/youtube/callback`).

## Upload

`POST /api/youtube/upload {job_id, clip_index, title?, description?, privacy,
publish_at?}`: resumable `videos.insert` (session POST, then one PUT of the
file). Title ≤ 100 chars, `<` `>` removed, `#Shorts` appended to the
description when neither field has it, `categoryId` 22. With `publish_at`
(ISO with timezone, in the future) the video goes up private with
`status.publishAt`. Google's own answer sets the recorded `privacy`: an
unaudited API project's uploads come back private whatever was asked.

Errors: not connected / reconnect needed → 409; `quotaExceeded`,
`uploadLimitExceeded` → 429 with the reset time (midnight Pacific; ≈1,600
units per upload, 10,000 a day); other Google refusals → 502 with Google's
message. A 401 on the session POST forces one token refresh and retries.

## Auto-upload

`PUT /api/youtube/auto {enabled, top_n 1-20, privacy, spacing_hours 0-168}`.
When a job completes (`app._finish_job`), `_youtube_auto_upload` uploads its
top `top_n` clips by `predicted_score` (`main.clip_render_order`), skipping
clips already posted. With `public` and a spacing, clip k is scheduled at
start + k × spacing. Out of quota, it sleeps until the reset and goes on;
not connected or expired stops it; every step is a line in the job's log.
The wait lives in the API process: a restart drops the rest of the batch.

Tests (`tests/test_youtube_publish.py`) replace `youtube_publish._transport`
with an `httpx.MockTransport` and `STATE_FILE` with a temp file.
