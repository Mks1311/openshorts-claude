# Thumbnail Studio (`thumbnail.py`, `/api/thumbnail/*`)

## Titles

Titles come from the transcript plus 10 frames at 1024px, never the whole video
(an hour of video is ~1M tokens for a text task). Two calls: a 25-title
brainstorm across fixed styles, then a critic that scores, dedupes by angle and
returns 10, each paired with a 1-4 word `thumbnail_text` that complements the
title rather than repeating it. Rules baked in: payoff inside 50 characters
(phones cut there), keyword in the first 3 words, same language as the
transcript.

Every Studio call is the Claude task `studio` (default `claude-sonnet-5-5`, picked in
Settings → Models): a small model is fine for a closed-choice layout pick and
visibly worse at creative titles. There is no image model.

## Images

Claude cannot draw. Thumbnails are `count` **different concepts** built on one
picture: the uploaded face photo, else the frame picked below, else the uploaded
background. One text call designs each concept (hook text, which side it sits
on, its colour) and PIL sets that text in Anton with a black stroke, so accents
and spelling are never wrong (`burn_text=false` returns the picture as is).
Every output is cover-cropped to 1280x720 and saved under YouTube's 2 MB limit.

`GET /api/thumbnail/frames/{session}` scores sampled frames by face area and
sharpness (MediaPipe + Laplacian), keeps them spread across the runtime, and the
dashboard offers them as the person reference so the thumbnail shows the creator;
an uploaded face photo still wins.

## Sources and limits

A link given to `/api/thumbnail/upload` or `/analyze` goes through
`_validate_source_url` like `/api/process`; the download then runs in a
worker thread.

Refinement and description calls are bounded: message/title length, the last
`THUMBNAIL_CONVERSATION_MAX_TURNS` chat turns sent back and refinements per
session. The limits are constants in `app.py`, env-overridable.

Transcribing in the API process (thumbnails, `/api/subtitle` on a dubbed clip)
must end with `transcribe_backends.release_models()`, or the ASR model stays
resident in uvicorn (see `jobs-and-deploys.md`).
