# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

shorts maxxing (forked from OpenShorts; module, env and repo names still say openshorts) is a self-hosted vertical video generator that turns long YouTube videos or local uploads into short 9:16 clips for TikTok, Instagram Reels and YouTube Shorts. Jobs run one at a time from a queue (`MAX_CONCURRENT_JOBS=1`). Every AI step runs on Claude through the headless `claude` CLI (`claude_cli.py`, via `llm_backend.py`) on the signed-in Claude subscription: no API key. Each call belongs to a task (`claude_cli.TASKS`) whose model is picked in Settings, and its tokens are recorded per job and per task. Prompts and answer schemas live in `llm_prompts.py`. The dashboard is the only client: there is no MCP server, CLI, webhook or agent upload API (they were removed; git history has them).

## Development Commands

```bash
docker compose up --build        # full stack: backend :8000 (FastAPI), frontend :5175 (Vite proxies the API)

cd dashboard && npm install
npm run dev                      # dev server with HMR (:5173)
npm run build                    # production build (CI runs this)
npm run lint                     # ESLint, --max-warnings 0

pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
pytest tests/ -v                 # CI runs this without the heavy ML stack
```

## Pipeline

Ingest (yt-dlp or upload) → transcription (word timestamps) → scene detection → Claude picks moments (15-60 s) → FFmpeg cut → vertical reframing → effects/subtitles → hook overlay → optional ElevenLabs dubbing → optional S3 backup → optional social posting via Upload-Post.

## Key Files

- `main.py`: core processing (transcription, scene detection, clip extraction, reframing). Runs as a child process per job.
- `app.py`: FastAPI server, job queue, REST endpoints, deploy handover/drain.
- `clip_selection.py`, `layout_picker.py`, `reframe_v2.py`, `split_layout.py`, `screencast_layout.py`, `camera_inset.py`: moment picking and layouts.
- `editor.py` (Claude edit plans → FFmpeg effects), `pro_mode.py` (talking-head re-edit), `hooks.py` (hook overlays), `subtitles.py` (SRT/ASS, burning), `translate.py` (ElevenLabs dubbing), `thumbnail.py`.
- `dashboard/src/App.jsx`: the dashboard shell (Clips, Queue, YouTube Studio, Settings); `components/QueuePage.jsx` + `lib/jobs.js` (the `/api/jobs` feed), `components/LogPanel.jsx`, `components/ModelSettings.jsx` (models and usage); `lib/serverConfig.js` reads `/api/config`. Remotion previews and exports render in the browser (`lib/renderInBrowser.js`).

## Subsystem reference (read before touching)

- Layout picker, reframing modes, hook grounding → `docs/architecture/layouts-and-reframing.md`
- Clip count, silent-footage fallback, Claude CLI → `docs/architecture/clip-selection.md`
- Models per task, token usage records → `docs/architecture/models-and-usage.md`
- Thumbnail Studio → `docs/architecture/thumbnails.md`
- Pro mode (talking-head re-edit, per-clip switch) → `docs/architecture/pro-mode.md`
- Concurrency/VRAM, auto-retry, deploy handover and drain → `docs/architecture/jobs-and-deploys.md`
- Download routing, yt-dlp clients → `docs/architecture/proxy-routing.md`

## Hard rules

**Layouts and rendering**
- `layout_picker` sends 12 frames at 1024px and asks for a closed choice, never the video and never a measurement.
- `layout_picker.apply()` only adds options: an explicit user choice is never turned off by the model.
- A source that is already vertical skips the picker and classifier and renders TRACK.
- SPLIT captions need ASS with `{\an5}` per word event on the seam; SRT cannot do per-stretch alignment. Preserve `layout_ranges` through recut (`layout_ranges.remap`).
- Mouth activity is normalised per speaker before comparing (`normalise_activity`).
- The reframe render runs in one GPU pass (`gpu_render` + `osrender/`) built from the scenes' own filtergraphs: a layout graph that `gpu_render.layers_from_graph` cannot read silently renders through ffmpeg, so keep new layouts to crop/scale/gblur/overlay/vstack/pad and run `tests/test_gpu_render_equivalence.py` on a GPU. After editing `osrender/src/kernels.cu`, regenerate `kernels.ptx` (see `osrender/README.md`).
- Pro mode (`pro_mode.py`) renders frame by frame on the CPU: always through `pro_mode.run` (own process, host slot), never inside the API or a job thread, and `PRO_MODE=auto` stays opt-in. The pro file is a `recut_` with its `recipe`; never a new filename prefix.
- `/videos` and `/thumbnails` serve only what `media_auth.is_servable` allows: the job directories also hold manifests, transcripts and checkpoints.
- The free-plan watermark (`watermarked.py`, `main.mark_delivery`, `wm_` copies) is dormant: nothing sets `WATERMARK` any more, but the `wm_` naming still runs through recut and the clip editor.

**Clip selection**
- The scoring pass ranks every window (global top N), never "choose up to K per batch". The `min_clips` floor is enforced in code.
- No stage sends a video file to the model: Claude gets timestamped JPEG frames (`frame_sampler.jpegs_every`). `get_visual_clips` watches near-equal stretches of at most 100 frames (`vision_segments`).
- Every `claude -p` call is isolated (own system prompt, no tools/settings/MCP, scratch cwd) to keep it near 1.5k input tokens. Tests never reach the real CLI (`CLAUDE_BIN` points nowhere in `tests/conftest.py`).
- Every Claude call passes its `task` (a key of `claude_cli.TASKS`): that picks the model and labels its usage. A new kind of call adds a task. Model names are validated by `claude_cli.valid_model` wherever they come from. Settings and usage live in dot files of `output/` (`.models.json`, `.usage.jsonl`), which no sweep deletes; tests redirect them to a temp dir.

**Jobs, GPU, deploys**
- Call `transcribe_backends.release_models()` after any ASR (job processes and in-API transcriptions alike). Size `MAX_CONCURRENT_JOBS` by free VRAM first.
- `DRAIN_TIMEOUT_SECONDS` must stay below the deployment's stop grace period. The image must ship `curl` for the platform health check.
- A drain-cancelled job keeps its manifest and is resumed, never marked failed or completed.
- The disk sweeps and size caps delete only what `_job_is_active` / `_upload_is_live` call dead: a submission counts as live from its `_begin_job_dir` (source still streaming in).
- Whatever is read back from a job directory (recipes, metadata, sidecars, subtitles, log lines) is data: numbers as numbers, text as text, never pasted into an ffmpeg filter graph, a path or markup. A filter string that is not a plain chain of `editor._ALLOWED_FILTERS` is not run.
- Pipeline subprocesses get `app.child_env()`: the server's API keys (`CHILD_ENV_DENYLIST`) never reach yt-dlp or ffmpeg.

**Download proxies**
- Route order is direct → static (`STATIC_PROXY_URLS`) → `PROXY_URL`. Never use `PROXY_URL` for non-YouTube URLs.
- The probe also carries `YOUTUBE_COOKIES`, like the download does; first attempt with cookies, second anonymous, on probe and download alike.
- Keep the explicit `yt_clients.py` client list; never put `player_skip: webpage` back. `noplaylist` always; non-video URLs refused by an allowlist.
- Every yt-dlp extraction that sends `YOUTUBE_COOKIES` takes its cookie file from a `yt_account.Session` (one of the configured accounts, `YOUTUBE_COOKIES_2`.. add more) and runs inside its `request()`, and none is sent while `yt_account.cooling_down()`: each account's budget is shared by every probe and download. A failure that is only the account limit is retried after the cooldown (`YT_ACCOUNT_LIMITED`), never reported as a broken video. An age-restricted refusal moves the video to another account (`Session.age_gated`), never to the proxy.
- A YouTube video is fetched once per `SOURCE_CACHE_HOURS` (`source_cache.py`: source, transcript and probe by video id). Only whole sources over a free route go in (never ranged, never the `PROXY_URL` 720p); a new dot entry of `output/` that holds data stays out of `app._output_jobs_size`.
- `DOWNLOAD_SKIP_STATICS` only when the probe saw every static bot-checked. Never keep `PROXY_URL` in a local `.env`.
- yt-dlp and server-side scrapes run inside `ssrf_guard.guarded()`: redirects and DNS answers are checked at connect time, not only the submitted URL.

**Public repo**
- This repository is public: no hosts, IPs, server paths, private metrics or personal data in code, comments, docs or commit messages.

## CI: a green pipeline closes the task, not the push

- Before pushing, run locally what CI runs: `pytest tests/ -v` and `npm run build` (+ `npm run lint`) in `dashboard/`.
- After every `git push`, wait for the commit's workflow and confirm it is green: `ci-wait` if available, or `gh run watch $(gh run list -c $(git rev-parse HEAD) -L1 --json databaseId -q ".[0].databaseId") --exit-status`.
- If it is red: fix, push, check again. Never report the task as done with a red CI.
- Every push to `main` redeploys the API; the dashboard app redeploys only when the push changes `dashboard/`. Batch small commits (tests, docs) with the next real change when possible.
