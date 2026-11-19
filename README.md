# shorts maxxing

Turn long videos into vertical shorts on your own machine. Paste a YouTube link
or drop a file, line up as many as you want, and come back to captioned 9:16
clips ready for TikTok, Instagram Reels and YouTube Shorts.

Every AI step runs on **Claude** through the `claude` CLI signed in on your
machine, on your Claude subscription. There is no API key to buy and no video
leaves your box: Claude only sees the transcript and still frames.

<p align="center">
  <img src="screenshots/split-before-after.gif" width="600" alt="a landscape interview reframed into a vertical split clip">
</p>

## What it does

- **Clips** — Claude reads the transcript, ranks every stretch and picks the
  15–60 s moments that stand on their own. Each clip is reframed to 9:16 (face
  tracking, split screen for two speakers, screen + camera for screencasts),
  captioned word by word, and gets a hook title. Clip only part of a long
  video or livestream by giving a start and end time.
- **Queue** — add videos while one is running; they run one after another. The
  Queue page shows the running job's stage, progress and time, the waiting
  ones in order with their ETA, and the finished ones with their clips, how
  long they took, or why they failed. Each job shows its link, thumbnail, clip
  range and the options it was started with. Stop a running job or remove a
  waiting one at any time; **delete** a finished one to drop it and its files.
  A stopped job's files are deleted right away, and whatever failed jobs left
  is cleared when the server starts.
- **Edit** — per clip: trim, re-frame, re-caption (styles, position), change
  the hook, auto edit (zooms and cuts planned by Claude), pro mode for
  talking heads, dub into another language (ElevenLabs).
- **YouTube Studio** — titles, thumbnail text on a frame of the video, and a
  description with chapters, for the long video itself.
- **Publish** — post or schedule clips to TikTok, Instagram and YouTube through
  [Upload-Post](https://upload-post.com) (optional, your own key).
- **Models and usage** — pick the Claude model for each step (moment picking,
  layout, hooks, edits, Studio...) in Settings. Every Claude call is recorded:
  each job shows its tokens per step, and Settings shows the totals per day
  and per step.
- **Agents** — the same pipeline as an MCP server, a REST API with completion
  webhooks, a CLI and an agent skill.

## Requirements

- macOS or Linux (Windows through WSL2)
- Python 3.11, Node 20+, `ffmpeg` on the `PATH`
- The [Claude Code CLI](https://docs.claude.com/en/docs/claude-code) signed in
  once with your Claude account (`claude`, then `/login`), or
  `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`
- Optional: an NVIDIA GPU (see [GPU](#gpu-optional-nvidia)), an Upload-Post key
  for posting, an ElevenLabs key for dubbing

## Run it

```bash
git clone https://github.com/Mks1311/openshorts-claude.git shorts-maxxing
cd shorts-maxxing
cp .env.example .env            # everything in it is optional
```

Backend (API on `:8000`):

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

Dashboard (on `:5173`, proxies the API):

```bash
cd dashboard && npm install
VITE_PROXY_TARGET=http://localhost:8000 npm run dev
```

Open <http://localhost:5173>, paste a link, press **generate clips**. While a
job runs the button reads **add to queue**.

`docker compose up --build` runs the same stack (dashboard on `:5175`), but
the image does not ship the `claude` CLI yet, so the AI steps fail in it; run
it on the host as above for now.

Restarting the backend while a job runs is safe: the job resumes from the
start of its run when the server is back. Finished jobs are kept 24 hours
(`JOB_RETENTION_SECONDS`).

## Settings

| Section | What it does |
|---|---|
| **Models** | The Claude model for each step: pick moments, pick moments in silent videos, layout, hooks, burned-in subtitle check, auto edit, YouTube Studio, pro mode. Every step defaults to `claude-sonnet-5-5`; `haiku` is fastest and lightest on your plan's usage limit, `opus` the strongest. A change applies from the next call, running jobs included. Saved in `output/.models.json`. |
| **Usage** | Tokens and calls of the last day, 7 or 30 days, per step and per day (`output/.usage.jsonl`). Each job's own breakdown is in its view and on the Queue page. |
| **Publishing** | Your Upload-Post key and the profile to post as. |
| **Dubbing** | Your ElevenLabs key for the **dub voice** button on a clip. |
| **Agents** | How to connect Claude Code, Cursor or another MCP client to this server. |

Keys you type in the dashboard stay in your browser (lightly obfuscated in
`localStorage`), never on the server.

## How a clip is made

1. **Ingest** — `yt-dlp` for links (cached per video for `SOURCE_CACHE_HOURS`), or the uploaded file.
2. **Transcribe** — faster-whisper (Parakeet on an NVIDIA GPU) with word timestamps.
3. **Scenes** — PySceneDetect finds the camera cuts.
4. **Pick moments** — Claude scores every transcript window, then details the best ones (15–60 s). A video with no speech is picked from frames instead.
5. **Cut and reframe** — FFmpeg cuts; faces are tracked into 9:16, with split or screen layouts when the video needs them.
6. **Captions and hook** — word-timed ASS captions and a hook title, burned in.
7. **Deliver** — the clips appear in the dashboard one by one; optional S3 backup and posting.

The job log shows each step, and every Claude call as one line with its step,
model, tokens and time. Filter it to key steps, Claude calls or problems,
search it, copy or download it.

## Agents and automation

Interactive API docs live at <http://localhost:8000/docs>.

**MCP** — Claude Code, Cursor or any MCP client can clip, check and publish:

```bash
claude mcp add --transport http shorts-maxxing http://localhost:8000/mcp
# or without the web server running, over stdio:
claude mcp add shorts-maxxing -- python mcp_stdio.py
```

Tools: `process_video`, `create_upload`, `get_job_status`, `list_clips`,
`add_subtitles`, `recut_clip`, `publish_clip`.

**REST + webhooks** — `POST /api/process` with `{"url": "...", "acknowledged": true}`
queues a job; add `webhook_url` (and `webhook_secret`) to get one `POST` when
it ends, signed `X-OpenShorts-Signature: sha256=<hmac>` when a secret is set.
`GET /api/jobs` lists every job with its status and token usage.

**CLI** — `cli/` (`OPENSHORTS_API_URL=http://localhost:8000 openshorts process <url> --wait`).
**Agent skill** — `skills/openshorts/`. **n8n** — `examples/n8n/`.

## GPU (optional, NVIDIA)

With an NVIDIA card, transcription and encoding move to the GPU and an
8-minute video clips in about a minute instead of 5 to 8. In `.env`:

```
WHISPER_MODEL=large-v3-turbo
WHISPER_DEVICE=cuda
WHISPER_COMPUTE=float16
FFMPEG_ENCODER=auto           # h264_nvenc when it works, x264 otherwise
TRANSCRIBE_BACKEND=parakeet   # optional, ~2x faster for 25 European languages
```

`MAX_CONCURRENT_JOBS` (default 1) runs that many jobs at once; raise it only
when the card has the VRAM for it (about 4–6 GB per job while it transcribes).

## Useful environment variables

| Variable | Default | What it does |
|---|---|---|
| `MAX_CONCURRENT_JOBS` | `1` | Jobs that run at the same time; the rest wait in the queue. |
| `JOB_RETENTION_SECONDS` | `86400` | How long finished jobs and their clips are kept. |
| `LLM_MODEL` | — | Model for the text steps when Settings picks none (else `claude-sonnet-5-5`). |
| `LLM_TIMEOUT` | `600` | Seconds one Claude call may take. |
| `LLM_SCORE_BATCH` | `8` | Transcript windows per scoring call. |
| `CLAUDE_BIN` | `claude` | Path to the Claude CLI. |
| `AUTO_LAYOUT` | `0` | `1` lets Claude pick split / screen layouts on every job. |
| `WHISPER_MODEL` | `small` | Transcription model (`large-v3-turbo` on a GPU). |
| `YOUTUBE_COOKIES` | — | Netscape cookies for videos YouTube refuses anonymously. |
| `AWS_*` | — | Optional S3 backup of the clips. |

## Development

```bash
.venv/bin/pytest tests/ -v                       # backend tests (no real Claude calls)
cd dashboard && npm run lint && npm run build    # what CI runs for the dashboard
```

Architecture notes for each subsystem are in [`docs/architecture/`](docs/architecture/);
[`CLAUDE.md`](CLAUDE.md) lists the rules the code relies on.

## License

MIT (see [LICENSE](LICENSE)). shorts maxxing is built on
[OpenShorts](https://github.com/mutonby/openshorts); the `cloud/` directory
keeps its own license (see [NOTICE](NOTICE)).
