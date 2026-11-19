import os
import llm_backend
import re
import sys
import uuid
import subprocess
import threading
import json
import shutil
import glob
import time
import zipfile
import math
import functools
import heapq
import asyncio
import signal
import socket
from dotenv import load_dotenv
from typing import Any, Dict, Optional, List
from contextlib import asynccontextmanager, suppress
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel
from s3_uploader import upload_job_artifacts
import recut
import layout_ranges
import watermarked
import media_auth
import yt_account
from jsonfile import write_json_atomic

load_dotenv()

# Constants
UPLOAD_DIR = "uploads"
OUTPUT_DIR = "output"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Configuration
MAX_FILE_SIZE_MB = 2048  # 2GB limit

# How TikTok receives our uploads. MEDIA_UPLOAD lands the video in the user's
# TikTok drafts so they finish the post inside TikTok's own editor; DIRECT_POST
# publishes straight to their feed, which is Upload-Post's default.
#
# Drafts are the safer default for an automated pipeline: nothing reaches an
# audience without the account owner seeing it first, and TikTok's own editor is
# where covers, sounds and hashtags actually get chosen. The UI must say so —
# a user who expects a published post and finds a draft will read it as a bug.
TIKTOK_POST_MODE = os.environ.get("TIKTOK_POST_MODE", "MEDIA_UPLOAD").strip()
# Ceiling for the working directory once it lives on a persistent volume: the
# age-based sweep alone can't stop a burst of long videos from filling the disk.
# 0 disables the cap.
OUTPUT_MAX_GB = int(os.environ.get("OUTPUT_MAX_GB", "25"))
# A job dir touched this recently is left alone by the size cap. Where the
# jobs still queued or running hold more than the cap by themselves, the cap
# purged every finished job at the next sweep: an edit made minutes after the
# clips were ready lost its files before its archive upload ended, and a
# project restored from the library was purged again while it was open.
OUTPUT_CAP_GRACE_SECONDS = int(os.environ.get("OUTPUT_CAP_GRACE_SECONDS", "1800"))
# Same idea for source uploads, which are the biggest single files on disk.
UPLOADS_MAX_GB = int(os.environ.get("UPLOADS_MAX_GB", "15"))
# Pre-flight quality gate: warn before processing a YouTube source below this
# height (0 disables). Only applies to URLs; uploads are whatever the user gave.
QUALITY_GATE_MIN_HEIGHT = int(os.environ.get("QUALITY_GATE_MIN_HEIGHT", "720"))
# Reject sources shorter than this before starting (0 disables). A 24s YouTube
# Short cannot yield 15-60s clips: the model returns nothing and the job dies
# with "no usable clips".
MIN_SOURCE_SECONDS = int(os.environ.get("MIN_SOURCE_SECONDS", "45"))
QUALITY_PROBE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quality_probe.py")

# Server credentials the pipeline subprocesses (main.py, quality_probe.py) never
# read. A child that runs yt-dlp and ffmpeg on user-supplied URLs and files has
# no business holding them; one exploit in that stack would hand them over.
CHILD_ENV_DENYLIST = frozenset({
    "UPLOAD_POST_API_KEY", "ELEVENLABS_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
})


def child_env(base=None):
    """A copy of the environment for a pipeline subprocess, minus the server
    secrets in CHILD_ENV_DENYLIST."""
    env = dict(os.environ if base is None else base)
    for name in CHILD_ENV_DENYLIST:
        env.pop(name, None)
    return env


DISABLE_YOUTUBE_URL = os.environ.get("DISABLE_YOUTUBE_URL", "false").lower() in ("1", "true", "yes")

# Every log line in this module is emoji-prefixed, and a Windows console is
# cp1252 by default. _recover_jobs_from_disk() prints one during startup, so
# without this the server dies before it ever listens:
#
#   UnicodeEncodeError: 'charmap' codec can't encode characters in position 0-1
#   ERROR:    Application startup failed. Exiting.
#
# subtitles._configure_stdio solved this for the transcription path; the server
# needs it too, and needs it before the first print.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Self-host runs one job at a time, so a queue of submissions finishes one after
# another; set it higher on a box with the VRAM for parallel jobs.
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "1"))

# Job/file retention (issue #46): 24h. The OUTPUT_MAX_GB / UPLOADS_MAX_GB caps
# below already bound the disk.
JOB_RETENTION_SECONDS = int(
    os.environ.get("JOB_RETENTION_SECONDS", "86400")
)
# The retained download of a URL job (--keep-original) is the one artifact that
# is a full copy of someone else's video rather than something we made, so it
# can be aged out ahead of the clips it produced. Defaults to the job clock,
# i.e. no change: dropping it earlier costs the clip editor, whose rerender,
# reframe, scenes and EDL endpoints all read that file and answer 409 once it
# is gone. Lower it only if you would rather lose in-session re-edits than
# keep the original around. Uploads are deliberately untouched: that file is
# the user's own content, which they attested to owning.
SOURCE_RETENTION_SECONDS = int(
    os.environ.get("SOURCE_RETENTION_SECONDS", str(JOB_RETENTION_SECONDS))
)


async def resolve_upload_post(request: Request, body_key: Optional[str] = None):
    """Resolve the Upload-Post key and the profile to post as.

    Returns ``(api_key, None)``: the key is the header, then the body key,
    then the env; the profile is the one the caller names.
    """
    header = request.headers.get("X-Upload-Post-Key")
    key = header or body_key or os.environ.get("UPLOAD_POST_API_KEY")
    return key, None


def resolve_post_profile(forced_profile: Optional[str], client_profile: Optional[str]) -> str:
    """The Upload-Post profile to act as, for posting/scheduling/analytics.

    The caller owns the Upload-Post account whose key resolved above, so it
    picks its own profile; none is a 400.
    """
    profile = forced_profile or client_profile
    if not profile:
        raise HTTPException(status_code=400, detail="Missing Upload-Post user profile")
    return profile


def plan_source_range(probed_minutes, source_range):
    """Validate the dashboard's "clip only this part" range against the source.

    ``source_range`` is ``(start_s, end_s)`` (either may be None for "from the
    start" / "to the end"). Returns ``{"start_seconds", "end_seconds"}``, or
    None when no range was asked or it covers the whole video (then the job is
    an ordinary whole-video one). Raises 400 for a range shorter than
    ``MIN_RANGE_SECONDS`` or one that starts past the end.
    """
    if not source_range:
        return None
    start_raw, end_raw = source_range
    duration = max(0.0, float(probed_minutes) * 60.0)
    try:
        start = float(start_raw) if start_raw not in (None, "") else 0.0
        end = float(end_raw) if end_raw not in (None, "") else duration
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="source_start / source_end must be seconds.")
    start = max(0.0, start)
    end = min(duration, end) if duration else end
    if start <= 1.0 and (not duration or end >= duration - 1.0):
        return None
    if end - start < MIN_RANGE_SECONDS:
        raise HTTPException(status_code=400, detail=(
            f"Pick at least {MIN_RANGE_SECONDS} seconds of the video to clip."))
    return {"start_seconds": round(start, 3), "end_seconds": round(end, 3)}


def _apply_source_cut(env, partial):
    """Put a partial job's cut in the main.py env (MAX_SOURCE_MINUTES, and
    SOURCE_START_SECONDS for a chosen range), or clear it.

    A chosen range is cut to its exact length, not the rounded-down minutes it
    was billed: the user paid for "from 2:10 to 7:40", not for 5 minutes.
    """
    env.pop("SOURCE_START_SECONDS", None)
    if not partial or not partial.get("processed_minutes"):
        env.pop("MAX_SOURCE_MINUTES", None)
        return
    if partial.get("start_seconds") is not None and partial.get("end_seconds") is not None:
        start, end = float(partial["start_seconds"]), float(partial["end_seconds"])
        env["MAX_SOURCE_MINUTES"] = f"{(end - start) / 60.0:.4f}"
        if start > 0:
            env["SOURCE_START_SECONDS"] = f"{start:.3f}"
    else:
        env["MAX_SOURCE_MINUTES"] = str(partial["processed_minutes"])


def _discard_rejected_source(input_path, job_output_dir):
    """Undo what /api/process wrote to disk for a job that was refused."""
    if input_path and os.path.exists(input_path):
        try:
            os.remove(input_path)
        except OSError:
            pass
    if job_output_dir:
        shutil.rmtree(job_output_dir, ignore_errors=True)
        _end_job_dir(os.path.basename(str(job_output_dir).rstrip(os.sep)))


def _parse_source_range(start, end):
    """``(start, end)`` from the request fields, or None when neither was sent."""
    if start in (None, "") and end in (None, ""):
        return None
    return (start, end)


# A stretch shorter than this cannot hold a few 15-60 s clips.
MIN_RANGE_SECONDS = 60


def _self_host_range(source_range):
    """The partial for a stretch picked in the dashboard, or None.

    No metering probe runs here, so the duration is unknown: an end time is
    required, and main.py's cut stops at the real end of the source.
    """
    if not source_range:
        return None
    if source_range[1] in (None, ""):
        raise HTTPException(status_code=400, detail="Give an end time for the part to clip.")
    # Unknown length: infinity keeps plan_source_range from clamping to it.
    chosen = plan_source_range(math.inf, source_range)
    if chosen is None:
        return None
    minutes = (chosen["end_seconds"] - chosen["start_seconds"]) / 60.0
    return {"processed_minutes": round(minutes, 2), "total_minutes": None, **chosen, "chosen": True}

# URL probes made for the dashboard's cost line, reused by /api/process when
# the user submits the same link: (owner, url) -> (monotonic, minutes).
_probe_cache: dict = {}


# job_id -> (owner id, monotonic start) for /api/process submissions that
# passed the job-limit check and are not in `jobs` yet. Entries end when the
# job is registered or refused; the age cap only covers a request that died
# without either.
_submissions: dict = {}


def _end_submission(job_id):
    _submissions.pop(str(job_id), None)


# job_id -> monotonic start for /api/process requests that created their job
# dir and are not in `jobs` yet. The source can take minutes to stream in, and
# the disk sweeps run meanwhile: a size cap over its limit deleted the half-
# written upload and the empty dir under the request. Entries end when the job
# is registered or refused; the age cap only covers a request that died.
_dirs_in_flight: dict = {}
_DIR_IN_FLIGHT_MAX_AGE = 3 * 3600


def _begin_job_dir(job_id):
    _dirs_in_flight[str(job_id)] = time.monotonic()


def _end_job_dir(job_id):
    _dirs_in_flight.pop(str(job_id), None)


def _job_dir_in_flight(job_id) -> bool:
    started = _dirs_in_flight.get(str(job_id))
    if started is None:
        return False
    if time.monotonic() - started > _DIR_IN_FLIGHT_MAX_AGE:
        _dirs_in_flight.pop(str(job_id), None)
        return False
    return True


# Application State
# PriorityQueue holds (priority, key, job_id). Every job enqueues at priority 2
# and the key is its submission time (``submitted_at``, which travels in the
# resume manifest, so a job enqueued again after a deploy keeps its place):
# plain FIFO.
job_queue = asyncio.PriorityQueue()
jobs: Dict[str, Dict] = {}
thumbnail_sessions: Dict[str, Dict] = {}
publish_jobs: Dict[str, Dict] = {}  # {publish_id: {status, result, error}}
# Semester to limit concurrency to MAX_CONCURRENT_JOBS
concurrency_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


# Queue position + ETA for /api/status. ``_admitting`` holds jobs already taken
# off the queue that wait for a free slot / GPU room: they go before anything
# still queued. Durations are a rolling sample of this instance's finished jobs.
_admitting: set = set()
_recent_job_seconds: list = []
DEFAULT_JOB_SECONDS = 240


def _record_job_duration(seconds):
    if seconds and seconds > 0:
        _recent_job_seconds.append(float(seconds))
        del _recent_job_seconds[:-30]


def queue_estimate(ahead: int, running: int, slots: int, typical_seconds: float) -> int:
    """Seconds until a job with ``ahead`` jobs in front of it starts.

    With a slot free it starts right away (a few seconds of admission);
    otherwise each full round of ``slots`` jobs ahead costs about one typical
    job, since the running ones are on average half done.
    """
    slots = max(1, int(slots))
    free = max(0, slots - int(running))
    if ahead < free:
        return 15
    waiting = ahead - free + 1
    rounds = waiting / slots
    return int(max(30, round(typical_seconds * (0.5 + rounds - 1 / slots))))


# Every change this module makes to the heap bumps this, so /api/status (polled
# every 2 s by each open tab) ranks a queue of thousands once per change, not
# once per poll.
_queue_version = 0
_rank_cache = {"key": None, "rank": {}}


def _queue_changed():
    global _queue_version
    _queue_version += 1


def _queue_ranks() -> dict:
    """``{job_id: (index, priority)}`` of the queued entries in dispatch order.
    Cached on the heap's identity, version and size, and for one second at
    most (a status flip nobody announced)."""
    heap = job_queue._queue
    key = (id(job_queue), _queue_version, len(heap), int(time.monotonic()))
    if _rank_cache["key"] != key:
        rank, i = {}, 0
        for prio, _k, jid in sorted(heap):
            if jid in rank or (jobs.get(jid) or {}).get('status') != 'queued':
                continue
            rank[jid] = (i, prio)
            i += 1
        _rank_cache.update(key=key, rank=rank)
    return _rank_cache["rank"]


def queue_snapshot(job_id):
    """``{position, ahead, eta_seconds, eta_is_minimum}`` for a queued job,
    else None. Position is in the real dispatch order (lanes and newest-first
    included); ``eta_is_minimum`` says later arrivals can still pass it."""
    job = jobs.get(job_id) or {}
    if job.get('status') != 'queued':
        return None
    try:
        ranks = _queue_ranks()
    except Exception:
        ranks = {}
    prio = None
    if job_id in _admitting:
        ahead = len([j for j in _admitting if j != job_id])
    elif job_id in ranks:
        idx, prio = ranks[job_id]
        ahead = len(_admitting) + idx
    else:
        return None
    sample = sorted(_recent_job_seconds)
    typical = sample[len(sample) // 2] if sample else DEFAULT_JOB_SECONDS
    return {
        "position": ahead + 1,
        "ahead": ahead,
        "eta_seconds": queue_estimate(ahead, len(_running_jobs), MAX_CONCURRENT_JOBS, typical),
        "eta_is_minimum": False,
    }


_last_arrival = 0.0


def _arrival_now() -> float:
    """Now, strictly after the last arrival handed out (equal keys would fall
    back to comparing job ids)."""
    global _last_arrival
    _last_arrival = max(time.time(), _last_arrival + 1e-6)
    return _last_arrival


def _job_arrival(job_id) -> float:
    """When the job was submitted (stamped on its record the first time)."""
    job = jobs.get(job_id)
    if job is None:
        return _arrival_now()
    t = job.get('submitted_at')
    if t is None:
        t = job['submitted_at'] = _arrival_now()
    return float(t)


def _entry_key(priority, job_id, old_key=None) -> float:
    """Heap key of ``job_id``: its submission time (FIFO). Without a record
    the old key's time stands."""
    if jobs.get(job_id) is not None:
        return _job_arrival(job_id)
    if old_key is not None:
        return abs(float(old_key))
    return _arrival_now()


def _enqueue_job(job_id: str, priority: int = 2):
    job_queue.put_nowait((priority, _entry_key(priority, job_id), job_id))
    _queue_changed()


def _manifest_names() -> list:
    """Job dirs holding a resume manifest. OUTPUT_DIR also has loose files (the
    instance marker, the queue watch state and lock, lock files): skip them
    instead of reading ``<file>/.resume.json``."""
    try:
        entries = os.listdir(OUTPUT_DIR)
    except FileNotFoundError:
        return []
    return [n for n in entries
            if not n.startswith(".") and os.path.isfile(_manifest_path(n))]


def _relocate_root_job_artifacts(job_id: str, job_output_dir: str) -> bool:
    """
    Backward-compat rescue:
    If main.py accidentally wrote metadata/clips into OUTPUT_DIR root (e.g. output/<jobid>_...),
    move them into output/<job_id>/ so the API can find and serve them.
    """
    try:
        os.makedirs(job_output_dir, exist_ok=True)
        root = OUTPUT_DIR
        pattern = os.path.join(root, f"{job_id}_*_metadata.json")
        meta_candidates = sorted(glob.glob(pattern), key=lambda p: os.path.getmtime(p), reverse=True)
        if not meta_candidates:
            return False

        # Move the newest metadata and its associated clips.
        metadata_path = meta_candidates[0]
        base_name = os.path.basename(metadata_path).replace("_metadata.json", "")

        # Move metadata
        dest_metadata = os.path.join(job_output_dir, os.path.basename(metadata_path))
        if os.path.abspath(metadata_path) != os.path.abspath(dest_metadata):
            shutil.move(metadata_path, dest_metadata)

        # Move any clips that match the same base_name into the job folder
        clip_pattern = os.path.join(root, f"{base_name}_clip_*.mp4")
        for clip_path in glob.glob(clip_pattern):
            dest_clip = os.path.join(job_output_dir, os.path.basename(clip_path))
            if os.path.abspath(clip_path) != os.path.abspath(dest_clip):
                shutil.move(clip_path, dest_clip)

        # Also move any temp_ clips that might remain
        temp_clip_pattern = os.path.join(root, f"temp_{base_name}_clip_*.mp4")
        for clip_path in glob.glob(temp_clip_pattern):
            dest_clip = os.path.join(job_output_dir, os.path.basename(clip_path))
            if os.path.abspath(clip_path) != os.path.abspath(dest_clip):
                shutil.move(clip_path, dest_clip)

        return True
    except Exception:
        return False

def _canonical_clip_file(output_dir, base_name, index):
    """The file to serve for clip ``index``, preferring a derived version.

    The pipeline writes the clean reframe as ``<base>_clip_<n>.mp4`` and any
    post-processing (auto-captions, /api/subtitle re-styles, and clip-editor
    recuts) as ``subtitled_<ts>_<clean>.mp4`` / ``recut_<ts>_<clean>.mp4``,
    keeping the original for re-styling. Every place that rebuilds the
    canonical name from disk — restore after a restart, the R2 upload, the
    download bundle — must therefore resolve to the newest derived file, or
    clips silently lose their captions (or their recut) on a redeploy.
    """
    clean = f"{base_name}_clip_{index + 1}.mp4"
    try:
        # subtitled_*_{clean} also matches subtitled_<ts>_recut_<ts>_{clean}
        # and subtitled_<ts>_hooked_<ts>_{clean}, i.e. captioned recuts and
        # captioned hooks; the bare recut_/hooked_/hook_ patterns cover
        # derivations that shipped uncaptioned (hook_ is the legacy manual-
        # hook prefix, kept so old jobs still resolve).
        # wm_*{clean} is the free plan's marked copy of any of those (or of
        # the clean file itself: the * matches nothing), written last.
        derived = (glob.glob(os.path.join(output_dir, f"subtitled_*_{clean}"))
                   + glob.glob(os.path.join(output_dir, f"recut_*_{clean}"))
                   + glob.glob(os.path.join(output_dir, f"hooked_*_{clean}"))
                   + glob.glob(os.path.join(output_dir, f"hook_{clean}"))
                   + glob.glob(os.path.join(output_dir, f"wm_*{clean}")))
    except Exception:
        derived = []
    if not derived:
        return clean
    # Highest timestamp wins — that's the most recent styling.
    return os.path.basename(max(derived, key=os.path.getmtime))


def _clips_actually_rendered(job_id, output_dir, base_name, clips):
    """Keep only the clips whose file is really on disk. Returns (kept, missing).

    The metadata JSON is written BEFORE any clip renders, and main.py's worker
    pool swallows a per-clip exception (it only prints "❌ Clip N failed") and
    still exits 0. So ``shorts`` is what Gemini promised, not what ffmpeg
    produced, and ``_canonical_clip_file`` happily returns the name of a file
    that was never written.

    Handing those back as delivered clips is what let a job that rendered
    NOTHING be marked completed: the minutes were committed, ClipsDelivered
    fired, ``archive_job`` skipped every missing file and uploaded the metadata
    alone — which, because it returns before writing the Project row, left the
    job unrestorable and invisible in history — and the dashboard offered clips
    whose only existence was a title, so "download all" answered 404 "No clip
    files found for this job" (ticket #4711, job 89d76bbc, 6-sep-2026).
    Measured over the R2 archive on 7-sep-2026: 195 of 1687 archived jobs held
    a metadata file and not one clip, across 184 users.

    Trust the disk, not the promise.
    """
    # main.py announces the file it actually finished for each clip
    # (CLIP_READY, printed after the whole reframe/watermark/hook/caption
    # chain). Prefer it over rebuilding the name: the marker is what the file
    # IS, _canonical_clip_file is a guess from the naming convention.
    ready_files = (jobs.get(job_id) or {}).get('ready_files') or {}
    kept = []
    for i, clip in enumerate(clips):
        clip_filename = (ready_files.get(i)
                         or _canonical_clip_file(output_dir, base_name, i))
        clip_path = os.path.join(output_dir, clip_filename)
        try:
            present = os.path.getsize(clip_path) > 0
        except OSError:
            present = False
        if not present:
            print(f"⚠️  Clip {i + 1} of {job_id} never rendered "
                  f"({clip_filename}) — dropping it from the result.")
            continue
        clip['video_url'] = f"/videos/{job_id}/{clip_filename}"
        kept.append(clip)
    return kept, len(clips) - len(kept)


def _strip_watermark_copy(output_dir, filename):
    """``wm_<final>`` -> ``<final>`` when the clean twin is on disk.

    The free plan serves a marked copy of the final file and keeps the clean
    one next to it (see the ``watermarked`` module); every derivation starts
    from the clean one, so the mark is always the outermost layer to strip.
    Same fail-safe contract as the other walk-backs: unchanged when there is
    nothing to strip or the twin is gone."""
    if watermarked.is_marked(filename):
        clean = watermarked.clean_name(filename)
        if os.path.exists(os.path.join(output_dir, clean)):
            return clean
    return filename


def _strip_burned_captions(output_dir, filename, materialize=True):
    """Walk ``subtitled_<ts>_`` prefixes back to the file without burned captions.

    Returns the name unchanged when there is nothing to strip (or when the
    underlying file is gone, e.g. a library restore that only kept the current
    version). A hooked-only picture the pipeline kept as a recipe
    (hooks.materialize) counts as there and is encoded here, unless
    ``materialize`` is False (the caller strips the hook next and never opens
    it). Encodes: call it off the event loop.
    """
    import hooks
    filename = _strip_watermark_copy(output_dir, filename)
    while True:
        m = re.match(r'^subtitled_\d+_(.+)$', filename)
        if not m:
            return filename
        under = m.group(1)
        if not os.path.exists(os.path.join(output_dir, under)):
            if not hooks.has_recipe(output_dir, under):
                return filename
            if materialize and not hooks.materialize(output_dir, under):
                return filename
        filename = under


def _strip_burned_hook(output_dir, filename):
    """Walk ``hooked_<ts>_`` (and legacy ``hook_``) prefixes back to the file
    without a burned hook. Same fail-safe contract as _strip_burned_captions:
    the name is returned unchanged when there is nothing to strip or the
    underlying file is gone."""
    filename = _strip_watermark_copy(output_dir, filename)
    while True:
        m = re.match(r'^(?:hooked_\d+_|hook_)(.+)$', filename)
        if not m or not os.path.exists(os.path.join(output_dir, m.group(1))):
            return filename
        filename = m.group(1)


def _media_guard(path: str) -> bool:
    """``/videos`` allowlist: deliverable types only (media_auth), and on a
    job rendered for the free plan never the clean twin of a served clip.
    The twins exist so an upgrade can re-point at them, not for stripping
    ``wm_`` off a URL."""
    if not media_auth.is_servable(path):
        return False
    job_id, _, filename = path.replace("\\", "/").strip("/").partition("/")
    if not filename or not watermarked.is_clean_deliverable(filename):
        return True
    return not watermarked.job_is_marked(os.path.join(OUTPUT_DIR, job_id))


def _reapply_captions(job_id, clip_index, video_path):
    """Re-burn the default captions onto a freshly derived file.

    Captions must always be the LAST layer. Editing or hooking a clip that
    already had them burned in produced `edited_subtitled_<...>`, and the next
    subtitle pass then stacked a second caption layer on top of the first —
    visibly doubled and unreadable in real user clips (26-jul-2026). So the
    derivation runs on the clean file and captions go back on afterwards.

    Returns the captioned path, or None if there was nothing to caption.
    """
    try:
        meta_files = glob.glob(os.path.join(OUTPUT_DIR, job_id, "*_metadata.json"))
        if not meta_files:
            return None
        with open(meta_files[0], 'r') as f:
            data = json.load(f)
        transcript = data.get('transcript')
        clips = data.get('shorts', [])
        if not transcript or clip_index >= len(clips):
            return None
        clip = clips[clip_index]
        # A recut clip is a concatenation of source segments, so the flat
        # start..end window is wrong for it — caption against the clip-relative
        # remapped transcript instead (same trick /api/subtitle uses).
        recipe_segments = (clip.get('recipe') or {}).get('segments')
        if recipe_segments:
            transcript = recut.virtual_transcript(transcript, recipe_segments)
            start, end = 0.0, recut.total_duration(recipe_segments)
        else:
            start, end = clip['start'], clip['end']
        if clip.get('caption_words'):
            transcript = _words_transcript(clip['caption_words'],
                                           transcript.get('language', 'en'))
            start, end = 0.0, transcript['segments'][0]['end']
        if clip.get('caption_style'):
            return _restyle_captions(video_path, transcript, start, end,
                                     clip['caption_style'],
                                     clip.get('layout_ranges'))
        import main as _main
        return _main.auto_caption_clip(video_path, transcript, start, end)
    except Exception as e:
        print(f"⚠️  Could not re-apply captions to {video_path}: {e}")
        return None


# The /api/subtitle request fields that make up a caption look. The endpoint
# records them on the clip ('caption_style' in the metadata) so the paths that
# put captions back on a freshly derived file (hook, AI edit, recut) burn the
# look the user picked. They used to burn the default one, so adding a hook
# silently swapped someone's small serif captions for big yellow Anton
# (github issue #82).
CAPTION_STYLE_FIELDS = (
    "style", "position", "font_size", "font_name", "font_color",
    "border_color", "border_width", "bg_color", "bg_opacity",
    "highlight_color", "effect", "base_opacity", "uppercase", "reveal",
    "shadow", "max_chars", "max_duration")


def _words_transcript(words, language="en"):
    """A one-segment transcript from clip-relative caption words
    ({word, start, end}, word with its leading space), so the generators burn
    user-edited text verbatim."""
    words = sorted(words, key=lambda w: w["start"])
    return {
        "language": language,
        "segments": [{
            "start": words[0]["start"], "end": max(w["end"] for w in words),
            "text": " ".join(w["word"] for w in words),
            "words": words,
        }],
    }


def _burn_caption_style(video_path, out_path, sub_path, transcript, start, end,
                        style, split_ranges=None):
    """Generate the caption file for ``style`` (CAPTION_STYLE_FIELDS, a
    missing field takes the SubtitleRequest default) and burn it onto
    ``video_path``. Returns False when no words fall in the range."""
    s = {k: SubtitleRequest.model_fields[k].default for k in CAPTION_STYLE_FIELDS}
    s.update({k: v for k, v in (style or {}).items() if k in s})
    max_chars = (max(1, min(40, int(s["max_chars"])))
                 if s["max_chars"] is not None else None)
    max_duration = (max(0.5, min(5.0, float(s["max_duration"])))
                    if s["max_duration"] is not None else None)
    if s["style"] == "karaoke":
        opts = dict(
            split_ranges=split_ranges,
            alignment=s["position"], fontsize=s["font_size"],
            font_name=s["font_name"], font_color=s["font_color"],
            border_color=s["border_color"], border_width=s["border_width"],
            highlight_color=s["highlight_color"], bg_color=s["bg_color"],
            bg_opacity=s["bg_opacity"], effect=s["effect"],
            base_opacity=s["base_opacity"], uppercase=s["uppercase"],
            reveal=s["reveal"], shadow=s["shadow"])
        if max_chars is not None:
            opts["max_chars"] = max_chars
        if max_duration is not None:
            opts["max_duration"] = max_duration
        ok = generate_ass(transcript, start, end, sub_path, **opts)
    else:
        ok = generate_srt(transcript, start, end, sub_path,
                          max_chars or 20, max_duration or 2.0)
    if not ok:
        return False
    burn_subtitles(video_path, sub_path, out_path,
                   alignment=s["position"], fontsize=s["font_size"],
                   font_name=s["font_name"], font_color=s["font_color"],
                   border_color=s["border_color"], border_width=s["border_width"],
                   bg_color=s["bg_color"], bg_opacity=s["bg_opacity"])
    return True


def _restyle_captions(video_path, transcript, start, end, style,
                      layout=None):
    """Burn a recorded caption style next to ``video_path`` with the naming
    auto_caption_clip uses (``subtitled_<ts>_<file>``, so the walk-backs find
    the clean file). Returns the captioned path, or None (no words, failure)."""
    output_dir = os.path.dirname(video_path)
    generation_id = int(time.time())
    karaoke = (style or {}).get("style") == "karaoke"
    # Neutral name: the path goes inside an ffmpeg filter string, where an
    # apostrophe from a clip title would close the quote.
    sub_path = os.path.join(
        output_dir,
        f"subs_restyle_{generation_id}_{uuid.uuid4().hex[:8]}.{'ass' if karaoke else 'srt'}")
    out_path = os.path.join(
        output_dir, f"subtitled_{generation_id}_{os.path.basename(video_path)}")
    seams = layout_ranges.split_ranges(layout or layout_ranges.read(video_path))
    try:
        if not _burn_caption_style(video_path, out_path, sub_path, transcript,
                                   start, end, style, seams):
            return None
        return out_path
    except Exception as e:
        print(f"⚠️  Could not burn the recorded caption style on {video_path}: {e}")
        return None


def _recut_captioner(clip):
    """perform_recut's ``captioner`` for a clip whose captions were styled
    in the modal, or None for the default look. Edited words are not carried:
    their timings belong to the previous cut."""
    style = clip.get('caption_style')
    if not style:
        return None
    return lambda path, transcript, start, end: _restyle_captions(
        path, transcript, start, end, style)


def _recover_jobs_from_disk():
    """Rebuild completed jobs from OUTPUT_DIR after a restart (issue #46 / #18).

    Jobs live in memory, so a restart used to orphan finished clips that are
    still on disk: the frontend restores the job_id from localStorage but every
    endpoint answers 404 "Job not found". Rebuild a minimal completed record
    for each job directory that has a metadata JSON.
    """
    recovered = 0
    try:
        entries = os.listdir(OUTPUT_DIR)
    except FileNotFoundError:
        return
    for job_id in entries:
        job_path = os.path.join(OUTPUT_DIR, job_id)
        if not os.path.isdir(job_path) or job_id in jobs:
            continue
        if os.path.isfile(os.path.join(job_path, _RESUME_FILE)):
            continue  # in flight (interrupted or on the other instance): resume, not recover
        json_files = glob.glob(os.path.join(job_path, "*_metadata.json"))
        if not json_files:
            continue
        try:
            with open(json_files[0], 'r') as f:
                data = json.load(f)
            base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
            clips = data.get('shorts', [])
            for i, clip in enumerate(clips):
                if not clip.get('video_url'):
                    clip['video_url'] = (
                        f"/videos/{job_id}/"
                        f"{_canonical_clip_file(job_path, base_name, i)}")
            owner = None
            owner_path = os.path.join(job_path, ".owner")
            if os.path.exists(owner_path):
                with open(owner_path) as f:
                    raw = f.read().strip()
                owner = int(raw) if raw.isdigit() else (raw or None)
            jobs[job_id] = {
                'status': 'completed',
                'logs': _TimedLog(["♻️ Job recovered from disk after server restart."]),
                'output_dir': job_path,
                'user_id': owner,
                'result': {'clips': clips, 'cost_analysis': data.get('cost_analysis')},
            }
            recovered += 1
        except Exception as e:
            print(f"⚠️ Could not recover job {job_id}: {e}")
    if recovered:
        print(f"♻️  Recovered {recovered} completed job(s) from disk.")


# --- Mid-flight job resume (survive a redeploy without losing work) ----------
# A job lives only in memory, so killing the container mid-processing used to
# lose it: the user's clip just stops. We persist a tiny manifest per job and,
# on startup, re-enqueue any that were interrupted — the user sees it resume
# instead of vanish. Bounded by MAX_RESUME_ATTEMPTS so a video that reliably
# crashes the worker can't crashloop the service; only resumes of a run that
# had started count (a queued video survives any number of deploys).
_RESUME_FILE = ".resume.json"
MAX_RESUME_ATTEMPTS = 2

# --- Deploy handover (two instances, one disk) -------------------------------
# Coolify starts the NEW container before it stops the old one (rolling
# update), and both see the same OUTPUT_DIR. Without coordination the new one
# re-enqueued, at startup, the very jobs the old one was still rendering —
# and the old one had 30 s to live anyway. Now:
#   * every instance stamps OUTPUT_DIR/.instance with its id at startup; an
#     instance that sees another id there knows it is the OLD one and DRAINS:
#     it finishes what it is running, starts nothing new, and leaves queued
#     manifests for the new instance to pick up;
#   * a running job writes a heartbeat into its manifest every few seconds,
#     so the new instance skips manifests that are alive elsewhere and resumes
#     only the stale ones (an instance killed mid-job stops heartbeating);
#   * SIGTERM (docker stop) also drains, up to DRAIN_TIMEOUT_SECONDS — keep it
#     under the Coolify stop grace period — before letting uvicorn exit.
INSTANCE_ID = os.environ.get("INSTANCE_ID") or socket.gethostname()
_INSTANCE_MARKER = ".instance"
HEARTBEAT_EVERY = 10                 # seconds between manifest heartbeats
HEARTBEAT_STALE_AFTER = 60           # no heartbeat for this long = nobody has it
RESUME_SCAN_INTERVAL = 30            # seconds between looks for stale manifests
HANDOVER_CHECK_INTERVAL = 5          # seconds between looks at the marker
DRAIN_TIMEOUT_SECONDS = int(os.environ.get("DRAIN_TIMEOUT_SECONDS", "840"))
# After the jobs are drained, keep SERVING this long with /health/ready at 503
# before closing the socket: the proxy only drops a container once its Docker
# healthcheck has failed interval*retries times (15 s with the Coolify
# settings), and closing the socket earlier sends that many seconds of
# requests to a dead port. Measured 2026-08-25: ~60 s of alternating 502/200
# per deploy with retries=12 and no grace at all.
PROXY_DRAIN_SECONDS = float(os.environ.get("PROXY_DRAIN_SECONDS", "20"))
# Once uvicorn has the signal it closes within --timeout-graceful-shutdown
# (15 s), but the interpreter then waits for non-daemon threads, and a
# request cancelled mid-flight can leave an executor thread stuck in a
# network probe (yt-dlp) for as long as that takes. Seen 2026-08-25: "Finished
# server process" printed, container alive until the 900 s SIGKILL, deploy
# stuck in "Removing old containers". So the process is ended outright a
# little after uvicorn was told to stop. Jobs are already drained by then.
HARD_EXIT_SECONDS = float(os.environ.get("HARD_EXIT_SECONDS", "30"))
_draining = False
_stopping = False                    # SIGTERM received: report not-ready so the
                                     # proxy stops routing here before the
                                     # listening socket closes
_running_jobs: set = set()           # job ids with a live subprocess here


def _int_env(name, default):
    try:
        return max(0, int(os.environ.get(name, str(default)).strip()))
    except (TypeError, ValueError):
        return default


def _local_running() -> int:
    """Jobs whose main.py runs on this host: what the GPU slots count."""
    return len(_running_jobs)


def _manifest_path(job_id):
    return os.path.join(OUTPUT_DIR, job_id, _RESUME_FILE)


def _read_manifest(job_id):
    try:
        with open(_manifest_path(job_id)) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"⚠️ Bad resume manifest for {job_id}: {e}")
        return None


def _touch_manifest(job_id, now=None, started=False):
    """Stamp 'this instance is running it, as of now' into the manifest.
    ``started`` (at dispatch) also stamps ``run_started``: a heartbeat says the
    worker is alive, not that the job is moving, so the metering sweeper's
    hung-job cap counts from the start of the run."""
    m = _read_manifest(job_id)
    if m is None:
        return
    m["heartbeat"] = now if now is not None else time.time()
    m["instance"] = INSTANCE_ID
    if started:
        m["run_started"] = m["heartbeat"]
    try:
        # Atomic: the other instance reads it at any time (resume scan, GPU
        # admission, the metering sweeper's live set), and a half-written
        # manifest reads as no manifest at all.
        write_json_atomic(_manifest_path(job_id), m)
    except Exception as e:
        print(f"⚠️ Could not heartbeat manifest for {job_id}: {e}")


def _manifest_busy_elsewhere(m, now=None):
    """True when ANOTHER instance heartbeated this job recently. Our own id
    with a fresh heartbeat is a job WE were running before a restart of this
    same container (same hostname) — that one must be resumed, not skipped."""
    now = time.time() if now is None else now
    return (m.get("instance") not in (None, INSTANCE_ID)
            and now - float(m.get("heartbeat") or 0) < HEARTBEAT_STALE_AFTER)


def _jobs_busy_elsewhere(now=None) -> int:
    """How many jobs another instance is running right now (fresh heartbeat).

    During a deploy the old container keeps running its jobs while it drains,
    on the same GPU. Counting them is what stops the new container from
    stacking its own full MAX_CONCURRENT_JOBS on top: 22-sep-2026, 7 old + 3
    new jobs filled the 20 GB card and 5 of 12 jobs died of CUDA OOM.
    """
    now = time.time() if now is None else now
    busy = 0
    try:
        entries = os.listdir(OUTPUT_DIR)
    except FileNotFoundError:
        return 0
    for name in entries:
        path = os.path.join(OUTPUT_DIR, name, _RESUME_FILE)
        if not os.path.exists(path):
            continue
        m = _read_manifest(name)
        # A job delivering elsewhere has left the card: it holds no GPU slot.
        if m and _manifest_busy_elsewhere(m, now) and m.get("stage") != "delivering":
            busy += 1
    return busy


SHARED_GPU_WAIT_SECONDS = 5
SHARED_GPU_MAX_WAIT = DRAIN_TIMEOUT_SECONDS + 120
# Free VRAM a new job needs before it starts. A job peaks at ~4-6 GB while it
# transcribes; starting one on a nearly full card is what made NVENC and
# TransNetV2 of the jobs already running fail with CUDA OOM. 0 disables.
GPU_MIN_FREE_MB = int(os.environ.get("GPU_MIN_FREE_MB", "4500"))
_gpu_free_cache = {"at": 0.0, "mb": None}


def _gpu_free_mb():
    """Free memory on GPU 0 in MiB, or None when there is no nvidia-smi (CPU
    host, self-host without a GPU): then only the job count limits."""
    now = time.monotonic()
    if now - _gpu_free_cache["at"] < 2:
        return _gpu_free_cache["mb"]
    mb = None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits",
             "-i", "0"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            mb = int(float(out.stdout.strip().splitlines()[0]))
    except Exception:
        mb = None
    _gpu_free_cache.update(at=now, mb=mb)
    return mb


def gpu_has_room(running_here: int, busy_elsewhere: int, free_mb) -> bool:
    """Whether a new job may start now.

    The job count covers both instances during a deploy handover. The free
    VRAM check covers what the count cannot see: jobs differ by an order of
    magnitude in memory. With nothing running anywhere the job always starts,
    so a card held by something outside our control can never stall the queue.
    """
    total = running_here + busy_elsewhere
    if total >= MAX_CONCURRENT_JOBS:
        return False
    if total == 0 or GPU_MIN_FREE_MB <= 0 or free_mb is None:
        return True
    return free_mb >= GPU_MIN_FREE_MB


async def _wait_for_shared_gpu():
    """Hold a new job until the GPU has room for it: this instance's jobs
    plus the ones another instance is still draining must fit
    MAX_CONCURRENT_JOBS, and the card must have GPU_MIN_FREE_MB free.
    Bounded: a killed instance stops heartbeating within
    HEARTBEAT_STALE_AFTER, a drain cannot outlive DRAIN_TIMEOUT_SECONDS, and
    past SHARED_GPU_MAX_WAIT the job starts anyway rather than wait forever."""
    waited = 0.0
    logged = False
    loop = asyncio.get_event_loop()
    while waited < SHARED_GPU_MAX_WAIT:
        # Reads every manifest on the shared disk: off the loop.
        elsewhere = await loop.run_in_executor(None, _jobs_busy_elsewhere)
        free_mb = await loop.run_in_executor(None, _gpu_free_mb)
        if gpu_has_room(_local_running(), elsewhere, free_mb):
            return
        if not logged:
            print(f"⏳ Holding the next job: {_local_running()} running here, "
                  f"{elsewhere} on a draining instance, "
                  f"{free_mb if free_mb is not None else '?'} MiB GPU free "
                  f"(needs {GPU_MIN_FREE_MB}).")
            logged = True
        await asyncio.sleep(SHARED_GPU_WAIT_SECONDS)
        waited += SHARED_GPU_WAIT_SECONDS


def _write_instance_marker():
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(os.path.join(OUTPUT_DIR, _INSTANCE_MARKER), "w") as f:
            f.write(INSTANCE_ID)
    except Exception as e:
        print(f"⚠️ Could not write instance marker: {e}")


def _read_instance_marker():
    try:
        with open(os.path.join(OUTPUT_DIR, _INSTANCE_MARKER)) as f:
            return f.read().strip()
    except Exception:
        return None


def _begin_drain(reason):
    global _draining
    if not _draining:
        _draining = True
        print(f"⏸️ Draining ({reason}): finishing {len(_running_jobs)} running job(s), "
              f"starting none.")


def _check_instance_marker():
    """A different id in the marker means a newer instance is up: drain."""
    other = _read_instance_marker()
    if other and other != INSTANCE_ID:
        _begin_drain(f"newer instance {other} is up")
        return True
    return False


async def _handover_watch():
    while not _draining:
        await asyncio.sleep(HANDOVER_CHECK_INTERVAL)
        _check_instance_marker()


async def _resume_scan():
    """Keep looking for manifests nobody is running (the old instance left
    them queued, or was killed at the end of its grace period)."""
    while True:
        await asyncio.sleep(RESUME_SCAN_INTERVAL)
        if _draining:
            continue
        try:
            await _resume_interrupted_jobs_async()
        except Exception as e:
            print(f"⚠️ Resume scan failed: {e}")


async def _drain_then_exit(previous_handler, timeout=None, proxy_grace=None,
                           hard_exit_after=None):
    """Wait for running jobs (bounded), let the proxy notice we are not ready,
    then hand the signal to uvicorn."""
    timeout = DRAIN_TIMEOUT_SECONDS if timeout is None else timeout
    proxy_grace = PROXY_DRAIN_SECONDS if proxy_grace is None else proxy_grace
    hard_exit_after = HARD_EXIT_SECONDS if hard_exit_after is None else hard_exit_after
    deadline = time.time() + timeout
    while _running_jobs and time.time() < deadline:
        await asyncio.sleep(1)
    if _running_jobs:
        print(f"⏱️ Drain timeout after {timeout}s with {len(_running_jobs)} job(s) still "
              f"running — they will resume on the next instance.")
    else:
        print("✅ Drained: no running jobs.")
    if proxy_grace > 0:
        print(f"⏳ Serving {proxy_grace:.0f}s more while the proxy drops this instance.")
        await asyncio.sleep(proxy_grace)
    print("👋 Shutting down.")
    if hard_exit_after > 0:
        import threading
        threading.Timer(hard_exit_after, _hard_exit).start()
    if callable(previous_handler):
        previous_handler(signal.SIGTERM, None)
    else:
        os._exit(0)


def _hard_exit():
    print(f"⛔ Still alive {HARD_EXIT_SECONDS:.0f}s after the stop signal (a thread "
          f"is hanging) — exiting now.", flush=True)
    os._exit(0)


def _install_drain_signal_handler():
    """Replace uvicorn's SIGTERM handler with drain-first. uvicorn's own
    handler closes the listening socket at once, which would make Traefik
    send half the traffic to a refused port for the whole drain."""
    previous = signal.getsignal(signal.SIGTERM)
    loop = asyncio.get_running_loop()

    def on_sigterm():
        global _stopping
        _stopping = True
        _begin_drain("SIGTERM")
        asyncio.ensure_future(_drain_then_exit(previous))

    try:
        loop.add_signal_handler(signal.SIGTERM, on_sigterm)
    except (NotImplementedError, RuntimeError, ValueError) as e:
        print(f"⚠️ Drain-on-SIGTERM unavailable ({e}); jobs will resume on restart instead.")


def _write_resume_manifest(job_id, cmd, priority, user_id,
                           partial=None, source_cap_minutes=None, skip_statics=False,
                           submitted_at=None,
                           label=None, settings=None):
    try:
        path = os.path.join(OUTPUT_DIR, job_id, _RESUME_FILE)
        # Atomic like every manifest write: the other instance reads them.
        write_json_atomic(path, {
                "cmd": cmd, "priority": priority,
                "user_id": None if user_id is None else str(user_id),
                "attempts": 0,
                # A resumed job downloads the source again, so the cut must
                # travel with it.
                "partial": partial,
                # Same reason for the whole-video jobs' safety cap.
                "source_cap_minutes": source_cap_minutes,
                # The probe's "statics bot-checked for this video" verdict.
                "skip_statics": bool(skip_statics),
                # Submission time: a job enqueued again (deploy, resume scan)
                # keeps its place in the queue.
                "submitted_at": submitted_at,
                "label": label,
                "settings": settings,
        })
    except Exception as e:
        print(f"⚠️ Could not write resume manifest for {job_id}: {e}")


def _clear_resume_manifest(job_id):
    """Drop the manifest once a job reaches a terminal state, so it is never
    re-run on a later restart. Only an interrupted (still-running) job keeps it."""
    try:
        os.remove(os.path.join(OUTPUT_DIR, job_id, _RESUME_FILE))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"⚠️ Could not clear resume manifest for {job_id}: {e}")


def _legacy_submitted_at(job_id, manifest_mtime=None):
    """Submission time of a manifest written before ``submitted_at`` existed:
    the ``.owner`` sidecar is written at submit; else the manifest's own mtime
    (its last write, the best left); else now."""
    try:
        return os.path.getmtime(os.path.join(OUTPUT_DIR, job_id, ".owner"))
    except OSError:
        pass
    return manifest_mtime if manifest_mtime else time.time()


def _resume_candidates(held: set, now: float):
    """Disk part of the resume scan (a worker thread on the periodic scan):
    ``[(job_id, manifest)]``. ``held`` is a copy of the
    ids this instance already has. Each picked manifest is rewritten with its
    attempt count, ``resumed_at`` and, for one written before it existed, the
    submission time, so its place in the queue is fixed from now on."""
    picked = []
    for job_id in _manifest_names():
        manifest_path = _manifest_path(job_id)
        # A manifest outlives only an interrupted run (the job wrapper drops it
        # at every terminal state), so a metadata file next to it means the job
        # was stopped mid-render, not that it finished: resume it too.
        try:
            with open(manifest_path) as f:
                m = json.load(f)
            mtime = os.path.getmtime(manifest_path)
        except Exception as e:
            print(f"⚠️ Bad resume manifest for {job_id}: {e}")
            continue
        if not isinstance(m, dict):
            continue

        if job_id in held:
            continue  # already ours (queued, running or recovered)
        if _manifest_busy_elsewhere(m, now):
            continue  # the other instance is on it; we take over if it goes stale

        # Only a run that STARTED since the last resume counts as an attempt:
        # the limit is there for a video that crashes the worker, and a video
        # that only waited in the queue through deploys has crashed nothing.
        # Counting every resume dropped long-queued videos that never ran
        # once a few deploys had gone by.
        ran = (float(m.get("run_started") or m.get("heartbeat") or 0)
               > float(m.get("resumed_at") or 0))
        attempts = int(m.get("attempts", 0)) + (1 if ran else 0)
        if attempts > MAX_RESUME_ATTEMPTS:
            # Poison job: don't resume; the user can retry by hand.
            print(f"🛑 Job {job_id} exceeded {MAX_RESUME_ATTEMPTS} resume attempts — giving up.")
            _clear_resume_manifest(job_id)
            continue

        m["attempts"] = attempts
        m["resumed_at"] = now
        if m.get("submitted_at") is None:
            m["submitted_at"] = _legacy_submitted_at(job_id, mtime)
        try:
            write_json_atomic(manifest_path, m)
        except Exception:
            pass
        picked.append((job_id, m))
    return picked


def _register_resumed(picked) -> int:
    """Loop part of the resume scan: a record and a heap entry per picked
    manifest. The heap orders them by submission time, whatever order the
    directory listing gave."""
    resumed = 0
    for job_id, m in picked:
        if job_id in jobs:
            continue  # registered while the disk was being read
        user_id = m.get("user_id")
        attempts = int(m.get("attempts", 0))
        job_path = os.path.join(OUTPUT_DIR, job_id)
        # Rebuild env from scratch — the manifest holds no secrets.
        env = child_env()
        partial = m.get("partial")
        _apply_source_cut(env, partial)
        if m.get("source_cap_minutes"):
            env["SOURCE_CAP_MINUTES"] = str(m["source_cap_minutes"])
        else:
            env.pop("SOURCE_CAP_MINUTES", None)
        if m.get("skip_statics"):
            env["DOWNLOAD_SKIP_STATICS"] = "1"
        else:
            env.pop("DOWNLOAD_SKIP_STATICS", None)

        priority = int(m.get("priority", 2))
        jobs[job_id] = {
            'status': 'queued',
            'logs': _TimedLog([f"♻️ Resuming your video after a server update (attempt {attempts})."]),
            'cmd': m.get("cmd"),
            'env': env,
            'output_dir': job_path,
            'user_id': None if user_id is None else user_id,
            'partial': partial or None,
            'label': m.get("label") if isinstance(m.get("label"), str) else None,
            'settings': m.get("settings") if isinstance(m.get("settings"), dict) else None,
            # It ran before (drain-cut, or an instance that died).
            'last_run': float(m.get("run_started") or m.get("heartbeat") or 0) or None,
            'submitted_at': float(m["submitted_at"] if m.get("submitted_at") is not None else time.time()),
        }
        _enqueue_job(job_id, priority)
        resumed += 1
    if resumed:
        print(f"♻️  Re-enqueued {resumed} interrupted job(s) after restart.")
    return resumed


def _resume_interrupted_jobs():
    """Re-enqueue jobs that were mid-processing when the server last stopped.

    Runs after _recover_jobs_from_disk: a job whose clips already finished has a
    metadata JSON and is recovered as 'completed', so we only resume manifests
    with no metadata yet (analysis never finished). Also called periodically
    (_resume_scan, through ``_resume_interrupted_jobs_async``): a manifest
    another instance is heartbeating is left alone until that heartbeat goes
    stale, and a job this instance already holds is never enqueued twice.
    A poison job (too many attempts) is simply not resumed.
    """
    _register_resumed(_resume_candidates(set(jobs), time.time()))


async def _resume_interrupted_jobs_async():
    """The periodic resume scan: the manifests are read (and rewritten) in a
    worker thread, only the records and the heap are touched on the loop."""
    _register_resumed(await asyncio.to_thread(_resume_candidates, set(jobs), time.time()))


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# A job dir older than JOB_RETENTION_SECONDS can still be alive: on a busy day
# a job can wait in the queue past the retention, and a long render runs past
# it too. The sweeps used to delete those by age alone, under the worker's
# feet. A manifest pins its dir for at most this long, so one nobody will
# ever resume cannot keep a directory on disk forever.
ACTIVE_JOB_MAX_SECONDS = int(os.environ.get("ACTIVE_JOB_MAX_SECONDS", str(24 * 3600)))


def _job_is_active(job_id, now=None) -> bool:
    """True while a job is queued or running here, or its resume manifest says
    it has not reached a terminal state yet (the other instance's jobs during a
    handover included). The disk sweeps must never touch such a job."""
    if job_id in _running_jobs or _job_dir_in_flight(job_id):
        return True
    if (jobs.get(job_id) or {}).get('status') in ('queued', 'processing'):
        return True
    try:
        age = (time.time() if now is None else now) - os.path.getmtime(_manifest_path(job_id))
    except OSError:
        return False
    return age <= ACTIVE_JOB_MAX_SECONDS


def _upload_job_id(filename: str) -> str:
    """Source uploads are stored as ``<job_id>_<name>``."""
    return filename.split("_", 1)[0]


def _upload_is_live(filename: str, now=None) -> bool:
    """A source the sweeps must keep: its job is live."""
    return _job_is_active(_upload_job_id(filename), now)


def _output_jobs_size() -> int:
    """Bytes under OUTPUT_DIR that the size cap can free: the job dirs (and
    loose files), not the dot entries. Those are never purged by it, and one
    of them is the download cache (``.source_cache``, source_cache.py) with a
    cap of its own: counting it here would purge finished jobs to make room
    for files this cap may not delete."""
    total = 0
    try:
        names = os.listdir(OUTPUT_DIR)
    except OSError:
        return 0
    for name in names:
        if _not_a_job_dir(name):
            continue
        path = os.path.join(OUTPUT_DIR, name)
        try:
            total += _dir_size(path) if os.path.isdir(path) else os.path.getsize(path)
        except OSError:
            pass
    return total


def _not_a_job_dir(name) -> bool:
    """Dot entries of OUTPUT_DIR are the instances' shared state, never a job:
    the country database (``.geoip/``), the queue watch and mode files, the
    priority stamp, lock files. No sweep, size cap or purge touches them."""
    return str(name).startswith(".")


def _sweep_expired_job_dirs(now):
    """Drop job dirs older than JOB_RETENTION_SECONDS, except live jobs."""
    for job_id in os.listdir(OUTPUT_DIR):
        # Not a job: the thumbnails dir backs a StaticFiles mount, so
        # deleting it would 500 every /thumbnails request until reboot.
        if job_id == os.path.basename(THUMBNAILS_DIR) or _not_a_job_dir(job_id):
            continue
        job_path = os.path.join(OUTPUT_DIR, job_id)
        if not os.path.isdir(job_path):
            continue
        try:
            expired = now - os.path.getmtime(job_path) > JOB_RETENTION_SECONDS
        except OSError:
            continue
        if not expired or _job_is_active(job_id, now):
            continue
        print(f"🧹 Purging old job: {job_id}")
        shutil.rmtree(job_path, ignore_errors=True)
        jobs.pop(job_id, None)


def _sweep_expired_uploads(now):
    """Drop source uploads older than JOB_RETENTION_SECONDS, except the source
    of a job that is still queued or running."""
    for filename in os.listdir(UPLOAD_DIR):
        file_path = os.path.join(UPLOAD_DIR, filename)
        try:
            if (now - os.path.getmtime(file_path) > JOB_RETENTION_SECONDS
                    and not _upload_is_live(filename, now)):
                os.remove(file_path)
        except Exception:
            pass


def _job_dir_is_fresh(job_id, now=None) -> bool:
    """The job's output dir was written within OUTPUT_CAP_GRACE_SECONDS: its
    clips just came out or someone is editing them, so the size caps leave
    the job and its source alone."""
    try:
        mtime = os.path.getmtime(os.path.join(OUTPUT_DIR, job_id))
    except OSError:
        return False
    return (time.time() if now is None else now) - mtime < OUTPUT_CAP_GRACE_SECONDS


def _enforce_uploads_size_cap():
    """Delete the oldest source uploads while UPLOAD_DIR is over UPLOADS_MAX_GB.

    Sources are only needed while a job runs (and for the preview afterwards),
    but they're the biggest files on disk — up to MAX_FILE_SIZE_MB each. The
    source of a job whose clips are fresh stays (_job_dir_is_fresh): with the
    queued jobs alone over the cap, every upload went minutes after its job
    ended, and with it the source preview and every edit that cuts from it.
    """
    cap = UPLOADS_MAX_GB * 1024 ** 3
    if cap <= 0:
        return
    used = _dir_size(UPLOAD_DIR)
    if used <= cap:
        return
    files = []
    for name in os.listdir(UPLOAD_DIR):
        p = os.path.join(UPLOAD_DIR, name)
        if (os.path.isfile(p) and not _upload_is_live(name)
                and not _job_dir_is_fresh(_upload_job_id(name))):
            try:
                files.append((os.path.getmtime(p), p, os.path.getsize(p)))
            except OSError:
                pass
    files.sort()
    print(f"🧹 Uploads at {used / 1024**3:.1f} GB (cap {UPLOADS_MAX_GB} GB) — trimming.")
    for _mtime, path, size in files:
        if used <= cap:
            break
        try:
            os.remove(path)
            used -= size
            print(f"🧹 Size cap: removed upload {os.path.basename(path)}")
        except OSError:
            pass


def _enforce_output_size_cap():
    """Delete the oldest job dirs while OUTPUT_DIR is over OUTPUT_MAX_GB.
    Live jobs and dirs touched within OUTPUT_CAP_GRACE_SECONDS are kept, so
    the directory can sit over the cap by what those hold."""
    cap = OUTPUT_MAX_GB * 1024 ** 3
    if cap <= 0:
        return
    used = _output_jobs_size()
    if used <= cap:
        return
    thumbs = os.path.basename(THUMBNAILS_DIR)
    fresh_after = time.time() - OUTPUT_CAP_GRACE_SECONDS
    candidates = []
    for job_id in os.listdir(OUTPUT_DIR):
        if job_id == thumbs or _not_a_job_dir(job_id):
            continue
        p = os.path.join(OUTPUT_DIR, job_id)
        if os.path.isdir(p) and not _job_is_active(job_id):
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            if mtime <= fresh_after:
                candidates.append((mtime, p, job_id))
    candidates.sort()  # oldest first
    print(f"🧹 Output dir at {used / 1024**3:.1f} GB (cap {OUTPUT_MAX_GB} GB) — trimming.")
    for _mtime, path, job_id in candidates:
        if used <= cap:
            break
        size = _dir_size(path)
        shutil.rmtree(path, ignore_errors=True)
        jobs.pop(job_id, None)
        used -= size
        print(f"🧹 Size cap: purged {job_id} ({size / 1024**2:.0f} MB)")


def _sweep_retained_sources(now=None):
    """Delete retained downloads older than SOURCE_RETENTION_SECONDS.

    Only the ``source_video`` a URL job kept in its own directory: an upload is
    the user's own file and ages out with the job like everything else. Yields
    the job ids it emptied. A no-op unless the knob is set below the job clock.
    """
    if SOURCE_RETENTION_SECONDS >= JOB_RETENTION_SECONDS:
        return
    now = time.time() if now is None else now
    for job_id in os.listdir(OUTPUT_DIR):
        if job_id == os.path.basename(THUMBNAILS_DIR) or _not_a_job_dir(job_id):
            continue
        try:
            metas = glob.glob(os.path.join(OUTPUT_DIR, job_id, "*_metadata.json"))
            if not metas:
                continue
            with open(metas[0]) as f:
                name = json.load(f).get('source_video')
            if not name:
                continue
            src = os.path.join(OUTPUT_DIR, job_id, os.path.basename(name))
            if (os.path.exists(src)
                    and now - os.path.getmtime(src) > SOURCE_RETENTION_SECONDS):
                os.remove(src)
                yield job_id
        except Exception:
            continue


async def cleanup_jobs():
    """Background task to remove old jobs and files."""
    import time
    print("🧹 Cleanup task started.")
    while True:
        try:
            await asyncio.sleep(300) # Check every 5 minutes
            now = time.time()
            
            # Simple directory cleanup based on modification time
            # Check OUTPUT_DIR
            _sweep_expired_job_dirs(now)

            for job_id in _sweep_retained_sources(now):
                print(f"🧹 Dropped retained source for job {job_id}")

            # Hard disk cap. The time-based sweep above bounds the *age* of what
            # we keep, not its size: a burst of long videos can fill the volume
            # inside one retention window. Drop the oldest jobs until we're back
            # under the cap.
            _enforce_output_size_cap()
            _enforce_uploads_size_cap()

            # Cleanup Uploads
            _sweep_expired_uploads(now)

        except Exception as e:
            print(f"⚠️ Cleanup error: {e}")

async def process_queue():
    """Background worker to process jobs from the queue with concurrency limit."""
    print(f"🚀 Job Queue Worker started with {MAX_CONCURRENT_JOBS} concurrent slots.")
    while True:
        try:
            # Wait for a job (priority, seq, job_id) — lowest priority first.
            _priority, _seq, job_id = await job_queue.get()
            _queue_changed()

            if _draining:
                # Leave it on disk for the next instance (manifest, no heartbeat).
                print(f"⏸️ Draining — leaving {job_id} for the next instance.")
                job_queue.task_done()
                continue

            # Acquire semaphore slot (waits if max jobs are running)
            _admitting.add(job_id)
            await concurrency_semaphore.acquire()
            if _draining:
                _admitting.discard(job_id)
                concurrency_semaphore.release()
                job_queue.task_done()
                print(f"⏸️ Draining — leaving {job_id} for the next instance.")
                continue
            # The old container may still be draining its jobs on this GPU.
            await _wait_for_shared_gpu()
            _admitting.discard(job_id)
            if _draining:
                concurrency_semaphore.release()
                job_queue.task_done()
                print(f"⏸️ Draining — leaving {job_id} for the next instance.")
                continue
            print(f"🔄 Acquired slot for job: {job_id}")
            _running_jobs.add(job_id)
            if job_id in jobs:
                jobs[job_id]['run_started'] = time.time()
            _touch_manifest(job_id, started=True)

            # Process in background task to not block the loop (allowing other slots to fill)
            asyncio.create_task(run_job_wrapper(job_id))
            
        except Exception as e:
            print(f"❌ Queue dispatch error: {e}")
            await asyncio.sleep(1)

def _release_slot(job_id):
    """The job left the running set: free what it held."""
    _running_jobs.discard(job_id)
    concurrency_semaphore.release()
    job_queue.task_done()


async def run_job_wrapper(job_id):
    """Wrapper to run job and release semaphore"""
    # Keep the record itself: run_job writes its status into this same dict,
    # even if the entry is dropped from ``jobs`` mid-run.
    job = jobs.get(job_id)
    interrupted = False
    started = time.monotonic()
    try:
        if job:
            await run_job(job_id, job)
            if job.get('status') == 'completed':
                _record_job_duration(time.monotonic() - started)
    except asyncio.CancelledError:
        # The server is shutting down (drain timeout of a deploy) and took this
        # task with it. Not a failure: the manifest stays on disk and the next
        # instance resumes the job (22-sep-2026: 6 jobs were marked failed and
        # refunded at once this way instead).
        interrupted = True
    except Exception as e:
         print(f"❌ Job wrapper error {job_id}: {e}")
    finally:
        if interrupted or (_stopping and job and job.get('status') != 'completed'):
            _release_slot(job_id)
            print(f"♻️ Server stopping: leaving {job_id} for the next instance.")
            if interrupted:
                raise asyncio.CancelledError()
            return
        if _should_auto_retry(job):
            _schedule_auto_retry(job_id, job)
            _release_slot(job_id)
            print(f"🔁 Released slot for job {job_id} (auto-retry scheduled).")
            return
        # The subprocess returned (success or genuine failure): a terminal state.
        _clear_resume_manifest(job_id)
        _release_slot(job_id)
        print(f"✅ Released slot for job: {job_id}")
    if job is not None:
        if job.get('stop_requested'):
            _discard_job_files(job_id, job)


def _discard_job_files(job_id, job=None):
    """A stopped job leaves nothing on disk: its partial clips, downloaded
    source and uploaded file go now instead of at the retention sweep. The
    record stays, so the queue still shows it was stopped. Only called once
    main.py is gone (or never started)."""
    shutil.rmtree(os.path.join(OUTPUT_DIR, job_id), ignore_errors=True)
    for path in glob.glob(os.path.join(UPLOAD_DIR, f"{glob.escape(job_id)}_*")):
        try:
            os.remove(path)
        except OSError:
            pass
    if job is not None:
        job['result'] = None


def _clear_unfinished_job_dirs():
    """Startup, self-host: drop what failed or stopped jobs left behind. A job
    dir with neither clips (metadata) nor a resume manifest ended without
    clips; one with a manifest is resumed instead, so it is kept. Nothing is
    running yet, so nothing here can be in use."""
    try:
        names = os.listdir(OUTPUT_DIR)
    except OSError:
        return
    cleared = 0
    for name in names:
        path = os.path.join(OUTPUT_DIR, name)
        if (name == os.path.basename(THUMBNAILS_DIR) or _not_a_job_dir(name)
                or not os.path.isdir(path) or os.path.exists(os.path.join(path, _RESUME_FILE))
                or glob.glob(os.path.join(glob.escape(path), "*_metadata.json"))):
            continue
        _discard_job_files(name)
        cleared += 1
    if cleared:
        print(f"🧹 Cleared {cleared} failed or stopped job(s) left on disk.")


def _env_int(name, default, minimum=0):
    try:
        return max(minimum, int(os.environ.get(name, str(default)).strip()))
    except (TypeError, ValueError):
        return default


# --- Automatic retry of transient failures ----------------------------------
# A job that dies of a condition on OUR side (a full GPU, an NVENC session
# refused, a Gemini 5xx) is run once more by itself instead of showing the user
# "failed". Content problems (no audio, private video, nothing clip-shaped,
# policy block) fail as before: retrying cannot fix them.
AUTO_RETRY_LIMIT = int(os.environ.get("AUTO_RETRY_LIMIT", "1"))
AUTO_RETRY_DELAY_SECONDS = float(os.environ.get("AUTO_RETRY_DELAY_SECONDS", "30"))
_TRANSIENT_MARKERS = (
    "out of memory", "outofmemory", "cuda", "cublas", "cudnn", "nvenc",
    "generic error in an external library", "exit code 187", "broken pipe",
    "no clips could be rendered", "resource_exhausted", "resource exhausted",
    "503", "502", "500 internal", "overloaded", "deadline exceeded",
    "exit code -9", "exit code -15", "killed",
)
_PERMANENT_MARKERS = (
    "no audio", "no_audio", "video unavailable", "private video", "members-only",
    "prohibited_content", "blocked this video", "blocked its answer",
    "did not return usable clips", "clip detection failed", "short-form content",
    "sign in to confirm your age", "not available in your country",
)


def is_transient_failure(logs) -> bool:
    """Whether a failed job's logs describe something a second run can fix."""
    text = _job_error_text(list(logs or [])).lower()
    if any(m in text for m in _PERMANENT_MARKERS):
        return False
    return any(m in text for m in _TRANSIENT_MARKERS)


def _should_auto_retry(job) -> bool:
    if not (job and job.get('status') == 'failed' and not _draining and job.get('cmd')):
        return False
    if job.get('stop_requested'):
        return False
    # YouTube limiting the account is neither the video's fault nor a server
    # fault: the job waits out the cooldown, a few times at most, on its own
    # counter (it has not used its one retry for real failures).
    if job.get('yt_limited'):
        return int(job.get('yt_retries') or 0) < YT_LIMIT_RETRIES
    return bool(int(job.get('auto_retries') or 0) < AUTO_RETRY_LIMIT
                and is_transient_failure(job.get('logs')))


def _clean_for_retry(output_dir):
    """Drop the half-made outputs of a failed run; keep the source, the
    transcript checkpoint (the retry skips transcription), owner and manifest."""
    try:
        names = os.listdir(output_dir)
    except OSError:
        return
    for name in names:
        if ("_clip_" in name or name.endswith("_metadata.json")
                or name.startswith(("temp_", "hooked_", "autosubs_"))):
            try:
                os.remove(os.path.join(output_dir, name))
            except OSError:
                pass


YT_LIMIT_RETRIES = _int_env("YT_LIMIT_RETRIES", 3)
YT_LIMITED_LOG = ("⏳ YouTube is limiting downloads right now. Your video keeps its place "
                  "and starts again in about {n} min, no minutes are charged twice.")


def _schedule_auto_retry(job_id, job):
    delay = AUTO_RETRY_DELAY_SECONDS
    if job.pop('yt_limited', None):
        # Back in line for when the account's cooldown is over (plus a spread,
        # so the videos that waited do not all ask YouTube in the same second).
        job['yt_retries'] = int(job.get('yt_retries') or 0) + 1
        delay = max(delay, yt_account.cooling_down()) + 20 + (hash(job_id) % 120)
        job['logs'].append(YT_LIMITED_LOG.format(n=max(1, round(delay / 60))))
        print(f"⏳ {job_id} waits {delay:.0f}s for the YouTube account "
              f"({job['yt_retries']}/{YT_LIMIT_RETRIES}).")
    else:
        job['auto_retries'] = int(job.get('auto_retries') or 0) + 1
        job['logs'].append("🔁 A temporary server problem interrupted your video. "
                           "Retrying it automatically, no minutes are charged twice.")
        print(f"🔁 Auto-retry {job['auto_retries']}/{AUTO_RETRY_LIMIT} for {job_id} "
              f"in {AUTO_RETRY_DELAY_SECONDS:.0f}s.")
    job['status'] = 'queued'
    job['result'] = None
    job['ready_files'] = {}
    _clean_for_retry(job.get('output_dir') or os.path.join(OUTPUT_DIR, job_id))

    async def _later():
        await asyncio.sleep(delay)
        if job_id in jobs and jobs[job_id].get('status') == 'queued':
            m = _read_manifest(job_id) or {}
            _enqueue_job(job_id, int(m.get("priority", 1)))

    asyncio.create_task(_later())


# Markers that identify a line as an actual error rather than progress noise.
# "Error:" (capital E) catches raised exception lines — RuntimeError:,
# DownloadError:, GeminiBlockedError: — which "ERROR:" alone missed, leaving
# alerts with a bare "Traceback ... exit code 1" and no cause (prod 20-ago).
_ERROR_MARKERS = ("❌", "ERROR:", "Error:", "Traceback", "FATAL", "Exception",
                  "Process failed with exit code", "No metadata file generated",
                  "Execution error:",
                  # A clip render dies in two steps: reframe_v2 raises (it has
                  # no False return, only `return True`), main.py prints this
                  # and falls back to the v1 loop, and only if v1 ALSO fails
                  # does "❌ Clip N failed" appear. The ❌ line then carries
                  # v1's exception, not the one that started it, so without
                  # this marker the alert names the fallback and hides the
                  # cause. Both lines are wanted.
                  "Reframe v2 failed")


def _job_error_text(logs) -> str:
    """The lines that explain WHY a job failed, for the alert's classifier.

    The tail of the log is usually progress noise (scene detection, ffmpeg
    banners), which made alerts blame whatever word happened to be nearby —
    a silent upload got reported as a broken download path, and a Gemini blip
    as an ffmpeg problem. Pick the error-bearing lines instead, newest last.
    """
    # Per-attempt download warnings are only noise when a later attempt
    # RECOVERED: HD-direct fails on every job (banned server IP) and a static
    # proxy takes over, yet its "Video unavailable" made whole alerts read as
    # download outages when the job actually died in Gemini. When no attempt
    # succeeded, those lines carry the real cause and must stay.
    recovered = any("Download succeeded" in ln for ln in logs)
    hits = [ln for ln in logs
            if any(m in ln for m in _ERROR_MARKERS)
            and not (recovered and "Download attempt" in ln)]
    if not hits:
        return " ".join(logs[-10:])  # nothing recognisable — fall back to the tail
    return " ".join(hits[-6:])


# Where the last jobs' Parakeet ran ("cuda"/"cpu"), oldest first, from each
# job's JOB_CPU line. A job that never loaded Parakeet (whisper backend, a
# retry that reused its transcript) reports nothing and is not counted.
_recent_asr_devices: list = []


def _note_job_asr(payload):
    try:
        device = json.loads(payload).get("asr_device")
    except Exception:
        return
    if device in ("cuda", "cpu"):
        _recent_asr_devices.append(device)
        del _recent_asr_devices[:-12]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Self-host keeps no files of jobs that ended without clips.
    _clear_unfinished_job_dirs()
    # Rehydrate finished jobs from disk before serving (survives restarts).
    _recover_jobs_from_disk()
    # Re-enqueue jobs that were mid-processing when we stopped (redeploy).
    _resume_interrupted_jobs()
    # Deploy handover: claim the marker (any older instance sees it and drains),
    # keep watching it in case a newer one appears, keep looking for manifests
    # left behind, and drain instead of dying on docker stop.
    _write_instance_marker()
    _install_drain_signal_handler()
    asyncio.create_task(_handover_watch())
    asyncio.create_task(_resume_scan())
    asyncio.create_task(cleanup_jobs())
    asyncio.create_task(process_queue())
    yield

app = FastAPI(lifespan=lifespan)

# Enable CORS for the frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Without this the browser hides them from fetch(), so the clip download had
    # no total to measure against and could not show progress. Safelisted or not,
    # they only become readable to JS once they are named here.
    expose_headers=["Content-Length", "Content-Range", "Accept-Ranges"],
)


def _is_uuid_id(value) -> bool:
    """Job and thumbnail-session ids are lowercase uuid4 strings."""
    return isinstance(value, str) and bool(_JOB_ID_RE.match(value))


def _claim_thumbnail_session_id(session_id) -> str:
    """The thumbnail session id to use: the caller's existing one, or a fresh
    server-minted id when the client sent none or a malformed one (the id
    becomes a directory name, so only uuid4 strings are accepted)."""
    if session_id in thumbnail_sessions or _is_uuid_id(session_id):
        return session_id
    return str(uuid.uuid4())


class _GuardedStaticFiles(StaticFiles):
    """StaticFiles that serves only what ``guard`` allows: the job directories
    also hold manifests, transcripts and sidecars that are not deliverables."""

    def __init__(self, *args, guard, **kwargs):
        super().__init__(*args, **kwargs)
        self.guard = guard

    async def get_response(self, path: str, scope):
        if not self.guard(path):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)


# Mount static files for serving videos.
app.mount("/videos", _GuardedStaticFiles(directory=OUTPUT_DIR, guard=_media_guard), name="videos")

# Mount static files for serving thumbnails
THUMBNAILS_DIR = os.path.join(OUTPUT_DIR, "thumbnails")
os.makedirs(THUMBNAILS_DIR, exist_ok=True)
# Same allowlist as /videos: images only, never a session's scratch files.
app.mount("/thumbnails", _GuardedStaticFiles(
    directory=THUMBNAILS_DIR, guard=media_auth.is_servable), name="thumbnails")


def _safe_under(base_dir: str, user_rel_path: str) -> Optional[str]:
    """Resolve ``user_rel_path`` under ``base_dir`` and reject path traversal.

    Returns the absolute path only if it stays inside ``base_dir`` (after
    following ``..``); otherwise None. Used to sanitize client-supplied file
    references so ``../../.env`` can't escape the output directories.
    """
    base = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(base, user_rel_path))
    if target == base or target.startswith(base + os.sep):
        return target
    return None


# Masks user:password credentials embedded in any URL (e.g. the residential
# proxy URL that yt-dlp echoes in its verbose debug output) before the line is
# ever printed to the server console or stored in the job log.
_CREDENTIAL_URL_RE = re.compile(r'(\w+://)[^:/@\s]+:[^@/\s]+@')


def _scrub_secrets(line: str) -> str:
    return _CREDENTIAL_URL_RE.sub(r'\1***:***@', line)


class _TimedLog(list):
    """A job's log lines plus the time each one arrived (``.times``).

    The dashboard used to stamp every line with the time it was RENDERED, so
    all of them showed the same clock and it changed on every poll.
    """

    def __init__(self, lines=()):
        super().__init__(lines)
        self.times = [time.time()] * len(self)

    def append(self, line):
        super().append(line)
        self.times.append(time.time())

    def extend(self, lines):
        lines = list(lines)
        super().extend(lines)
        self.times.extend([time.time()] * len(lines))


def _visible_logs_timed(logs):
    """(lines, times) to surface to the client; see _visible_logs."""
    times = getattr(logs, "times", None)
    if times is not None and len(times) != len(logs):
        times = None
    return list(logs), list(times) if times is not None else None


def _handle_job_line(job_id, line):
    """One output line of a job's main.py: internal markers are consumed, the
    rest goes to the job's log."""
    decoded_line = _scrub_secrets(line.strip())
    if not decoded_line:
        return
    # Internal marker: a clip finished its whole chain and this is the file to
    # serve for it. Consumed here like PROXY_BYTES so it never reaches the
    # user's log.
    if decoded_line.startswith("CLIP_READY "):
        try:
            _, index, filename = decoded_line.split(" ", 2)
            if job_id in jobs:
                jobs[job_id].setdefault('ready_files', {})[int(index)] = filename
        except ValueError:
            pass
        return
    if decoded_line.startswith("JOB_CPU "):
        # Internal marker: the job's CPU time (main.py, at exit).
        print(f"📊 [job-cpu] {job_id} {decoded_line[8:]}")
        _note_job_asr(decoded_line[8:])
        return
    if decoded_line.startswith("YT_ACCOUNT_LIMITED"):
        # Internal marker from main.py's downloader: the download failed only
        # because YouTube is limiting the account (yt_account). The job goes
        # back in line for when the cooldown is over (_schedule_auto_retry).
        if job_id in jobs:
            jobs[job_id]['yt_limited'] = True
        return
    if decoded_line.startswith(("PROXY_BYTES=", "PROXY_ROUTE=")):
        return  # internal markers from main.py's downloader, not log lines
    if _DOWNLOADER_CHATTER.match(decoded_line):
        return
    print(f"📝 [Job Output] {decoded_line}")
    if job_id in jobs:
        jobs[job_id]['logs'].append(decoded_line)


# ffmpeg fetching a segmented (HLS) source says one line per request and per
# connection: tens of thousands for a long video. They are kept out of the
# job's log, which every status poll reads from start to end, and out of the
# server's own log. The downloader's errors and its summary still print.
_DOWNLOADER_CHATTER = re.compile(r'\[(?:hls|tcp|tls|https?|AVIOContext) @ 0x[0-9a-f]+\] ')


def enqueue_output(out, job_id):
    """Reads output from a subprocess and appends it to jobs logs."""
    try:
        for line in iter(out.readline, b''):
            _handle_job_line(job_id, line.decode('utf-8', 'replace'))
    except Exception as e:
        print(f"Error reading output for job {job_id}: {e}")
    finally:
        out.close()

def _job_nice():
    """Niceness for a job process and everything it starts (ffmpeg, ASR...).

    A job is batch work: when the host is short of CPU it should yield to the
    API answering a user, editor renders included, which run in this process
    at the normal priority. JOB_NICE=0 turns it off."""
    try:
        return max(0, min(19, int(os.environ.get("JOB_NICE", "10"))))
    except ValueError:
        return 10


def _lower_priority():
    n = _job_nice()
    if n:
        os.nice(n)


def _job_encode_env(env):
    """Job env plus the NVENC encode cap that bin/ffmpeg enforces.

    JOB_NVENC_SLOTS (default 4) job encodes at once on the card, host-wide:
    the lock files live on the shared output/ volume, so both containers of a
    deploy handover count against the same slots. Only job processes get it;
    editor renders in this process stay uncapped, like JOB_NICE. 0 turns it
    off."""
    env = dict(env)
    slots = os.environ.get("JOB_NVENC_SLOTS", "4").strip()
    env["NVENC_SLOTS"] = slots if slots.isdigit() else "4"
    env["NVENC_LOCK_DIR"] = os.path.abspath(OUTPUT_DIR)
    return env


def _refresh_partial_result(job_id, output_dir):
    """While a job runs: the clips main.py has announced so far, so the user
    sees each one as it lands."""
    try:
        json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
        if not json_files:
            return
        target_json = json_files[0]
        # It might be being written to: a partial file fails json.load and
        # the next look reads it whole.
        if os.path.getsize(target_json) <= 0:
            return
        with open(target_json, 'r') as f:
            data = json.load(f)
        clips = data.get('shorts', [])
        cost_analysis = data.get('cost_analysis')
        # Only clips main.py has announced as finished. It names the file
        # itself, so a clip shows up WITH its hook and captions instead of as
        # the bare reframe, and it is never served while ffmpeg is still
        # writing it. A clip whose marker never arrives (it failed) simply
        # stays hidden until the job ends and the result is rebuilt from disk.
        ready_files = (jobs.get(job_id) or {}).get('ready_files') or {}
        ready_clips = []
        for i, clip in enumerate(clips):
            clip_filename = ready_files.get(i)
            if not clip_filename:
                continue
            clip_path = os.path.join(output_dir, clip_filename)
            if os.path.exists(clip_path) and os.path.getsize(clip_path) > 0:
                clip['video_url'] = f"/videos/{job_id}/{clip_filename}"
                ready_clips.append(clip)
        if ready_clips:
            jobs[job_id]['result'] = {'clips': ready_clips, 'cost_analysis': cost_analysis}
    except Exception:
        # Ignore read errors during processing
        pass


def _finish_job(job_id, returncode, output_dir):
    """main.py exited: the job's final status
    and result, from the exit code and what is on disk."""
    if returncode == 0:
        jobs[job_id]['status'] = 'completed'
        jobs[job_id]['logs'].append("Process finished successfully.")

        # Optional AWS S3 backup (silent when not configured).
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, upload_job_artifacts, output_dir, job_id)

        # Find result JSON
        json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
        if not json_files:
            # Backward-compat rescue if outputs were written to OUTPUT_DIR root
            if _relocate_root_job_artifacts(job_id, output_dir):
                json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
        if json_files:
            target_json = json_files[0]
            with open(target_json, 'r') as f:
                data = json.load(f)

            # Enhance result with video URLs
            base_name = os.path.basename(target_json).replace('_metadata.json', '')
            clips = data.get('shorts', [])
            cost_analysis = data.get('cost_analysis')

            rendered, missing = _clips_actually_rendered(
                job_id, output_dir, base_name, clips)
            if not rendered:
                # Nothing to hand over: fail the job.
                jobs[job_id]['status'] = 'failed'
                jobs[job_id]['logs'].append(
                    "No clips could be rendered from this video.")
            else:
                if missing:
                    jobs[job_id]['logs'].append(
                        f"⚠️ {missing} of {len(clips)} clips failed to render.")
                jobs[job_id]['result'] = {'clips': rendered, 'cost_analysis': cost_analysis}
        else:
            jobs[job_id]['status'] = 'failed'
            jobs[job_id]['logs'].append("No metadata file generated.")
    else:
        jobs[job_id]['status'] = 'failed'
        jobs[job_id]['logs'].append(_scrub_secrets(f"Process failed with exit code {returncode}"))


def _kill_tree(process):
    """Kill main.py and what it started (yt-dlp, ffmpeg), so a stopped job
    leaves no encoder running on its own."""
    try:
        import psutil
        children = psutil.Process(process.pid).children(recursive=True)
    except Exception:
        children = []
    for p in (process, *children):
        try:
            p.kill()
        except Exception:
            pass


async def _run_main(job_id, cmd, env, tick=None):
    """Run a main.py command of ``job_id`` to its end and return its exit
    code: output lines into the job's log, the manifest heartbeated, ``tick``
    called every couple of seconds. Cancelled (the server is stopping): the
    child is killed so it cannot keep writing into the job dir while the next
    instance resumes the same job."""
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, # Merge stderr to stdout
        env=env,
        cwd=os.getcwd(),
        preexec_fn=_lower_priority,
    )
    # We need to capture logs in a thread because Popen isn't async
    t_log = threading.Thread(target=enqueue_output, args=(process.stdout, job_id))
    t_log.daemon = True
    t_log.start()

    last_heartbeat = time.time()
    while process.poll() is None:
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            try:
                process.kill()
            except Exception:
                pass
            raise
        if (jobs.get(job_id) or {}).get('stop_requested'):
            _kill_tree(process)
        if time.time() - last_heartbeat >= HEARTBEAT_EVERY:
            _touch_manifest(job_id)
            last_heartbeat = time.time()
        if tick is not None:
            tick()
    # The exit code can be read before the reader thread saw the last lines.
    await asyncio.to_thread(t_log.join, 5)
    return process.returncode


async def run_job(job_id, job_data):
    """Executes the subprocess for a specific job."""
    cmd = job_data['cmd']
    env = _job_encode_env(job_data['env'])
    # Each Claude call of the job is recorded in its own dir (claude_cli).
    env["USAGE_LOG"] = os.path.join(job_data['output_dir'], _USAGE_FILE)
    output_dir = job_data['output_dir']

    if job_data.get('stop_requested'):
        # Stopped while it waited in line: never start it.
        jobs[job_id]['status'] = 'failed'
        return
    jobs[job_id]['status'] = 'processing'
    jobs[job_id]['started_at'] = time.time()
    jobs[job_id]['logs'].append("Job started by worker.")
    print(f"🎬 [run_job] Executing command for {job_id}: {' '.join(cmd)}")

    try:
        returncode = await _run_main(
            job_id, cmd, env, tick=lambda: _refresh_partial_result(job_id, output_dir))
        _finish_job(job_id, returncode, output_dir)
    except Exception as e:
        jobs[job_id]['status'] = 'failed'
        # Exception text can embed URLs with credentials (e.g. the proxy URL
        # inside a yt-dlp/httpx error) — scrub before it reaches client logs.
        jobs[job_id]['logs'].append(_scrub_secrets(f"Execution error: {str(e)}"))


@app.get("/health")
async def health():
    """Lightweight liveness probe for uptime monitoring."""
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready():
    """Readiness probe for the Docker HEALTHCHECK (Dockerfile). Traefik's docker
    provider drops a container from the load balancer as soon as it turns
    unhealthy, so answering 503 from the moment SIGTERM arrives pulls this
    instance out of rotation while it can still serve, instead of after its
    socket is gone. Only SIGTERM flips it: a drain triggered by the instance
    marker starts while the new container is still booting, and going
    unready then would leave nobody routable."""
    if _stopping:
        return JSONResponse({"status": "stopping"}, status_code=503)
    return {"status": "ready"}

@app.get("/api/config")
async def get_config():
    return {
        "youtubeUrlEnabled": not DISABLE_YOUTUBE_URL,
        "jobRetentionSeconds": JOB_RETENTION_SECONDS,
        # Self-host only: tells the dashboard the Gemini key is optional
        # because the moment picker runs on an OpenAI-compatible server.
        "localLlm": llm_backend.describe(),
    }

async def _probe_youtube_quality(url: str) -> dict:
    """What the source offers ({max_height, duration, ...}); {} on any failure
    (fail-open). The metering probe a moment ago already asked YouTube for
    this very video: its answer is reused, so a submission costs the account
    one request here instead of two. Otherwise quality_probe.py runs in a
    worker thread."""
    try:
        facts = None
    except Exception:
        facts = None
    if facts:
        return facts

    def _run():
        try:
            proc = subprocess.run(
                [sys.executable, QUALITY_PROBE_SCRIPT, "--url", url],
                capture_output=True, timeout=75, env=child_env(),
            )
            return json.loads(proc.stdout.decode(errors="replace").strip() or "{}")
        except Exception as e:
            print(f"⚠️ Quality probe failed ({e}); starting job without gate.")
            return {}

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def _validate_source_url(url: str):
    """400 unless ``url`` is safe to hand to yt-dlp: http(s) on a globally
    routable host (security_utils) and, on YouTube, one video rather than a
    search / playlist / channel page (yt_clients). The download and the
    metering probe check the same things again; this is the one in front of
    the quality probe, which used to have none."""
    from security_utils import assert_public_url, UnsafeURLError
    from yt_clients import youtube_non_video_reason
    try:
        await asyncio.get_event_loop().run_in_executor(None, assert_public_url, url)
    except UnsafeURLError as e:
        raise HTTPException(status_code=400, detail=f"This URL can't be processed: {e}")
    reason = youtube_non_video_reason(url)
    if reason:
        raise HTTPException(status_code=400, detail=(
            f"This link is {reason}. Paste the link of one video "
            "(youtube.com/watch?v=... or youtu.be/...)."))


async def _quality_gate(url: str, force_low: bool):
    """Run the pre-flight probe: raises 400 for a too-short source, returns
    the needs_confirmation response for a low-resolution one, else None."""
    probe = await _probe_youtube_quality(url)
    # Hard reject, no confirm-and-retry: a too-short source fails the same
    # way on every retry, so letting the user force it just burns the job.
    source_duration = int(probe.get("duration") or 0)
    if MIN_SOURCE_SECONDS > 0 and 0 < source_duration < MIN_SOURCE_SECONDS:
        _reject_short_source(source_duration)
    max_height = int(probe.get("max_height") or 0)
    if not force_low and QUALITY_GATE_MIN_HEIGHT > 0 \
            and 0 < max_height < QUALITY_GATE_MIN_HEIGHT:
        print(f"⚠️ Quality gate: only {max_height}p available for {url} — asking user first.")
        return JSONResponse({
            "needs_confirmation": True,
            "quality_check": {
                "max_height": max_height,
                "min_height": QUALITY_GATE_MIN_HEIGHT,
                "cookies_invalid": bool(probe.get("cookies_invalid")),
            },
        })
    return None


async def _drop_unstarted_job(job_output_dir):
    """Undo a refused submission."""
    _end_submission(os.path.basename(str(job_output_dir or "").rstrip(os.sep)))
    _end_job_dir(os.path.basename(str(job_output_dir or "").rstrip(os.sep)))
    shutil.rmtree(job_output_dir, ignore_errors=True)


MAX_UPLOAD_NAME_BYTES = 120


def _clamp_upload_name(name: str, max_bytes: int = MAX_UPLOAD_NAME_BYTES) -> str:
    """Shorten a client-supplied file name so "<id>_<name>" fits the 255-byte
    limit most filesystems put on a name. The limit is in BYTES, not characters,
    and emoji or math-alphabet letters take 4 each. The extension is kept and
    the cut never lands inside a multi-byte character."""
    if len(name.encode("utf-8")) <= max_bytes:
        return name
    stem, ext = os.path.splitext(name)
    if len(ext.encode("utf-8")) > 16:
        stem, ext = name, ""
    room = max_bytes - len(ext.encode("utf-8"))
    stem = stem.encode("utf-8")[:room].decode("utf-8", "ignore").rstrip()
    return (stem or "upload") + ext


def _media_duration_seconds(path: str) -> float:
    """Container duration via ffprobe; 0.0 on any failure (fail-open)."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, timeout=30)
        return float(proc.stdout.decode().strip() or 0)
    except Exception:
        return 0.0


def _reject_short_source(duration: float):
    raise HTTPException(status_code=400, detail=(
        f"This video is only {int(duration)}s long — clip generation needs at "
        f"least {MIN_SOURCE_SECONDS}s of material to cut from. It already is "
        f"short-form content."))


# Layouts the caller can let the renderer choose from, mapped to the env var
# each one is gated on. The renderer only ever picks between layouts that are
# switched on here.
#
# This is opt-in per job, not a detector running on every video, because the
# detection is not good enough to be trusted unprompted: measured over the
# 48-clip corpus, routing every video through the on-screen-content check fixed
# 13 clips and spoiled 13 others (talking heads and corner tickers demoted to a
# layout they do not need). Asking the person who knows what they uploaded costs
# them one click and removes that whole class of error. It is also what OpusClip
# does — its "applicable auto layout" panel lets the user pick which layouts the
# AI may apply.
LAYOUT_ENV = {
    "split": "SPLIT_LAYOUT",          # two speakers stacked
    "screencast": "SCREENCAST_LAYOUT",  # slides/screen share over the speaker
    "speaker_cut": "SPEAKER_CUT",     # hard cuts to whoever is talking
    "punch_in": "PUNCH_IN",           # small push on the clip's beats
}

# Stacking and cutting both need to know who is speaking.
LAYOUT_IMPLIES = {
    "split": ["SPEAKER_SIGNAL"],
    "speaker_cut": ["SPEAKER_SIGNAL"],
}


def layout_env(requested):
    """Env overrides for the layouts this job allows. Unknown names are ignored
    rather than rejected: a newer dashboard must not break an older API.

    The special value "auto" hands the choice to Gemini (one call per video).
    It composes with explicit picks: layout_picker only ever adds, so asking for
    "auto,punch_in" means "decide the layout yourself, and punch in regardless".
    "none" is the opposite: it switches the picker OFF for this job even when
    the deployment runs with AUTO_LAYOUT=1, for the user who wants the plain
    single crop and nothing clever.
    """
    env = {}
    for name in requested or []:
        key = str(name).strip().lower()
        if key == "auto":
            env["AUTO_LAYOUT"] = "1"
            continue
        if key == "none":
            env["AUTO_LAYOUT"] = "0"
            continue
        var = LAYOUT_ENV.get(key)
        if not var:
            continue
        env[var] = "1"
        for extra in LAYOUT_IMPLIES.get(key, []):
            env[extra] = "1"
    return env


@app.post("/api/process")
async def process_endpoint(
    request: Request,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    acknowledged: Optional[str] = Form(None),
    output_format: Optional[str] = Form(None),
    layouts: Optional[str] = Form(None),
    force_low_quality: Optional[str] = Form(None),
    target_clips: Optional[str] = Form(None),
    clip_min_seconds: Optional[str] = Form(None),
    clip_max_seconds: Optional[str] = Form(None),
    auto_hook: Optional[str] = Form(None),
    auto_hook_style: Optional[str] = Form(None),
    thumbnail_session_id: Optional[str] = Form(None),
    captions: Optional[str] = Form(None),
    source_start: Optional[str] = Form(None),
    source_end: Optional[str] = Form(None),
):
    ack_flag = str(acknowledged).lower() in ("1", "true", "yes")
    force_low = str(force_low_quality).lower() in ("1", "true", "yes")

    # Handle JSON body manually for URL payload
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        url = body.get("url")
        ack_flag = bool(body.get("acknowledged"))
        force_low = bool(body.get("force_low_quality"))
        output_format = body.get("output_format")
        layouts = body.get("layouts")
        target_clips = body.get("target_clips")
        clip_min_seconds = body.get("clip_min_seconds")
        clip_max_seconds = body.get("clip_max_seconds")
        auto_hook = body.get("auto_hook")
        auto_hook_style = body.get("auto_hook_style")
        thumbnail_session_id = body.get("thumbnail_session_id")
        captions = body.get("captions")
        source_start = body.get("source_start")
        source_end = body.get("source_end")

    # Normalize output format (auto = keep pipeline default).
    if output_format not in ("vertical", "horizontal", "square"):
        output_format = "auto"

    # Accepts a JSON list or a comma-separated form field.
    if isinstance(layouts, str):
        layouts = [p for p in layouts.split(",") if p.strip()]
    elif not isinstance(layouts, list):
        layouts = []

    # Module handover (issue #68): reuse the Thumbnail Studio source video and
    # its transcript so publishing to YouTube can flow straight into clip
    # generation without re-uploading or re-transcribing.
    thumb_session = None
    if thumbnail_session_id and not url and not file:
        thumb_session = thumbnail_sessions.get(thumbnail_session_id)
        if not thumb_session:
            raise HTTPException(status_code=404, detail="Thumbnail session not found or expired")
        src = thumb_session.get("video_path")
        if not src or not os.path.exists(src):
            raise HTTPException(status_code=404, detail="Source video for this session is no longer on disk")

    if not url and not file and not thumb_session:
        raise HTTPException(status_code=400, detail="Must provide URL or File")

    if not ack_flag:
        raise HTTPException(status_code=400, detail="You must confirm you own the content or have rights to process it.")

    if url and DISABLE_YOUTUBE_URL:
        raise HTTPException(status_code=403, detail="YouTube URL ingest is disabled on this deployment. Please upload a file you own.")

    # Refuse a URL no server-side fetch may touch before anything fetches it:
    # the quality probe below used to run yt-dlp on it unvalidated (tailnet
    # hosts included) and ahead of the balance / rate checks.
    if url:
        await _validate_source_url(url)

    # Capture attestation context for legal record (IP + timestamp + UA)
    client_ip = request.client.host if request.client else "unknown"
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        client_ip = fwd.split(",")[0].strip()
    user_agent = request.headers.get("user-agent", "")
    attestation = {
        "acknowledged": True,
        "ip": client_ip,
        "user_agent": user_agent,
        "timestamp": time.time(),
        "source": ("thumbnail_session" if thumb_session else "url" if url else "file"),
    }

    job_id = str(uuid.uuid4())
    job_output_dir = os.path.join(OUTPUT_DIR, job_id)
    _begin_job_dir(job_id)  # the sweeps keep its dir and source until it is in `jobs`
    os.makedirs(job_output_dir, exist_ok=True)

    # Prepare Command
    # sys.executable, not "python": bare "python" resolves against PATH, which
    # outside Docker is whatever interpreter happens to be first — not the venv
    # running this server. Every job then dies on `import cv2`. The quality
    # probe above already gets this right.
    cmd = [sys.executable, "-u", "main.py"] # -u for unbuffered
    env = child_env()
    # The stdio fix above only covers this process. main.py prints an emoji on
    # its first line and configures nothing, so on a cp1252 console the child
    # still dies before it renders anything -- the server starts and every job
    # fails instead. setdefault, so an explicit PYTHONIOENCODING still wins.
    env.setdefault("PYTHONIOENCODING", "utf-8")

    # Optional layouts are per job. The renderer reads these at import time in
    # the subprocess, so they must be set before Popen — same path WATERMARK
    # already takes.
    chosen = layout_env(layouts)
    env.update(chosen)
    if chosen:
        print(f"[layouts] job={job_id} enabled={sorted(chosen)}")

    # Auto-hook: burn each clip's Gemini hook text during the render. Off when
    # the field is absent, so API callers keep their old output
    # byte-for-byte; the dashboard sends an explicit value either way.
    if str(auto_hook).lower() in ("1", "true", "yes"):
        env["AUTO_HOOK"] = "1"
        from hooks import HOOK_STYLES
        if auto_hook_style in HOOK_STYLES:
            env["AUTO_HOOK_STYLE"] = auto_hook_style
        print(f"[auto-hook] job={job_id} style={env.get('AUTO_HOOK_STYLE', 'classic')}")

    # Manual generation controls (discussion #65): optional clip-count target
    # and duration band, forwarded to the selection prompts via the same env
    # overrides the A/B harness already reads (clip_selection.py). All three
    # are honest TARGETS, not guarantees — the model may return fewer clips
    # when the material doesn't hold them. Bad values 400 instead of silently
    # producing something the user didn't ask for.
    def _gen_control(raw, name, lo, hi, integer=False):
        if raw in (None, ""):
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{name} must be a number")
        if integer and val != int(val):
            raise HTTPException(status_code=400, detail=f"{name} must be an integer")
        if not (lo <= val <= hi):
            raise HTTPException(status_code=400,
                                detail=f"{name} must be between {lo:g} and {hi:g}")
        return int(val) if integer else val

    n_clips = _gen_control(target_clips, "target_clips", 1, 15, integer=True)
    min_secs = _gen_control(clip_min_seconds, "clip_min_seconds", 5, 175)
    max_secs = _gen_control(clip_max_seconds, "clip_max_seconds", 10, 180)
    if min_secs is not None and max_secs is not None and max_secs < min_secs + 5:
        raise HTTPException(status_code=400,
                            detail="clip_max_seconds must be at least 5s above clip_min_seconds")
    if n_clips is not None:
        env["CLIP_TARGET_MIN"] = env["CLIP_TARGET_MAX"] = str(n_clips)
    if min_secs is not None:
        env["CLIP_MIN_SECONDS"] = str(min_secs)
    if max_secs is not None:
        env["CLIP_MAX_SECONDS"] = str(max_secs)
    if n_clips is not None or min_secs is not None or max_secs is not None:
        print(f"[gen-controls] job={job_id} clips={n_clips} band={min_secs}-{max_secs}")

    # captions=false: the source already carries burned-in subtitles (or the
    # caller adds its own later), so skip the free auto-caption pass instead
    # of stacking a second layer. Absent → the deployment default (on).
    if captions is not None and str(captions).lower() in ("0", "false", "no"):
        env["AUTO_CAPTIONS"] = "0"
        print(f"[captions] job={job_id} auto-captions off")

    input_path = None
    if url:
        # Keep the downloaded source inside the job dir: the clip editor's
        # re-render path cuts new segments from it, and it ages out with the
        # rest of the job (retention window + OUTPUT_MAX_GB cap) either way.
        cmd.extend(["-u", url, "--keep-original"])
    elif thumb_session:
        # Hardlink (or copy) the session's video under the job's name so source
        # lookup, the clip editor and the preview treat it exactly like a normal
        # upload; the transcript rides along so the pipeline skips Whisper.
        src = thumb_session["video_path"]
        src_duration = _media_duration_seconds(src)
        if MIN_SOURCE_SECONDS > 0 and 0 < src_duration < MIN_SOURCE_SECONDS:
            _discard_rejected_source(None, job_output_dir)
            _reject_short_source(src_duration)
        input_path = os.path.join(UPLOAD_DIR, f"{job_id}_{_clamp_upload_name(os.path.basename(src))}")
        try:
            os.link(src, input_path)
        except OSError:
            shutil.copyfile(src, input_path)
        cmd.extend(["-i", input_path])
        # An empty transcript (e.g. a silent or music-only source) is not worth
        # forwarding: main.py would reject it and retranscribe anyway.
        if thumb_session.get("transcript_ready") and (thumb_session.get("transcript") or {}).get("segments"):
            transcript_path = os.path.join(job_output_dir, "source_transcript.json")
            with open(transcript_path, "w") as f:
                json.dump(thumb_session["transcript"], f)
            cmd.extend(["--transcript", transcript_path])
    else:
        # Save uploaded file with size limit check.
        # basename() strips any path components from the client-supplied
        # filename so a name like "../../main.py" can't escape UPLOAD_DIR.
        safe_name = _clamp_upload_name(os.path.basename(file.filename or "upload") or "upload")
        input_path = os.path.join(UPLOAD_DIR, f"{job_id}_{safe_name}")

        # Read file in chunks to check size
        size = 0
        limit_bytes = MAX_FILE_SIZE_MB * 1024 * 1024

        with open(input_path, "wb") as buffer:
            while content := await file.read(1024 * 1024): # Read 1MB chunks
                size += len(content)
                if size > limit_bytes:
                    _discard_rejected_source(input_path, job_output_dir)
                    raise HTTPException(status_code=413, detail=f"File too large. Max size {MAX_FILE_SIZE_MB}MB")
                buffer.write(content)

        upload_duration = _media_duration_seconds(input_path)
        if MIN_SOURCE_SECONDS > 0 and 0 < upload_duration < MIN_SOURCE_SECONDS:
            _discard_rejected_source(input_path, job_output_dir)
            _reject_short_source(upload_duration)

        cmd.extend(["-i", input_path])

    cmd.extend(["-o", job_output_dir])
    if output_format and output_format != "auto":
        cmd.extend(["--format", output_format])

    print(f"[attestation] job={job_id} ip={attestation['ip']} source={attestation['source']} ack=true")

    user_id, priority = None, 2
    partial = _self_host_range(_parse_source_range(source_start, source_end))

    # Pre-flight quality gate: probe the offered resolution BEFORE starting, so
    # the user can abort (refresh cookies / update yt-dlp) instead of burning
    # 20 min on a 360p-only source. Fail-open: any probe error starts normally.
    # The probe also runs under force_low_quality so the short-source check
    # can't be bypassed through the quality-gate confirm.
    if url and (QUALITY_GATE_MIN_HEIGHT > 0 or MIN_SOURCE_SECONDS > 0):
        try:
            gate = await _quality_gate(url, force_low)
        except BaseException:
            await _drop_unstarted_job(job_output_dir)
            raise
        if gate is not None:
            await _drop_unstarted_job(job_output_dir)
            return gate

    # A metered URL job never processes more than it paid for: the duration
    # came from the metering probe, but the download is a second request and
    # a server can answer it with a much longer file. main.py cuts anything
    # clearly past the reserved minutes (partial jobs already carry a cut).
    source_cap = getattr(request.state, "reserved_minutes", None)
    if url and source_cap and not partial:
        env["SOURCE_CAP_MINUTES"] = str(source_cap)
    else:
        env.pop("SOURCE_CAP_MINUTES", None)
        source_cap = None
    skip_statics = bool(url) and bool(getattr(request.state, "skip_statics", False))
    if skip_statics:
        env["DOWNLOAD_SKIP_STATICS"] = "1"
    else:
        env.pop("DOWNLOAD_SKIP_STATICS", None)
    if partial:
        # main.py cuts the source down to this many minutes before anything
        # reads it, so the whole pipeline (and the editor) sees a short video.
        _apply_source_cut(env, partial)

    # What the user asked for, shown on the job list. Every value is already
    # validated above: numbers, a closed format and the known layout names.
    settings = {
        "format": output_format,
        "clips": n_clips, "min_seconds": min_secs, "max_seconds": max_secs,
        "layouts": sorted({str(n).strip().lower() for n in layouts}
                          & (set(LAYOUT_ENV) | {"auto", "none"})),
        "hook": env.get("AUTO_HOOK_STYLE", "classic") if env.get("AUTO_HOOK") == "1" else None,
        "captions": env.get("AUTO_CAPTIONS") != "0",
    }

    # Enqueue Job
    _end_submission(job_id)  # from here the job counts in `jobs` itself
    jobs[job_id] = {
        'status': 'queued',
        'logs': _TimedLog([f"Job {job_id} queued."]),
        'cmd': cmd,
        'env': env,
        'output_dir': job_output_dir,
        'attestation': attestation,
        # What the job list shows: the link, or the uploaded file's name.
        'label': url or (file.filename if file else None),
        'settings': settings,
        'user_id': user_id,
        'partial': partial,
        # The submission time that orders the queue; it travels in the resume
        # manifest.
        'submitted_at': _arrival_now(),
    }
    _end_job_dir(job_id)  # `jobs` keeps it live now

    # Resume manifest: enough to re-run this job if the container dies mid-flight
    # (a redeploy). No secrets — the env is rebuilt from os.environ on resume.
    _write_resume_manifest(job_id, cmd, priority, user_id,
                           partial=partial,
                           source_cap_minutes=source_cap, skip_statics=skip_statics,
                           submitted_at=jobs[job_id]['submitted_at'], label=jobs[job_id]['label'],
                           settings=settings)

    _enqueue_job(job_id, priority)

    return {"job_id": job_id, "status": "queued", "partial": partial,
            "first_video": bool(getattr(request.state, "first_video", False))}

def _job_view_from_disk(job_id):
    """What the disk says about a job this instance does not hold in memory.

    During a deploy handover a poll can land on either instance; the one that
    did not accept the job must still answer from the shared directory: a
    metadata JSON means completed (recovered like at startup), a manifest
    means the other instance has it (heartbeat) or will pick it up (queued).
    """
    job_path = os.path.join(OUTPUT_DIR, job_id)
    if not os.path.isdir(job_path):
        return None
    m = _read_manifest(job_id)
    if m is None:
        if glob.glob(os.path.join(job_path, "*_metadata.json")):
            _recover_jobs_from_disk()
            return jobs.get(job_id)
        return None
    alive = time.time() - float(m.get("heartbeat") or 0) < HEARTBEAT_STALE_AFTER
    owner = m.get("user_id")
    return {
        # Rendered, delivery pending: the next instance finishes it, no queue.
        'status': 'processing' if alive or m.get("stage") == "delivering" else 'queued',
        'logs': _TimedLog(["♻️ The server was updated; your video continues on the new instance."]),
        'user_id': (int(owner) if isinstance(owner, str) and owner.isdigit() else owner),
        'result': None,
    }


def _presented_status(job_id, job):
    """A job we hold as 'queued' while draining is really the next instance's:
    if it has started it, say so instead of showing a queue that never moves."""
    if job.get('status') == 'queued' and _draining:
        m = _read_manifest(job_id)
        if m and _manifest_busy_elsewhere(m):
            return 'processing'
    return job['status']


@app.get("/api/status/{job_id}")
async def get_status(job_id: str, request: Request):
    job = jobs.get(job_id)
    if job is None:
        job = _job_view_from_disk(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    _logs_view = _visible_logs_timed(job['logs'])
    return {
        "status": _presented_status(job_id, job),
        "logs": _logs_view[0],
        # When each line arrived (epoch seconds, parallel to "logs"), or None.
        "log_times": _logs_view[1],
        "result": job.get('result'),
        # Position in line and a rough wait while the job is still queued.
        "queue": queue_snapshot(job_id),
        # Set when only the first part of the source was clipped (quota wall
        # offer), so the dashboard can say so next to the clips.
        "partial": job.get('partial'),
        "usage": _job_usage(job),
    }


def _job_title(job):
    """The source file name main.py recorded once the job finished, or None.
    Read once and kept on the record."""
    if 'title' not in job:
        job['title'] = None
        try:
            meta = glob.glob(os.path.join(job['output_dir'], "*_metadata.json"))
            if meta:
                with open(meta[0]) as f:
                    name = json.load(f).get('source_video')
                if isinstance(name, str):
                    job['title'] = os.path.basename(name)
        except (OSError, KeyError, ValueError):
            pass
    return job['title']


_USAGE_FILE = ".usage.jsonl"


def _job_usage(job):
    """Tokens and calls of one job, per task (claude_cli.summarize), or None."""
    path = os.path.join(job.get('output_dir') or "", _USAGE_FILE)
    if not job.get('output_dir') or not os.path.exists(path):
        return None
    import claude_cli
    return claude_cli.summarize(claude_cli.read_usage(path))


def _job_summary(job_id, job):
    """One row of GET /api/jobs: what was asked, where it stands, how it went."""
    status = _presented_status(job_id, job)
    clips = (job.get('result') or {}).get('clips') or []
    queue = queue_snapshot(job_id)
    try:
        at = float(job.get('submitted_at') or os.path.getmtime(job['output_dir']))
    except (OSError, KeyError, TypeError, ValueError):
        at = None
    lines, times = _visible_logs_timed(job.get('logs') or [])
    partial = job.get('partial') or {}
    row = {
        "job_id": job_id,
        "status": status,
        "label": job.get('label'),
        "title": _job_title(job) if status == 'completed' else None,
        "settings": job.get('settings'),
        "range": ({"start": partial.get('start_seconds'), "end": partial.get('end_seconds')}
                  if partial.get('end_seconds') is not None else None),
        "submitted_at": at,
        "started_at": job.get('started_at'),
        "finished_at": times[-1] if times and status in ('completed', 'failed') else None,
        "clips": len(clips),
        "position": queue["position"] if queue else None,
        "eta_seconds": queue["eta_seconds"] if queue else None,
        "stopping": bool(job.get('stop_requested')) and status in ('queued', 'processing'),
        "stopped": bool(job.get('stop_requested')) and status == 'failed',
        "error": None,
        "usage": _job_usage(job),
        # The running job's log: the dashboard reads its stage and clip count.
        "logs": lines[-400:] if status == 'processing' else None,
    }
    if status == 'failed' and job.get('stop_requested'):
        row["error"] = "Stopped before it finished."
    elif status == 'failed':
        try:
            row["error"] = (_job_error_text(lines) or (lines[-1] if lines else ''))[-300:]
        except Exception:
            row["error"] = None
    return row


class ModelPicks(BaseModel):
    # task -> model name, or None to go back to the default.
    models: Dict[str, Optional[str]]


@app.get("/api/settings/models")
async def get_model_settings():
    """The model each kind of Claude call runs on, and what can be picked.

    shortcut: self-host only, like /api/jobs: one operator owns the server."""
    import claude_cli
    picked = claude_cli.model_overrides()
    return {
        "tasks": [{"id": t, "label": label, "help": help_, "default": claude_cli.TASKS[t][2],
                   "picked": picked.get(t), "model": claude_cli.model_for(t)}
                  for t, (label, help_, _default) in claude_cli.TASKS.items()],
        "choices": claude_cli.MODEL_CHOICES,
    }


@app.put("/api/settings/models")
async def put_model_settings(body: ModelPicks):
    """Pick a model per task; None restores the default. Takes effect on the
    next call, running jobs included."""
    import claude_cli
    picks = claude_cli.model_overrides()
    for task, model in body.models.items():
        if task not in claude_cli.TASKS:
            raise HTTPException(status_code=400, detail=f"Unknown task: {task[:40]}")
        if model is None or model == "":
            picks.pop(task, None)
        elif claude_cli.valid_model(model):
            picks[task] = model
        else:
            raise HTTPException(status_code=400, detail="Not a model name")
    write_json_atomic(claude_cli.SETTINGS_FILE, picks)
    return await get_model_settings()


@app.get("/api/usage")
async def get_usage(days: float = 7):
    """Claude calls of the last ``days`` on this server: totals, per task and
    per day (tokens count against the subscription's usage limit)."""
    import claude_cli
    since = time.time() - max(0.0, min(days, 90.0)) * 86400
    records = claude_cli.read_usage(claude_cli.USAGE_FILE, since)
    by_day = {}
    for r in records:
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(float(r.get("at"))))
        except (TypeError, ValueError, OverflowError):
            continue
        by_day.setdefault(day, []).append(r)
    return {"since": since, **claude_cli.summarize(records),
            "days": [{"day": d, **{k: v for k, v in claude_cli.summarize(rs).items() if k != "tasks"}}
                     for d, rs in sorted(by_day.items())]}


@app.get("/api/jobs")
async def list_jobs():
    """Every job this server holds, newest first: the dashboard's queue page,
    so queued, running and finished jobs stay reachable after "new video"."""
    out = [_job_summary(job_id, job) for job_id, job in list(jobs.items())]
    out.sort(key=lambda j: j["submitted_at"] or 0, reverse=True)
    return {"jobs": out}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str, request: Request):
    """Delete a finished, failed or stopped job: it leaves the list and its
    folder in output/ (clips, source, sidecars) and uploaded file are deleted.
    A queued or running job is stopped first (POST .../stop)."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if (_presented_status(job_id, job) in ('queued', 'processing')
            or job_id in _running_jobs):
        raise HTTPException(status_code=409, detail="Stop the job before deleting it.")
    jobs.pop(job_id, None)
    _discard_job_files(job_id)
    return {"status": "deleted"}


@app.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str, request: Request):
    """Stop a queued or running job for good: main.py and its children are
    killed within ~2 s (``_run_main``), and it ends failed with no auto-retry."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get('status') not in ('queued', 'processing'):
        raise HTTPException(status_code=409, detail="This job is not running.")
    if not job.get('stop_requested'):
        job['stop_requested'] = True
        job['logs'].append("⏹️ Stopped. No clips will be made from this run.")
    if job.get('status') == 'queued' and _take_off_queue(job_id):
        # Still waiting in line: it leaves the queue now instead of when it
        # reaches a slot, which with one slot can be hours away.
        job['status'] = 'failed'
        _clear_resume_manifest(job_id)
        _discard_job_files(job_id, job)
        return {"status": "removed"}
    # Running, or already taken by the worker: run_job / _run_main end it.
    return {"status": "stopping"}


def _take_off_queue(job_id) -> bool:
    """Drop a waiting job's entry from the queue; False if it is not there."""
    heap = job_queue._queue
    kept = [e for e in heap if e[2] != job_id]
    if len(kept) == len(heap):
        return False
    heap[:] = kept
    heapq.heapify(heap)
    try:
        job_queue.task_done()  # its entry was put once and will not be got
    except ValueError:
        pass
    _queue_changed()
    return True


def _locate_source(job_id: str):
    """Find a job's source video on disk, or None.

    Upload jobs keep it in uploads/{job_id}_*; URL jobs keep the download in
    the job dir (--keep-original) under the name recorded as ``source_video``
    in metadata.json. Either way it ages out with the normal retention caps.
    """
    matches = [
        f for f in glob.glob(os.path.join(UPLOAD_DIR, f"{glob.escape(job_id)}_*"))
        if not os.path.basename(f).startswith("thumb_")
    ]
    if matches:
        return matches[0]
    try:
        meta_files = glob.glob(os.path.join(OUTPUT_DIR, job_id, "*_metadata.json"))
        if meta_files:
            with open(meta_files[0], 'r') as f:
                name = json.load(f).get('source_video')
            if name:
                candidate = os.path.join(OUTPUT_DIR, job_id, os.path.basename(name))
                if os.path.exists(candidate):
                    return candidate
    except Exception:
        pass
    return None


def _source_url(job_id: str) -> str:
    """The URL to hand a `<video>` tag for this job's source."""
    return f"/api/source/{job_id}"


@app.get("/api/source-url/{job_id}")
async def get_source_url(job_id: str, request: Request):
    """The URL the dashboard's player loads this job's source from."""
    return {"url": _source_url(job_id)}


@app.get("/api/source/{job_id}")
async def get_source_video(job_id: str, request: Request):
    """Stream a job's original source video for the live-analysis preview and
    the clip editor's source monitor.

    Uploaded sources are blob URLs in the browser and don't survive a reload,
    so the recovered session points the preview here instead.
    """
    source_path = _locate_source(job_id)
    if not source_path:
        raise HTTPException(status_code=404, detail="Source not found")
    return FileResponse(source_path, media_type="video/mp4")


@app.get("/api/jobs/{job_id}/download-all")
async def download_all_clips(job_id: str, request: Request):
    """Bundle the current version of every clip of a job into one ZIP."""
    if job_id in jobs and jobs[job_id].get("user_id") is not None:
        pass
    else:
        pass

    output_dir = os.path.join(OUTPUT_DIR, job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Job not found")

    with open(json_files[0], 'r', encoding='utf-8') as f:
        data = json.load(f)

    # The metadata file on disk never carries video_url — the pipeline doesn't
    # write it, it's injected into the in-memory job record. So prefer the live
    # record (it also tracks edits like subtitled_/hook_ renames) and fall back
    # to the canonical name a job/restore rebuilds, instead of finding nothing.
    base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
    mem_clips = ((jobs.get(job_id) or {}).get('result') or {}).get('clips') or []

    files = []
    for i, clip in enumerate(data.get('shorts', [])):
        url = None
        if i < len(mem_clips):
            url = (mem_clips[i] or {}).get('video_url')
        url = url or clip.get('video_url')
        filename = (os.path.basename(url.split('/')[-1]) if url
                    else _canonical_clip_file(output_dir, base_name, i))
        path = os.path.join(output_dir, filename)
        if filename and os.path.exists(path):
            files.append((i, path))

    if not files:
        raise HTTPException(status_code=404, detail="No clip files found for this job")

    # One file per request: two downloads of the same job in the same second
    # used to share a timestamped name, so the second truncated the ZIP the
    # first was still streaming and the first cleanup deleted it under the second.
    zip_path = os.path.join(output_dir, f"clips_{uuid.uuid4().hex}.zip")

    def build_zip():
        # Videos are already compressed; store instead of deflate for speed.
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as zf:
            for i, path in files:
                zf.write(path, arcname=f"clip_{i + 1:02d}_{os.path.basename(path)}")

    def remove_zip():
        with suppress(FileNotFoundError):
            os.remove(zip_path)

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, build_zip)
    except BaseException:
        remove_zip()
        raise

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"shorts_maxxing_clips_{job_id[:8]}.zip",
        background=BackgroundTask(remove_zip),
    )


# Job ids are uuid4 strings.
_JOB_ID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


from editor import VideoEditor
from subtitles import generate_srt, generate_ass, burn_subtitles, generate_srt_from_video, CAPTION_PRESETS
from hooks import add_hook_to_video
from translate import translate_video
from thumbnail import (analyze_video_for_titles, refine_titles, generate_thumbnail,
                       generate_youtube_description, extract_face_frames)

class EditRequest(BaseModel):
    job_id: str
    clip_index: int
    api_key: Optional[str] = None
    input_filename: Optional[str] = None

@app.post("/api/edit")
async def edit_clip(
    req: EditRequest,
    request: Request,
):
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[req.job_id]
    if 'result' not in job or 'clips' not in job['result']:
        raise HTTPException(status_code=400, detail="Job result not available")

    try:
        # Resolve Input Path: Prefer explict input_filename from frontend (chaining edits)
        if req.input_filename:
            # Security: Ensure just a filename, no paths
            safe_name = os.path.basename(req.input_filename)
            input_path = os.path.join(OUTPUT_DIR, req.job_id, safe_name)
            filename = safe_name
        else:
            # Fallback to original clip
            clip = job['result']['clips'][req.clip_index]
            filename = clip['video_url'].split('/')[-1]
            input_path = os.path.join(OUTPUT_DIR, req.job_id, filename)
        
        if not os.path.exists(input_path):
             raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")

        # Edit the clip WITHOUT its burned captions, then put them back on top —
        # otherwise the captions are baked into the edit and the next subtitle
        # pass stacks a second layer over them (see _reapply_captions).
        clean_name = await asyncio.to_thread(
            _strip_burned_captions, os.path.join(OUTPUT_DIR, req.job_id), filename)
        had_captions = clean_name != filename
        if had_captions:
            filename = clean_name
            input_path = os.path.join(OUTPUT_DIR, req.job_id, clean_name)

        # Define output path for edited video
        edited_filename = f"edited_{filename}"
        output_path = os.path.join(OUTPUT_DIR, req.job_id, edited_filename)
        
        # Run editing in a thread to avoid blocking main loop
        # Since VideoEditor uses blocking calls (subprocess, API wait)
        def run_edit():
            editor = VideoEditor()
            
            # SAFE FILE RENAMING STRATEGY (Avoid UnicodeEncodeError in Docker)
            # Create a safe ASCII filename in the same directory. Unique per
            # request: with one name per job, two clips of a job edited at the
            # same time copied over each other's input while it was being
            # uploaded, deleted it under the other, and moved each other's
            # result (a clip could end up with another clip's edit, or none).
            token = uuid.uuid4().hex[:8]
            safe_filename = f"temp_input_{req.job_id}_{token}.mp4"
            safe_input_path = os.path.join(OUTPUT_DIR, req.job_id, safe_filename)
            safe_output_path = os.path.join(
                OUTPUT_DIR, req.job_id, f"temp_output_{req.job_id}_{token}.mp4")
            
            # Copy original file to safe path
            # (Copy is safer than rename if something crashes, we keep original)
            shutil.copy(input_path, safe_input_path)
            
            try:
                # 1. Upload (using safe path)
                vid_file = editor.sample_frames(safe_input_path)
                
                # 2. Get duration
                import cv2
                cap = cv2.VideoCapture(safe_input_path)
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                duration = frame_count / fps if fps else 0
                cap.release()
                
                # Load transcript from metadata
                transcript = None
                try:
                    meta_files = glob.glob(os.path.join(OUTPUT_DIR, req.job_id, "*_metadata.json"))
                    if meta_files:
                        with open(meta_files[0], 'r') as f:
                            data = json.load(f)
                            transcript = data.get('transcript')
                except Exception as e:
                    print(f"⚠️ Could not load transcript for editing context: {e}")

                # 3. Get Plan (Filter String)
                # Zooms would crop burned-in captions/hooks off screen, so tell
                # the editor when the source already carries them. `filename` is
                # the original clip name (safe_input_path is an ASCII temp copy).
                has_captions = ("subtitled_" in filename) or ("hook_" in filename) or ("hooked_" in filename)
                filter_data = editor.get_ffmpeg_filter(vid_file, duration, fps=fps, width=width, height=height, transcript=transcript, has_captions=has_captions)
                
                # 4. Apply (safe output name first)
                editor.apply_edits(safe_input_path, safe_output_path, filter_data)
                
                # Move result to final destination (rename works even if dest name has unicode if filesystem supports it, 
                # but python might still struggle if locale is broken? No, os.rename usually handles it better than subprocess args)
                # Actually, output_path is defined above: f"edited_{filename}"
                # If filename has unicode, output_path has unicode.
                # Let's hope shutil.move / os.rename works.
                # No file is a failed edit, not a finished one: answering 200
                # here left the clip pointing at a video that was never
                # written, and every later action on it (post, download,
                # restyle) failed on a missing file.
                if not os.path.exists(safe_output_path) or os.path.getsize(safe_output_path) == 0:
                    raise RuntimeError("the edit produced no video")
                shutil.move(safe_output_path, output_path)

                return filter_data
            finally:
                # Cleanup the temp copies, whatever happened.
                for temp_path in (safe_input_path, safe_output_path):
                    if os.path.exists(temp_path):
                        os.remove(temp_path)

        # Run in thread pool
        loop = asyncio.get_event_loop()
        plan = await loop.run_in_executor(None, run_edit)

        # Captions back on top, so the clip the user sees keeps them and the
        # clean edited file stays available for a later restyle.
        if had_captions:
            recap = await loop.run_in_executor(
                None, _reapply_captions, req.job_id, req.clip_index, output_path)
            if recap:
                edited_filename = os.path.basename(recap)

        edited_filename = edited_filename
        new_video_url = f"/videos/{req.job_id}/{edited_filename}"

        # Persist the new current file like /api/subtitle does: in-memory job
        # result + metadata.json, so reload/recovery/re-archive see this version.
        if req.clip_index < len(job['result']['clips']):
            job['result']['clips'][req.clip_index]['video_url'] = new_video_url
        try:
            meta_files = glob.glob(os.path.join(OUTPUT_DIR, req.job_id, "*_metadata.json"))
            if meta_files:
                with open(meta_files[0], 'r') as f:
                    meta = json.load(f)
                shorts = meta.get('shorts', [])
                if req.clip_index < len(shorts):
                    shorts[req.clip_index]['video_url'] = new_video_url
                    meta['shorts'] = shorts
                    write_json_atomic(meta_files[0], meta, indent=4)
        except Exception as e:
            print(f"⚠️ Failed to update metadata.json: {e}")


        return {
            "success": True,
            "new_video_url": new_video_url,
            "edit_plan": plan
        }

    except Exception as e:
        print(f"❌ Edit Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class CaptionWordIn(BaseModel):
    """One user-edited caption word, clip-relative ms — the same shape the
    /transcript endpoint hands the subtitle modal."""
    text: str
    startMs: int
    endMs: int


class SubtitleRequest(BaseModel):
    job_id: str
    clip_index: int
    position: str = "bottom" # top, middle, bottom
    font_size: int = 16
    font_name: str = "Verdana"
    font_color: str = "#FFFFFF"
    border_color: str = "#000000"
    border_width: int = 2
    bg_color: str = "#000000"
    bg_opacity: float = 0.0
    style: str = "classic"  # classic (uniform color) or karaoke (word highlight)
    highlight_color: str = "#FFD700"
    effect: str = "none"  # none | glow | pop | box (karaoke only)
    base_opacity: float = 1.0  # opacity of non-active words (dimmed modern look)
    uppercase: bool = False
    reveal: bool = False  # karaoke: words appear as they are spoken
    shadow: int = 0  # karaoke: drop shadow depth, 0-6
    # Words per line, as a character budget (1 = one word at a time). None
    # keeps the generator's defaults, which is what old clients send.
    max_chars: Optional[int] = None
    max_duration: Optional[float] = None
    # Named look (subtitles.CAPTION_PRESETS); any field sent explicitly
    # alongside it overrides that field of the preset.
    preset: Optional[str] = None
    input_filename: Optional[str] = None
    # User-edited caption words. When present, the burn uses them VERBATIM
    # instead of regenerating from the stored transcript — without this, text
    # edits in the modal were silently discarded on the server render path.
    words: Optional[List[CaptionWordIn]] = None


@app.get("/api/clip/{job_id}/{clip_index}/transcript")
async def get_clip_transcript(job_id: str, clip_index: int, request: Request):
    """Return word-level captions for a specific clip, formatted for Remotion."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    output_dir = os.path.join(OUTPUT_DIR, job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))

    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")

    with open(json_files[0], 'r') as f:
        data = json.load(f)

    transcript = data.get('transcript')
    if not transcript:
        raise HTTPException(status_code=400, detail="Transcript not found in metadata")

    clips = data.get('shorts', [])
    if clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")

    clip_data = clips[clip_index]

    # Recut clips are concatenations of source segments; remap the transcript
    # onto the clip timeline so every consumer keeps its flat start..end logic.
    recipe_segments = (clip_data.get('recipe') or {}).get('segments')
    if recipe_segments:
        transcript = recut.virtual_transcript(transcript, recipe_segments)
        clip_start, clip_end = 0.0, recut.total_duration(recipe_segments)
    else:
        clip_start = clip_data.get('start', 0)
        clip_end = clip_data.get('end', 0)

    # Extract words within clip range and convert to CaptionWord format
    captions = []
    for segment in transcript.get('segments', []):
        for word_info in segment.get('words', []):
            if word_info['end'] > clip_start and word_info['start'] < clip_end:
                captions.append({
                    "text": word_info.get('word', '').strip(),
                    "startMs": int((max(0, word_info['start'] - clip_start)) * 1000),
                    "endMs": int((max(0, word_info['end'] - clip_start)) * 1000),
                })

    duration_sec = clip_end - clip_start

    return {
        "captions": captions,
        "durationSec": duration_sec,
        "language": transcript.get('language', 'en'),
    }


# --- Clip editor: EDL + re-render ---

# The editor ships the WHOLE source transcript, not a window around the clip:
# the point of the source track is extending a cut into material the clip never
# covered, and you cannot pick a new in-point from words you were not sent.
# Cost is about 7 KB of JSON per minute of speech, fetched once per editor open.


def _source_duration_seconds(path):
    """Probe a video's duration; None when it can't be read."""
    try:
        import cv2
        cap = cv2.VideoCapture(path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        cap.release()
        # OpenCV reports -1/-1 for files it can't read — a naive truthiness
        # check would turn that into a phantom 1.0s duration.
        if fps > 0 and frames > 0:
            return round(frames / fps, 3)
    except Exception:
        pass
    return None


def _clip_recipe_parts(clip):
    """(segments, canonical_range) for a clip — synthesized from the flat
    start/end for clips that were never recut."""
    recipe = clip.get('recipe') or {}
    fallback = {"start": float(clip.get('start', 0) or 0),
                "end": float(clip.get('end', 0) or 0)}
    segments = recipe.get('segments') or [dict(fallback)]
    canonical_range = recipe.get('canonical_range') or dict(fallback)
    return segments, canonical_range


@app.get("/api/clip/{job_id}/{clip_index}/edl")
async def get_clip_edl(job_id: str, clip_index: int, request: Request):
    """The clip's editable recipe: which source segments it was cut from, the
    word timeline around them, and whether the source is still available for
    cuts outside the original range."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    output_dir = os.path.join(OUTPUT_DIR, job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
    with open(json_files[0], 'r') as f:
        data = json.load(f)

    clips = data.get('shorts', [])
    if clip_index < 0 or clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
    clip = clips[clip_index]

    segments, canonical_range = _clip_recipe_parts(clip)
    transcript = data.get('transcript') or {}
    words = recut.transcript_words(transcript)

    source_path = _locate_source(job_id)
    source_duration = _source_duration_seconds(source_path) if source_path else None
    duration_estimated = source_duration is None
    if duration_estimated:
        # Best remaining scale for the source track: the last spoken word or
        # the furthest point any recipe touches.
        candidates = [canonical_range['end']] + [s['end'] for s in segments]
        if words:
            candidates.append(words[-1]['e'])
        source_duration = round(max(candidates), 3)

    words_out = [
        {"w": w["w"], "s": round(w["s"], 3), "e": round(w["e"], 3)}
        for w in words
    ]

    current_file = (clip.get('video_url') or '').split('/')[-1]
    if not current_file:
        base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
        current_file = _canonical_clip_file(output_dir, base_name, clip_index)

    total = recut.total_duration(segments)
    return {
        "job_id": job_id,
        "clip_index": clip_index,
        "title": clip.get('video_title_for_youtube_short') or '',
        "segments": segments,
        "framing": (clip.get('recipe') or {}).get('framing') or 'auto',
        "canonical_range": canonical_range,
        "duration": total,
        "current_file": current_file,
        "has_captions": bool(re.match(r'^subtitled_\d+_',
                                      watermarked.clean_name(current_file))),
        "words": words_out,
        "source": {
            "available": bool(source_path),
            "url": _source_url(job_id) if (source_path) else None,
            "duration": source_duration,
            "duration_estimated": duration_estimated,
        },
        "limits": {
            "max_segments": recut.MAX_SEGMENTS,
            "min_segment_seconds": recut.MIN_SEGMENT_SECONDS,
            "max_total_seconds": recut.MAX_TOTAL_SECONDS,
        },
        "rerender_minutes": 0,
    }


class RerenderSegment(BaseModel):
    start: float
    end: float


class RerenderRequest(BaseModel):
    job_id: str
    clip_index: int
    segments: List[RerenderSegment]
    snap_to_words: bool = False
    reapply_captions: bool = True
    # None = inherit the recipe's framing (so plain trims keep the look);
    # 'auto' resets to the classifier; 'full'/'track' force a layout.
    framing: Optional[str] = None


# Manual framing -> reframe-engine strategy. 'full' shows the whole source
# frame (WIDE: no side-cropping, blurred filler bands); 'track' forces the
# subject-tracking crop. Anything non-auto needs the retained source video.
_FRAMING_STRATEGIES = {"auto": None, "full": "WIDE", "track": "TRACK"}


# One lock per job (same pattern as _restore_locks): rerenders on the same job
# share metadata.json and the canonical files, so they must not interleave.
_rerender_locks: Dict[str, asyncio.Lock] = {}

# Scene-listing builds write stable preview/thumbnail names per job; serialize
# them so overlapping editor opens don't tear each other's files.
_scenes_locks: Dict[str, asyncio.Lock] = {}


@app.post("/api/clip/rerender")
async def rerender_clip(req: RerenderRequest, request: Request):
    """Re-render a clip from an edited EDL (the clip editor's save button).

    Two paths, chosen automatically:
    - FAST: every segment stays inside the range the canonical clip was cut
      from → recut straight from the already-reframed canonical file. No ML,
      no source needed.
    - SOURCE: a segment reaches outside → recut from the retained source and
      re-reframe with the same engine the pipeline used. 409 when the source
      already aged out.
    """
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[req.job_id]

    # Serialize rerenders per job: concurrent saves would otherwise race on the shared metadata.json
    # read-modify-write below, with the last writer silently reverting the
    # other clip's recipe/video_url.
    lock = _rerender_locks.setdefault(req.job_id, asyncio.Lock())
    async with lock:
        return await _rerender_locked(req, request, job)


async def _rerender_locked(req: RerenderRequest, request: Request, job):
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
    with open(json_files[0], 'r') as f:
        data = json.load(f)

    clips = data.get('shorts', [])
    if req.clip_index < 0 or req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
    clip = clips[req.clip_index]
    transcript = data.get('transcript') or {}

    base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
    clean_name = f"{base_name}_clip_{req.clip_index + 1}.mp4"
    canonical_path = os.path.join(output_dir, clean_name)

    _, canonical_range = _clip_recipe_parts(clip)
    source_path = _locate_source(req.job_id)
    source_duration = _source_duration_seconds(source_path) if source_path else None
    framing = req.framing or (clip.get('recipe') or {}).get('framing') or 'auto'
    if framing not in _FRAMING_STRATEGIES:
        raise HTTPException(status_code=400,
                            detail="framing must be one of: auto, full, track")
    force_strategy = _FRAMING_STRATEGIES[framing]

    try:
        segments = recut.normalize_segments(
            [{"start": s.start, "end": s.end} for s in req.segments],
            source_duration)
        if req.snap_to_words:
            snap_bound = source_duration or max(s['end'] for s in segments)
            segments = recut.snap_segments(segments, transcript, snap_bound)
            if not source_path:
                # Snapping trails into silence and may nudge a boundary past
                # the canonical range; without a source the fast path is the
                # only path, so clamp back instead of failing with a 409.
                segments = [
                    {"start": round(max(s['start'], canonical_range['start']), 3),
                     "end": round(min(s['end'], canonical_range['end']), 3)}
                    for s in segments]
            # Re-validate after snapping/clamping: a segment fully outside the
            # canonical range clamps to an inverted (end < start) window, and
            # snapping can stretch the total past the cap. Without this it
            # reaches ffmpeg and dies as a 500 instead of a clean 400.
            segments = recut.normalize_segments(segments, source_duration)
    except recut.RecutError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # A framing override always re-reframes from the source: the canonical file
    # has the old layout baked into its pixels.
    fast = (force_strategy is None
            and os.path.exists(canonical_path)
            and recut.within_range(segments, canonical_range['start'],
                                   canonical_range['end']))
    if not fast and not source_path:
        raise HTTPException(
            status_code=409,
            detail=("The source video is no longer on the server; changing the "
                    "framing needs it." if force_strategy is not None else
                    "The source video is no longer on the server, so segments "
                    "must stay within the original clip range."))

    total = recut.total_duration(segments)

    v_transcript = (recut.virtual_transcript(transcript, segments)
                    if req.reapply_captions else None)

    def run_recut():
        if fast:
            return recut.perform_recut(
                input_path=canonical_path,
                segments=recut.rebase_segments(
                    segments, canonical_range['start'], canonical_range['end']),
                output_dir=output_dir, clean_name=clean_name,
                reframe=False, captions_transcript=v_transcript,
                captioner=_recut_captioner(clip))
        return recut.perform_recut(
            input_path=source_path, segments=segments,
            output_dir=output_dir, clean_name=clean_name,
            reframe=True, output_format=data.get('output_format', 'auto'),
            force_strategy=force_strategy,
            captions_transcript=v_transcript,
            captioner=_recut_captioner(clip),
            # Trim the source's own subtitles only when ours go back on.
            subtitle_band=(data.get('source_subtitle_band')
                           if v_transcript else None))

    try:
        loop = asyncio.get_event_loop()
        served_name, _clean_recut_name = await loop.run_in_executor(None, run_recut)
        served_name = served_name

        new_video_url = f"/videos/{req.job_id}/{served_name}"
        new_recipe = {"v": 1, "segments": segments,
                      "canonical_range": canonical_range}
        if framing != 'auto':
            new_recipe["framing"] = framing
        # Covering range, deliberately not segments[0]/segments[-1]: segments
        # may legally be out of source order, and downstream consumers only
        # need a sane positive window (the recipe is the real timeline).
        new_start = min(s['start'] for s in segments)
        new_end = max(s['end'] for s in segments)

        updates = {'video_url': new_video_url, 'start': new_start,
                   'end': new_end, 'recipe': new_recipe,
                   # The stacked stretches of THIS render (empty on the fast
                   # path, which never reframes): captions follow them.
                   'layout_ranges': layout_ranges.read(
                       os.path.join(output_dir, _clean_recut_name))}
        # Per-scene manual framing is keyed by scene indices of a specific cut;
        # this render neither applied it nor can it survive a changed cut, so
        # clear it rather than let /scenes serve stale overrides against the
        # wrong shots (re-frame after trimming to re-apply by hand).
        if clip.get('crop_overrides'):
            updates['crop_overrides'] = None
        # Edited caption words are timed against the previous cut; the new one
        # captions from the transcript (in the recorded style, if any).
        if clip.get('caption_words'):
            updates['caption_words'] = None
        updates.update(_leaves_pro_mode(clip))
        clip.update(updates)
        data['shorts'] = clips
        write_json_atomic(json_files[0], data, indent=2)
        mem_clips = (job.get('result') or {}).get('clips') or []
        if req.clip_index < len(mem_clips):
            mem_clips[req.clip_index].update(updates)

        return {
            "success": True,
            "new_video_url": new_video_url,
            "recipe": new_recipe,
            "framing": framing,
            "start": new_start,
            "end": new_end,
            "duration": total,
            "render_path": "fast" if fast else "source",
            "mode": "normal",
        }
    except Exception as e:
        print(f"❌ Rerender Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- Pro mode -----------------------------------------------------------------
#
# pro_mode.py re-edits a talking-head clip (dead air cut, a tighter frame that
# changes scale on every cut, graded picture with the background dimmed). The
# job pipeline flags the clips it fits (``pro_suggested``); this is the switch
# the dashboard puts on every clip, both ways. The pro file is a recut of the
# clip's current clean file, so the layers above it (hook, captions, the free
# plan's mark) are put back exactly as the other derivations put them back.

class ClipModeRequest(BaseModel):
    job_id: str
    clip_index: int
    mode: str  # "pro" | "normal"
    # The file the dashboard is showing for this clip (same field as
    # /api/hook and /api/subtitle).
    input_filename: Optional[str] = None


def _served_clip_file(job, output_dir, base_name, clip, index, asked=None):
    """The file a clip is served from right now. The metadata only learns it
    at the first edit (the job writes no ``video_url``), so ask in order: the
    caller, the job's in-memory result, the metadata, the newest derived file
    on disk. Each has to exist; the clean reframe is the last resort."""
    clean = f"{base_name}_clip_{index + 1}.mp4"
    mem_clips = (job.get('result') or {}).get('clips') or []
    mem = mem_clips[index] if index < len(mem_clips) else {}
    # What the caller names has to be a file of THIS clip: every derivation
    # keeps the clean name as its tail.
    asked = os.path.basename(str(asked or ''))
    candidates = [asked if asked.endswith(clean) else None,
                  mem.get('video_url'), clip.get('video_url'),
                  _canonical_clip_file(output_dir, base_name, index)]
    for candidate in candidates:
        name = os.path.basename(str(candidate or '').split('?')[0])
        if name and os.path.isfile(os.path.join(output_dir, name)):
            return name
    return clean


# How long a switch to pro waits for a render slot of this host (pro_mode.slot)
# before answering "busy": a person is looking at a spinner.
PRO_WAIT_SECONDS = float(os.environ.get("PRO_WAIT_SECONDS", "20"))


def _leaves_pro_mode(clip):
    """Metadata updates for a clip rebuilt from the canonical file or the
    source (rerender, manual reframe): that render is the normal look."""
    if clip.get('mode') != 'pro':
        return {}
    return {'mode': 'normal', 'pro_before': None}


def _clip_layers(clip, had_hook, had_captions):
    """What sits on top of a clip's clean file, as one comparable value: a
    served file can be reused only for the layers it was burned with."""
    return json.dumps([had_hook and clip.get('auto_hook'), had_captions,
                       had_captions and clip.get('caption_style'),
                       had_captions and clip.get('caption_words')],
                      sort_keys=True, default=str)


@app.post("/api/clip/mode")
async def set_clip_mode(req: ClipModeRequest, request: Request):
    """Switch a clip between its normal render and pro mode."""
    if req.mode not in ("pro", "normal"):
        raise HTTPException(status_code=400, detail="mode must be 'pro' or 'normal'")
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[req.job_id]
    if job.get('status') != 'completed':
        # The job rewrites the metadata when it ends: a switch made while it
        # runs would be undone under the user's eyes.
        raise HTTPException(
            status_code=409,
            detail="This video is still processing. Pro mode is available "
                   "once all its clips are ready.")

    # Same lock as the rerenders: they all read-modify-write the metadata and
    # replace the clip's files. It also makes a repeated click wait for the
    # first one and get its result.
    lock = _rerender_locks.setdefault(req.job_id, asyncio.Lock())
    async with lock:
        return await _set_clip_mode_locked(req, request, job)


async def _set_clip_mode_locked(req: ClipModeRequest, request: Request, job):
    import pro_mode
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
    with open(json_files[0], 'r') as f:
        data = json.load(f)
    clips = data.get('shorts', [])
    if req.clip_index < 0 or req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
    clip = clips[req.clip_index]

    base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
    clean_name = f"{base_name}_clip_{req.clip_index + 1}.mp4"
    served = _served_clip_file(job, output_dir, base_name, clip, req.clip_index,
                               req.input_filename)

    def answer(**extra):
        return {"success": True, "mode": clip.get('mode') or 'normal',
                "new_video_url": clip.get('video_url') or f"/videos/{req.job_id}/{served}",
                "start": clip.get('start'), "end": clip.get('end'), **extra}

    if (clip.get('mode') or 'normal') == req.mode:
        return answer(unchanged=True)
    if not os.path.exists(os.path.join(output_dir, served)):
        raise HTTPException(status_code=404, detail="Video file not found")
    # Walk the layers back to the clean file, noting which ones to put back.
    uncaptioned = await asyncio.to_thread(
        _strip_burned_captions, output_dir, served, False)
    had_captions = uncaptioned != _strip_watermark_copy(output_dir, served)
    base = _strip_burned_hook(output_dir, uncaptioned)
    had_hook = base != uncaptioned and bool((clip.get('auto_hook') or {}).get('text'))
    loop = asyncio.get_event_loop()

    reuse = None
    if req.mode == "pro":
        base_path = os.path.join(output_dir, base)
        # A restored project may only have the served file: with captions or
        # a hook still burned in, the reframing would crop and scale them.
        if (not os.path.exists(base_path) or watermarked.is_marked(base)
                or re.match(r'^(?:subtitled_\d+_|hooked_\d+_|hook_)', base)):
            raise HTTPException(
                status_code=409,
                detail="The clean render of this clip is no longer on the server, "
                       "so it cannot be converted.")
        layers = _clip_layers(clip, had_hook, had_captions)
        hook_seconds = 0.0
        if had_hook:
            try:
                hook_seconds = float((clip.get('auto_hook') or {}).get('duration_seconds') or 5.0)
            except (TypeError, ValueError):
                hook_seconds = 5.0
        try:
            new_base, updates = await loop.run_in_executor(
                None, lambda: pro_mode.convert(
                    base_path, clean_name, clip, data.get('transcript') or {},
                    wait=PRO_WAIT_SECONDS, hook_seconds=hook_seconds,
                    caption_style=CAPTION_PRESETS["cine"] if had_captions else None))
        except Exception as e:
            if isinstance(e, pro_mode.Busy):
                raise HTTPException(
                    status_code=503, headers={"Retry-After": "60"},
                    detail="Pro mode is busy right now. Try again in a minute.")
            if isinstance(e, pro_mode.NotApplicable):
                raise HTTPException(status_code=400,
                                    detail="Pro mode needs a vertical clip.")
            print(f"❌ Pro mode error: {e}")
            raise HTTPException(status_code=500, detail="Pro mode could not render this clip.")
        # Going back reuses the file served today when nothing on it changed.
        updates['pro_before'].update(served=_strip_watermark_copy(output_dir, served),
                                     layers=layers)
    else:
        before = clip.get('pro_before') or {}
        new_base = before.get('base') or clean_name
        if not os.path.exists(os.path.join(output_dir, new_base)):
            new_base = clean_name
        if not os.path.exists(os.path.join(output_dir, new_base)):
            raise HTTPException(status_code=409,
                                detail="The normal render of this clip is no longer on the server.")
        updates = pro_mode.restore(clip)
        kept = before.get('served')
        if (kept and os.path.exists(os.path.join(output_dir, kept))
                and before.get('layers') == _clip_layers(
                    {**clip, **updates}, had_hook, had_captions)):
            reuse = kept

    try:
        # A valid state first (the clean file of the new mode, no layers), so
        # nothing below can leave the clip pointing at the other mode's file.
        clip.update(updates)
        clip['video_url'] = f"/videos/{req.job_id}/{new_base}"
        data['shorts'] = clips
        write_json_atomic(json_files[0], data, indent=2)

        def layer():
            name = new_base
            if had_hook:
                hook = clip['auto_hook']
                hooked = f"hooked_{int(time.time())}_{new_base}"
                try:
                    add_hook_to_video(
                        os.path.join(output_dir, name), hook['text'],
                        os.path.join(output_dir, hooked),
                        position=hook.get('position') or "auto",
                        duration=hook.get('duration_seconds'),
                        style=hook.get('style') or "pill", font=hook.get('font'),
                        layout_ranges=clip.get('layout_ranges'),
                        has_captions=had_captions)
                    name = hooked
                except Exception as e:
                    # The clip without its hook beats no clip.
                    print(f"⚠️ Could not put the hook back on {new_base}: {e}")
            if had_captions:
                recap = _reapply_captions(req.job_id, req.clip_index,
                                          os.path.join(output_dir, name))
                if recap:
                    name = os.path.basename(recap)
            return name

        final = reuse or await loop.run_in_executor(None, layer)
        final = final
        clip['video_url'] = f"/videos/{req.job_id}/{final}"
        write_json_atomic(json_files[0], data, indent=2)
        mem_clips = (job.get('result') or {}).get('clips') or []
        if req.clip_index < len(mem_clips):
            mem_clips[req.clip_index].update(updates)
            mem_clips[req.clip_index]['video_url'] = clip['video_url']
        return answer(recipe=clip.get('recipe'))
    except Exception as e:
        print(f"❌ Clip mode error: {e}")
        raise HTTPException(status_code=500, detail="Could not switch the clip's mode.")


# --- Manual framing -----------------------------------------------------------
#
# The reframe engine picks the crop automatically, and on a podcast it is right
# most of the time and grossly wrong occasionally: a wide shot of the whole
# table averages over one face, so the scene falls to GENERAL (letterboxed) or
# tracks the wrong person. There was no way to say "no, frame it here".
#
# The unit is the SCENE, not the clip, because a podcast cuts between a fixed
# close camera and a fixed wide one, and the right crop differs per camera.
# Scene boundaries already are the camera changes: PySceneDetect finds them.
#
# Scenes the user never touches keep the automatic camera, so correcting one
# bad shot cannot spoil the ones the tracker got right.

class ReframeRequest(BaseModel):
    job_id: str
    clip_index: int
    # scene index (string key, JSON-style) -> either a crop centre as a
    # fraction of the source width, or {"top": f, "bottom": f} to stack two
    # regions. Fractions travel instead of pixels so the editor never needs to
    # know the source dimensions.
    crop_overrides: Dict[str, Any]
    reapply_captions: bool = True


def _clip_scene_workfile(source_path, segments, output_dir, token):
    """Cut the clip out of the source so scenes can be detected on it.

    The name is unique per call, unlike the thumbnails and the preview: two
    editor opens on the same clip would otherwise write the same temp file at
    once, and the first to finish deletes it out from under the second.
    """
    work_path = os.path.join(output_dir, f"scenes_{token}.mp4")
    recut.run_cut_concat(source_path, segments, work_path, output_dir)
    return work_path


@app.get("/api/clip/{job_id}/{clip_index}/scenes")
async def get_clip_scenes(job_id: str, clip_index: int, request: Request):
    """Scenes of a clip, each with a SOURCE frame to frame it against.

    The frames come from the uncropped cut, not the delivered clip: the point
    is to show what the automatic crop threw away, which the 9:16 file no
    longer contains.
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    output_dir = os.path.join(OUTPUT_DIR, job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
    with open(json_files[0], 'r') as f:
        data = json.load(f)

    clips = data.get('shorts', [])
    if clip_index < 0 or clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")

    if data.get('output_format') == 'horizontal':
        raise HTTPException(
            status_code=400,
            detail="Horizontal clips keep the full frame; there is no crop to reframe.")

    clip = clips[clip_index]
    segments, _canonical_range = _clip_recipe_parts(clip)
    # What was applied last time. Without this the editor reopens blank, and
    # since a re-render rebuilds from source using ONLY what it is sent, the
    # next save would silently drop every earlier adjustment.
    saved_overrides = clip.get('crop_overrides') or {}
    source_path = _locate_source(job_id)
    if not source_path:
        raise HTTPException(
            status_code=409,
            detail="The source video is no longer on the server, so the "
                   "framing of this clip can no longer be changed.")

    def build():
        import cv2
        import main as m

        # Stable for the files the browser fetches (no accumulation), unique
        # for the temp cut (no collision between overlapping requests).
        # temp_ prefix keeps these editor-only artifacts out of the self-host
        # S3 backup (it skips temp_*); stable names still avoid accumulation.
        token = str(clip_index)
        work_token = f"{clip_index}_{uuid.uuid4().hex[:8]}"
        preview_name = f"temp_preview_{clip_index}.mp4"
        preview_path = os.path.join(output_dir, preview_name)
        work_path = _clip_scene_workfile(source_path, segments, output_dir, work_token)
        try:
            scenes, fps = m.detect_scenes(work_path)
            fps = float(fps) or 30.0
            orig_w, orig_h = m.get_video_resolution(work_path)

            cap = cv2.VideoCapture(work_path)
            if not scenes:
                total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
                bounds = [(0, total)]
            else:
                bounds = [(s.get_frames(), e.get_frames()) for s, e in scenes]

            out = []
            for idx, (start_f, end_f) in enumerate(bounds):
                mid = (start_f + end_f) // 2
                cap.set(cv2.CAP_PROP_POS_FRAMES, mid)
                ok, frame = cap.read()
                thumb_name = f"temp_scene_{token}_{idx:03d}.jpg"
                suggested = 0.5
                suggested_y = 0.5
                if ok:
                    cv2.imwrite(os.path.join(output_dir, thumb_name), frame,
                                [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    # Start the rectangle on the biggest face in the shot, so
                    # the common case is a nudge rather than a hunt.
                    try:
                        faces = m.detect_face_candidates(frame)
                        if faces:
                            box = max(faces,
                                      key=lambda f: f['box'][2] * f['box'][3])['box']
                            suggested = min(1.0, max(0.0,
                                                     (box[0] + box[2] / 2) / orig_w))
                            # SPLIT halves crop vertically too, so the face's
                            # height matters there (TRACK ignores it).
                            suggested_y = min(1.0, max(0.0,
                                                       (box[1] + box[3] / 2) / orig_h))
                    except Exception:
                        pass
                else:
                    thumb_name = None

                out.append({
                    "index": idx,
                    "start": round(start_f / fps, 3),
                    "end": round(end_f / fps, 3),
                    "thumbnail_url": (f"/videos/{job_id}/{thumb_name}"
                                      if thumb_name else None),
                    "suggested_center": round(suggested, 4),
                    "suggested_center_y": round(suggested_y, 4),
                })
            cap.release()
            return orig_w, orig_h, out, preview_name
        finally:
            # The uncropped cut becomes a light preview instead of being
            # discarded: judging the framing means knowing who is talking, and
            # the delivered 9:16 file no longer shows the rest of the room.
            try:
                if os.path.exists(work_path):
                    subprocess.run(
                        ["ffmpeg", "-y", "-loglevel", "error", "-i", work_path,
                         "-vf", "scale=640:-2", "-c:v", "libx264", "-preset",
                         "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "96k",
                         # faststart: the editor's <video> streams the preview;
                         # a tail moov would stall it until fully downloaded.
                         "-movflags", "+faststart",
                         preview_path], check=True, timeout=600)
            except Exception as exc:
                print(f"Scene preview failed: {exc}")
            finally:
                if os.path.exists(work_path):
                    os.remove(work_path)

    # Serialized per job: two overlapping opens would run two ffmpeg writers
    # on the same stable preview/thumbnail names and serve a torn file.
    lock = _scenes_locks.setdefault(job_id, asyncio.Lock())
    async with lock:
        try:
            loop = asyncio.get_event_loop()
            orig_w, orig_h, scenes_out, preview_name = await loop.run_in_executor(None, build)
        except Exception as e:
            print(f"Scene listing error: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # Width of the crop window as a fraction of the source width: the editor
    # draws the rectangle with it and needs no pixel arithmetic of its own.
    # The aspect follows the job's output format (9:16, or 1:1 for square).
    aspect = 1.0 if data.get('output_format') == 'square' else 9.0 / 16.0
    crop_w = min(orig_w, orig_h * aspect)
    return {
        "job_id": job_id,
        "clip_index": clip_index,
        "source_width": orig_w,
        "source_height": orig_h,
        "crop_width_fraction": round(crop_w / orig_w, 4),
        "preview_url": f"/videos/{job_id}/{preview_name}",
        "saved_overrides": saved_overrides,
        "scenes": scenes_out,
    }


@app.post("/api/clip/reframe")
async def reframe_clip(req: ReframeRequest, request: Request):
    """Re-render a clip with hand-framed scenes, leaving its cut untouched.

    Deliberately separate from /api/clip/rerender: the overrides are keyed by
    scene index, and a scene index only means anything against a given cut. If
    framing rode along with a trim save, changing the trim would silently move
    every override onto the wrong shot.
    """
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[req.job_id]

    def _fraction(v):
        return min(1.0, max(0.0, float(v)))

    overrides = {}
    for key, value in (req.crop_overrides or {}).items():
        try:
            idx = int(key)
            if isinstance(value, dict):
                def _half(h):
                    if isinstance(h, dict):
                        return {"x": _fraction(h["x"]), "y": _fraction(h.get("y", 0.5))}
                    return {"x": _fraction(h), "y": 0.5}
                overrides[idx] = {"top": _half(value["top"]),
                                  "bottom": _half(value["bottom"])}
            else:
                overrides[idx] = _fraction(value)
        except (KeyError, TypeError, ValueError):
            continue
    if not overrides:
        raise HTTPException(status_code=400,
                            detail="No scene framing was provided.")

    # Same lock as /rerender: both read-modify-write the job's metadata.json,
    # and the metadata must be read INSIDE the lock or a rerender committing
    # in between gets clobbered by a write of stale data.
    lock = _rerender_locks.setdefault(req.job_id, asyncio.Lock())
    async with lock:
        return await _reframe_locked(req, request, job, overrides)


async def _reframe_locked(req: ReframeRequest, request: Request, job, overrides):
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
    with open(json_files[0], 'r') as f:
        data = json.load(f)

    clips = data.get('shorts', [])
    if req.clip_index < 0 or req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
    clip = clips[req.clip_index]

    if data.get('output_format') == 'horizontal':
        raise HTTPException(
            status_code=400,
            detail="Horizontal clips keep the full frame; there is no crop to reframe.")

    segments, canonical_range = _clip_recipe_parts(clip)
    source_path = _locate_source(req.job_id)
    if not source_path:
        raise HTTPException(
            status_code=409,
            detail="The source video is no longer on the server, so the "
                   "framing of this clip can no longer be changed.")

    # Whole-clip framing (recipe.framing, the clip editor's selector) still
    # applies to the scenes the user did NOT hand-position: apply_crop_overrides
    # runs after force_strategy, so a per-scene choice beats the whole-clip one.
    framing = (clip.get('recipe') or {}).get('framing') or 'auto'
    force_strategy = _FRAMING_STRATEGIES.get(framing)

    # Every default clip ships with burned captions; re-rendering without them
    # would silently hand back a caption-less file.
    v_transcript = (recut.virtual_transcript(data.get('transcript') or {}, segments)
                    if req.reapply_captions else None)

    base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
    # The CLEAN base name, exactly as /api/clip/rerender does — never the
    # current derived file. perform_recut prefixes whatever it is given, so
    # feeding it the newest derivative made every re-render add another
    # recut_<ts>_<hex>_ in front of the previous one. A few rounds of framing
    # and captioning and the path blew past the filesystem limit:
    # "Error opening output ...: File name too long".
    clean_name = f"{base_name}_clip_{req.clip_index + 1}.mp4"

    def run():
        return recut.perform_recut(
            input_path=source_path, segments=segments,
            output_dir=output_dir, clean_name=clean_name,
            reframe=True, output_format=data.get('output_format', 'auto'),
            force_strategy=force_strategy,
            crop_overrides=overrides,
            captions_transcript=v_transcript,
            captioner=_recut_captioner(clip),
            # Trim the source's own subtitles only when ours go back on.
            subtitle_band=(data.get('source_subtitle_band')
                           if v_transcript else None))

    try:
        loop = asyncio.get_event_loop()
        served_name, _clean = await loop.run_in_executor(None, run)
        served_name = served_name

        new_video_url = f"/videos/{req.job_id}/{served_name}"
        new_recipe = {"v": 1, "segments": segments,
                      "canonical_range": canonical_range}
        if framing != 'auto':
            new_recipe["framing"] = framing
        updates = {
            'video_url': new_video_url,
            'recipe': new_recipe,
            'crop_overrides': {str(k): v for k, v in overrides.items()},
            'layout_ranges': layout_ranges.read(os.path.join(output_dir, _clean)),
        }
        updates.update(_leaves_pro_mode(clip))
        clip.update(updates)
        data['shorts'] = clips
        write_json_atomic(json_files[0], data, indent=2)
        mem_clips = (job.get('result') or {}).get('clips') or []
        if req.clip_index < len(mem_clips):
            mem_clips[req.clip_index].update(updates)

        # The cut is untouched, but the response mirrors /rerender's shape
        # so the dashboard can reuse one handler without clearing the
        # clip's timing fields.
        return {
            "success": True,
            "new_video_url": new_video_url,
            "recipe": new_recipe,
            "start": min(s['start'] for s in segments),
            "end": max(s['end'] for s in segments),
            "framed_scenes": sorted(overrides),
            "mode": "normal",
        }
    except Exception as e:
        print(f"Reframe Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class EffectsGenerateRequest(BaseModel):
    job_id: str
    clip_index: int
    input_filename: Optional[str] = None

@app.post("/api/effects/generate")
async def generate_effects_config(
    req: EffectsGenerateRequest,
    request: Request,
):
    """Generate structured EffectsConfig JSON for Remotion rendering via Gemini AI."""
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[req.job_id]
    if 'result' not in job or 'clips' not in job['result']:
        raise HTTPException(status_code=400, detail="Job result not available")

    try:
        # Resolve input path
        if req.input_filename:
            safe_name = os.path.basename(req.input_filename)
            input_path = os.path.join(OUTPUT_DIR, req.job_id, safe_name)
        else:
            clip = job['result']['clips'][req.clip_index]
            filename = clip['video_url'].split('/')[-1]
            input_path = os.path.join(OUTPUT_DIR, req.job_id, filename)

        if not os.path.exists(input_path):
            raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")

        def run_effects_generation():
            editor = VideoEditor()

            # Create safe ASCII filename to avoid encoding issues
            safe_filename = f"temp_effects_{req.job_id}.mp4"
            safe_input_path = os.path.join(OUTPUT_DIR, req.job_id, safe_filename)
            shutil.copy(input_path, safe_input_path)

            try:
                # Upload video to Gemini
                vid_file = editor.sample_frames(safe_input_path)

                # Get video metadata via ffprobe
                probe_cmd = [
                    'ffprobe', '-v', 'error',
                    '-select_streams', 'v:0',
                    '-show_entries', 'stream=width,height,r_frame_rate,duration',
                    '-show_entries', 'format=duration',
                    '-of', 'json',
                    safe_input_path
                ]
                probe_result = subprocess.check_output(probe_cmd).decode().strip()
                probe_data = json.loads(probe_result)

                stream = probe_data.get('streams', [{}])[0]
                width = int(stream.get('width', 1080))
                height = int(stream.get('height', 1920))

                # Parse fps from r_frame_rate (e.g. "30/1")
                r_frame_rate = stream.get('r_frame_rate', '30/1')
                num, den = r_frame_rate.split('/')
                fps = round(int(num) / int(den), 2)

                # Get duration from stream or format
                duration = float(stream.get('duration', 0))
                if duration == 0:
                    duration = float(probe_data.get('format', {}).get('duration', 0))

                # Load transcript from metadata
                transcript = None
                try:
                    meta_files = glob.glob(os.path.join(OUTPUT_DIR, req.job_id, "*_metadata.json"))
                    if meta_files:
                        with open(meta_files[0], 'r') as f:
                            data = json.load(f)
                            transcript = data.get('transcript')
                except Exception as e:
                    print(f"⚠️ Could not load transcript for effects config: {e}")

                # Generate effects config
                effects_config = editor.get_effects_config(
                    vid_file, duration, fps=fps, width=width, height=height, transcript=transcript
                )

                return effects_config
            finally:
                if os.path.exists(safe_input_path):
                    os.remove(safe_input_path)

        loop = asyncio.get_event_loop()
        effects_config = await loop.run_in_executor(None, run_effects_generation)

        if effects_config is None:
            raise HTTPException(status_code=500, detail="Failed to generate effects config from Gemini")

        return {"effects": effects_config}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Effects Generation Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/subtitle")
async def add_subtitles(req: SubtitleRequest, request: Request):
    if req.preset:
        preset = CAPTION_PRESETS.get(req.preset.strip().lower())
        if preset is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown preset. Use one of: {', '.join(CAPTION_PRESETS)}.")
        req = req.model_copy(update={k: v for k, v in preset.items()
                                     if k not in req.model_fields_set})
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Reload job data from disk just in case metadata was updated
    job = jobs[req.job_id]

    # We need to access metadata.json to get the transcript
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
        
    with open(json_files[0], 'r') as f:
        data = json.load(f)
        
    transcript = data.get('transcript')
    if not transcript:
        raise HTTPException(status_code=400, detail="Transcript not found in metadata. Please process a new video.")
        
    clips = data.get('shorts', [])
    if req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
        
    clip_data = clips[req.clip_index]

    # Recut clips concatenate several source segments, so their caption window
    # is not the flat start..end range — restyle against the clip-relative
    # remapped transcript instead.
    recipe_segments = (clip_data.get('recipe') or {}).get('segments')
    if recipe_segments:
        sub_transcript = recut.virtual_transcript(transcript, recipe_segments)
        sub_start, sub_end = 0.0, recut.total_duration(recipe_segments)
    else:
        sub_transcript = transcript
        sub_start = clip_data.get('start', 0)
        sub_end = clip_data.get('end', 0)

    # User-edited captions win over both: build a synthetic clip-relative
    # transcript from them so the SRT/ASS generators burn the edited words
    # verbatim (issue #69 — edits used to be dropped on this path).
    caption_words = None
    if req.words:
        if len(req.words) > 2000:
            raise HTTPException(status_code=400, detail="Too many caption words (max 2000).")
        # Leading space = Whisper's word-boundary convention; without it the
        # block collector treats each word as a continuation fragment and
        # glues the whole line together.
        edited = [
            {"word": " " + w.text.strip(), "start": max(0.0, w.startMs / 1000.0),
             "end": max(0.0, w.endMs / 1000.0)}
            for w in req.words if w.text.strip() and w.endMs > w.startMs >= 0
        ]
        if edited:
            sub_transcript = _words_transcript(
                edited, (transcript or {}).get("language", "en"))
            sub_start, sub_end = 0.0, sub_transcript["segments"][0]["end"]
            caption_words = sub_transcript["segments"][0]["words"]

    # Video Path
    if req.input_filename:
        # Use chained file
        filename = os.path.basename(req.input_filename)
    else:
        # Fallback to standard naming
        filename = clip_data.get('video_url', '').split('/')[-1]
        if not filename:
             base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
             filename = f"{base_name}_clip_{req.clip_index+1}.mp4"

    # Re-subtitling must replace previous subtitles instead of burning over them.
    filename = await asyncio.to_thread(_strip_burned_captions, output_dir, filename)

    input_path = os.path.join(output_dir, filename)
    if not os.path.exists(input_path):
        # Try looking for edited version if url implied it?
        # Just fail if not found.
        raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")

    # Define outputs
    generation_id = int(time.time())
    is_karaoke = req.style == "karaoke"
    srt_filename = f"subs_{req.clip_index}_{generation_id}.{'ass' if is_karaoke else 'srt'}"
    srt_path = os.path.join(output_dir, srt_filename)

    # Style options shared by the karaoke ASS generator paths.
    # Stacked (SPLIT) stretches put their captions on the seam. The metadata
    # copy is authoritative; the sidecar covers clips rendered before it was
    # recorded there.
    seam_ranges = layout_ranges.split_ranges(
        clip_data.get('layout_ranges') or layout_ranges.read(input_path))
    caption_style = {k: getattr(req, k) for k in CAPTION_STYLE_FIELDS}
    karaoke_opts = dict(
        split_ranges=seam_ranges,
        alignment=req.position, fontsize=req.font_size, font_name=req.font_name,
        font_color=req.font_color, border_color=req.border_color,
        border_width=req.border_width, highlight_color=req.highlight_color,
        bg_color=req.bg_color, bg_opacity=req.bg_opacity,
        effect=req.effect, base_opacity=req.base_opacity, uppercase=req.uppercase,
        reveal=req.reveal, shadow=req.shadow,
    )
    if req.max_chars is not None:
        karaoke_opts["max_chars"] = max(1, min(40, int(req.max_chars)))
    if req.max_duration is not None:
        karaoke_opts["max_duration"] = max(0.5, min(5.0, float(req.max_duration)))

    # Output video
    # We create a new file "subtitled_..."
    output_filename = f"subtitled_{generation_id}_{filename}"
    output_path = os.path.join(output_dir, output_filename)

    # A dubbed clip gets a fresh transcription of its translated audio.
    is_dubbed = filename.startswith("translated_")

    try:
        # 1. Generate SRT — from the existing transcript, or a fresh
        # transcription when the audio was dubbed.
        if is_dubbed:
            print(f"🎙️ Dubbed video detected, transcribing audio for subtitles...")
            def run_transcribe_srt():
                if is_karaoke:
                    return generate_srt_from_video(input_path, srt_path, style="karaoke", **karaoke_opts)
                return generate_srt_from_video(input_path, srt_path)

            loop = asyncio.get_event_loop()
            try:
                success = await loop.run_in_executor(None, run_transcribe_srt)
            finally:
                # The ASR models must not stay resident in the API process:
                # they held 7.7 GB of VRAM idle (transcribe_backends.release_models).
                import transcribe_backends
                await loop.run_in_executor(None, transcribe_backends.release_models)
            if not success:
                raise HTTPException(status_code=400, detail="No words found for this clip range.")

            # 2. Burn Subtitles
            def run_burn():
                burn_subtitles(input_path, srt_path, output_path,
                               alignment=req.position, fontsize=req.font_size,
                               font_name=req.font_name, font_color=req.font_color,
                               border_color=req.border_color, border_width=req.border_width,
                               bg_color=req.bg_color, bg_opacity=req.bg_opacity)
            await loop.run_in_executor(None, run_burn)
        else:
            # Same code path the hook / edit / recut re-burn uses, so the look
            # recorded below is the look that was burned here.
            success = await asyncio.get_event_loop().run_in_executor(
                None, _burn_caption_style, input_path, output_path, srt_path,
                sub_transcript, sub_start, sub_end, caption_style, seam_ranges)
            if not success:
                raise HTTPException(status_code=400, detail="No words found for this clip range.")
        
    except Exception as e:
        print(f"❌ Subtitle Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


    output_filename = output_filename

    # 3. Update Result and Metadata
    # Update InMemory Jobs
    if req.clip_index < len(job['result']['clips']):
         job['result']['clips'][req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"
    
    # Update Metadata on Disk (Persistence)
    try:
        if req.clip_index < len(clips):
            clips[req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"
            # The look (and edited words) the hook / edit / recut paths burn
            # again when they put captions back (_reapply_captions, #82).
            clips[req.clip_index]['caption_style'] = caption_style
            if caption_words:
                clips[req.clip_index]['caption_words'] = caption_words
            else:
                clips[req.clip_index].pop('caption_words', None)
            # Update the main data structure
            data['shorts'] = clips
            
            # Write back
            write_json_atomic(json_files[0], data, indent=4)
            print(f"✅ Metadata updated with subtitled video for clip {req.clip_index}")
    except Exception as e:
        print(f"⚠️ Failed to update metadata.json: {e}")
        # Non-critical, but good for persistence


    return {
        "success": True,
        "new_video_url": f"/videos/{req.job_id}/{output_filename}"
    }

class RemoveSubtitlesRequest(BaseModel):
    job_id: str
    clip_index: int
    input_filename: Optional[str] = None


@app.post("/api/subtitle/remove")
async def remove_subtitles(req: RemoveSubtitlesRequest, request: Request):
    """Point a clip back at its un-captioned original.

    Clips ship captioned by default now, so there has to be a way out — without
    this, a user who doesn't want captions is stuck with them. No re-encode and
    no quota: the pipeline always keeps the clean file next to the derived
    ``subtitled_<ts>_`` one, so removing is just choosing the other file.
    """
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    job = jobs[req.job_id]

    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
    with open(json_files[0], 'r') as f:
        data = json.load(f)
    clips = data.get('shorts', [])
    if req.clip_index < 0 or req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")

    filename = os.path.basename(
        req.input_filename
        or (clips[req.clip_index].get('video_url') or '').split('/')[-1]
        or f"{os.path.basename(json_files[0]).replace('_metadata.json', '')}"
           f"_clip_{req.clip_index + 1}.mp4")

    # Same walk-back the burn path uses, so this undoes any number of restyles.
    filename = await asyncio.to_thread(_strip_burned_captions, output_dir, filename)

    if not os.path.exists(os.path.join(output_dir, filename)):
        raise HTTPException(status_code=404,
                            detail="The original clip is no longer available.")

    # Free plan: the un-captioned file is served through its wm_ copy, which
    # is reused when the clip shipped that way (no encode).
    filename = filename
    new_url = f"/videos/{req.job_id}/{filename}"
    if req.clip_index < len(job.get('result', {}).get('clips', [])):
        job['result']['clips'][req.clip_index]['video_url'] = new_url
    try:
        clips[req.clip_index]['video_url'] = new_url
        data['shorts'] = clips
        write_json_atomic(json_files[0], data, indent=4)
    except Exception as e:
        print(f"⚠️ Failed to update metadata.json: {e}")

    return {"success": True, "new_video_url": new_url}


class HookRequest(BaseModel):
    job_id: str
    clip_index: int
    text: Optional[str] = ""
    input_filename: Optional[str] = None
    # auto (off the faces and the captions, hooks/hook_placement), or an
    # explicit top / center / bottom, which is drawn exactly there.
    position: Optional[str] = "auto"
    size: Optional[str] = "M" # S, M, L
    duration_seconds: Optional[float] = None  # None = hook visible for the whole clip
    style: Optional[str] = "pill"  # pill/classic/dark/yellow/red/outline/outline_yellow
    font: Optional[str] = None  # montserrat/anton/serif (hooks.HOOK_FONTS); None = the style's own
    remove: Optional[bool] = False  # strip the burned hook instead of adding one

@app.post("/api/hook")
async def add_hook(req: HookRequest, request: Request):
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[req.job_id]
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))
    
    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")
        
    with open(json_files[0], 'r') as f:
        data = json.load(f)
        
    clips = data.get('shorts', [])
    if req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")
        
    clip_data = clips[req.clip_index]
    
    # Video Path
    if req.input_filename:
        filename = os.path.basename(req.input_filename)
    else:
        filename = clip_data.get('video_url', '').split('/')[-1]
        if not filename:
             base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
             filename = f"{base_name}_clip_{req.clip_index+1}.mp4"
         
    input_path = os.path.join(output_dir, filename)
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")

    if not req.remove and not (req.text or "").strip():
        raise HTTPException(status_code=400, detail="Hook text is required")

    # Same invariant as /api/edit: derive from the clip WITHOUT its burned
    # captions, then put them back on top, so a later restyle never stacks a
    # second caption layer (see _reapply_captions). The hook layer is stripped
    # too: a new hook REPLACES the burned one (auto-hook or a previous manual
    # one) instead of stacking on top of it.
    clean_name = _strip_burned_captions(output_dir, filename, materialize=False)
    had_captions = clean_name != filename
    clean_name = _strip_burned_hook(output_dir, clean_name)
    filename = clean_name
    input_path = os.path.join(output_dir, clean_name)

    if req.remove:
        # Nothing to burn: the hook-less file is the target; captions (if the
        # clip had them) go back on below.
        output_filename = filename
        output_path = input_path
    else:
        output_filename = f"hooked_{int(time.time())}_{filename}"
        output_path = os.path.join(output_dir, output_filename)

        # Map Size to Scale
        size_map = {"S": 0.8, "M": 1.0, "L": 1.3}
        font_scale = size_map.get(req.size, 1.0)

        # Meter the FFmpeg overlay re-encode (no-op for BYOK / self-host).

        try:
            # Run in thread pool
            def run_hook():
                return add_hook_to_video(
                    input_path, req.text, output_path, position=req.position or "auto",
                    font_scale=font_scale, duration=req.duration_seconds, style=req.style,
                    font=req.font, layout_ranges=clip_data.get('layout_ranges'),
                    has_captions=had_captions)

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_hook)

        except Exception as e:
            print(f"❌ Hook Error: {e}")
            raise HTTPException(status_code=500, detail=str(e))


    # Captions back on top (see /api/edit for the same invariant).
    if had_captions:
        recap = await asyncio.get_event_loop().run_in_executor(
            None, _reapply_captions, req.job_id, req.clip_index, output_path)
        if recap:
            output_filename = os.path.basename(recap)

    output_filename = output_filename

    # Record the burned hook so the editor knows what the clip carries (the
    # auto-hook pipeline writes the same key).
    if req.remove:
        clip_data.pop('auto_hook', None)
    else:
        clip_data['auto_hook'] = {
            "text": req.text, "style": req.style, "font": req.font, "position": req.position,
            "duration_seconds": req.duration_seconds,
        }

    # Update Persistence (Same logic as subtitles)
    # Update InMemory Jobs
    if req.clip_index < len(job['result']['clips']):
        mem_clip = job['result']['clips'][req.clip_index]
        mem_clip['video_url'] = f"/videos/{req.job_id}/{output_filename}"
        if req.remove:
            mem_clip.pop('auto_hook', None)
        else:
            mem_clip['auto_hook'] = clip_data['auto_hook']

    # Update Metadata on Disk
    try:
        if req.clip_index < len(clips):
            clips[req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"
            data['shorts'] = clips
            write_json_atomic(json_files[0], data, indent=4)
            print(f"✅ Metadata updated with hook video for clip {req.clip_index}")
    except Exception as e:
        print(f"⚠️ Failed to update metadata.json: {e}")


    return {
        "success": True,
        "new_video_url": f"/videos/{req.job_id}/{output_filename}",
        "burned_hook": None if req.remove else clip_data['auto_hook'],
    }

class TranslateRequest(BaseModel):
    job_id: str
    clip_index: int
    target_language: str
    source_language: Optional[str] = None
    input_filename: Optional[str] = None


@app.post("/api/translate")
async def translate_clip(
    req: TranslateRequest,
    request: Request,
    x_elevenlabs_key: Optional[str] = Header(None, alias="X-ElevenLabs-Key")
):
    """Translate a video clip to a different language using ElevenLabs dubbing."""
    if not x_elevenlabs_key:
        raise HTTPException(status_code=400, detail="Missing X-ElevenLabs-Key header")

    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[req.job_id]
    output_dir = os.path.join(OUTPUT_DIR, req.job_id)
    json_files = glob.glob(os.path.join(output_dir, "*_metadata.json"))

    if not json_files:
        raise HTTPException(status_code=404, detail="Metadata not found")

    with open(json_files[0], 'r') as f:
        data = json.load(f)

    clips = data.get('shorts', [])
    if req.clip_index >= len(clips):
        raise HTTPException(status_code=404, detail="Clip not found")

    clip_data = clips[req.clip_index]

    # Video Path
    if req.input_filename:
        filename = os.path.basename(req.input_filename)
    else:
        filename = clip_data.get('video_url', '').split('/')[-1]
        if not filename:
             base_name = os.path.basename(json_files[0]).replace('_metadata.json', '')
             filename = f"{base_name}_clip_{req.clip_index+1}.mp4"

    input_path = os.path.join(output_dir, filename)
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail=f"Video file not found: {input_path}")

    # Output video with language suffix
    base, ext = os.path.splitext(filename)
    output_filename = f"translated_{req.target_language}_{base}{ext}"
    output_path = os.path.join(output_dir, output_filename)

    try:
        # Run translation in thread pool (blocking API calls)
        def run_translate():
            return translate_video(
                video_path=input_path,
                output_path=output_path,
                target_language=req.target_language,
                api_key=x_elevenlabs_key,
                source_language=req.source_language,
            )

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, run_translate)

    except Exception as e:
        print(f"❌ Translation Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # AI Act art. 50(2): the voice in this file was synthesised, so the file
    # says so in a way a machine can read. Best-effort by design — see
    # ffmpeg_utils.mark_ai_generated.
    from ffmpeg_utils import mark_ai_generated
    await loop.run_in_executor(
        None, lambda: mark_ai_generated(output_path, "AI voice dubbing"))

    output_filename = output_filename

    # Update InMemory Jobs
    if req.clip_index < len(job['result']['clips']):
         job['result']['clips'][req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"

    # Update Metadata on Disk
    try:
        if req.clip_index < len(clips):
            clips[req.clip_index]['video_url'] = f"/videos/{req.job_id}/{output_filename}"
            data['shorts'] = clips
            write_json_atomic(json_files[0], data, indent=4)
            print(f"✅ Metadata updated with translated video for clip {req.clip_index}")
    except Exception as e:
        print(f"⚠️ Failed to update metadata.json: {e}")


    return {
        "success": True,
        "new_video_url": f"/videos/{req.job_id}/{output_filename}"
    }

class SocialPostRequest(BaseModel):
    job_id: str
    clip_index: int
    api_key: Optional[str] = None  # Upload-Post key (else the header or env)
    user_id: Optional[str] = None  # Upload-Post profile
    platforms: List[str]  # social_platforms.PLATFORMS
    # Optional overrides if frontend wants to edit them
    title: Optional[str] = None
    description: Optional[str] = None
    scheduled_date: Optional[str] = None # ISO-8601 string
    timezone: Optional[str] = "UTC"

import httpx


def _post_video_blocking(url, headers, data, file_path, filename, timeout):
    """Multipart POST of a video to Upload-Post. Blocking: call it through
    asyncio.to_thread from a request handler, never inline."""
    with open(file_path, "rb") as f:
        files = {"video": (filename, f.read(), "video/mp4")}
    with httpx.Client(timeout=timeout) as client:
        return client.post(url, headers=headers, data=data, files=files)


import social_platforms
from social_platforms import youtube_title


def _checked_platforms(platforms) -> List[str]:
    """The request's platforms, or a 400 naming the ones OpenShorts does not post to."""
    bad = social_platforms.unknown(platforms)
    if bad or not platforms:
        raise HTTPException(status_code=400, detail=(
            f"Unsupported platform: {', '.join(map(str, bad))}. " if bad else "No platform chosen. ")
            + f"Choose from {', '.join(social_platforms.PLATFORMS)}.")
    return social_platforms.clean(platforms)


# Async uploads started here: request_id -> owner (always None now); in
# memory, an unknown id after a restart is "unknown".
_post_request_owners: dict = {}


@app.post("/api/social/post")
async def post_to_socials(req: SocialPostRequest, request: Request):
    req.platforms = _checked_platforms(req.platforms)
    if req.job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    # Resolve the Upload-Post key + profile.
    upload_key, forced_profile = await resolve_upload_post(request, req.api_key)
    if not upload_key:
        raise HTTPException(status_code=400, detail="Missing Upload-Post API key")
    post_user = resolve_post_profile(forced_profile, req.user_id)

    # The awaits above can let cleanup or an account delete drop the job.
    job = jobs.get(req.job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if 'result' not in job or 'clips' not in job['result']:
        raise HTTPException(status_code=400, detail="Job result not available")


    try:
        clip = job['result']['clips'][req.clip_index]
        # Video URL is relative /videos/..., we need absolute file path
        # clip['video_url'] is like "/videos/{job_id}/{filename}"
        # We constructed it as: f"/videos/{job_id}/{clip_filename}"
        # And file is at f"{OUTPUT_DIR}/{job_id}/{clip_filename}"
        
        filename = clip['video_url'].split('/')[-1]
        file_path = os.path.join(OUTPUT_DIR, req.job_id, filename)

        if not os.path.exists(file_path):
            # The recorded file is not there (an edit that failed used to
            # record a name it never wrote): post the newest file this clip
            # does have, and point the clip back at it.
            job_dir = os.path.join(OUTPUT_DIR, req.job_id)
            meta_files = glob.glob(os.path.join(job_dir, "*_metadata.json"))
            if meta_files:
                base_name = os.path.basename(meta_files[0]).replace('_metadata.json', '')
                newest = _canonical_clip_file(job_dir, base_name, req.clip_index)
                if os.path.exists(os.path.join(job_dir, newest)):
                    filename, file_path = newest, os.path.join(job_dir, newest)
                    clip['video_url'] = f"/videos/{req.job_id}/{newest}"
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="Video file not found")

        # Construct parameters for Upload-Post API
        # Fallbacks
        final_title = req.title or clip.get('title', 'Viral Short')
        final_description = req.description or clip.get('video_description_for_instagram') or clip.get('video_description_for_tiktok') or "Check this out!"
        
        # Prepare form data
        url = "https://api.upload-post.com/api/upload"
        headers = {
            "Authorization": f"Apikey {upload_key}"
        }

        # Prepare data as dict (httpx handles lists for multiple values)
        data_payload = {
            "user": post_user,
            "title": final_title,
            "platform[]": req.platforms, # Pass list directly
            "async_upload": "true"  # Enable async upload
        }

        # Add scheduling if present
        if req.scheduled_date:
            data_payload["scheduled_date"] = req.scheduled_date
            if req.timezone:
                data_payload["timezone"] = req.timezone
        
        data_payload.update(social_platforms.upload_fields(
            req.platforms, title=final_title, description=final_description,
            yt_title=req.title or clip.get('video_title_for_youtube_short', final_title),
            tiktok_post_mode=TIKTOK_POST_MODE))

        # The upload is a blocking multipart POST of the whole clip (tens of
        # seconds for TikTok+YouTube). Run inline, it froze the event loop:
        # 23-sep-2026 19:47:48 UTC one post stalled every request, /health
        # included, for 42 s and the uptime monitor paged "openshorts-api
        # down". It runs in a worker thread so the API keeps answering.
        print(f"📡 Sending to Upload-Post for platforms: {req.platforms}")
        response = await asyncio.to_thread(
            _post_video_blocking, url, headers, data_payload, file_path, filename, 120.0
        )

        if response.status_code not in [200, 201, 202]: # Added 201
             print(f"❌ Upload-Post Error: {response.text}")
             raise HTTPException(status_code=response.status_code, detail=f"Vendor API Error: {response.text}")

        out = response.json()
        if isinstance(out, dict) and out.get("request_id"):
            _post_request_owners[str(out["request_id"])] = None
        return out

    except Exception as e:
        print(f"❌ Social Post Exception: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/social/post-status/{request_id}")
async def social_post_status(request_id: str, request: Request):
    """Where an async upload started by /api/social/post really ended up.

    A 200 from the upload only means Upload-Post accepted the file; the post
    itself can still fail per platform, and the dashboard used to say "Posted
    successfully!" either way.
    """
    owner = _post_request_owners.get(request_id)
    if owner is None:
        return {"status": "unknown"}
    if owner != None:
        raise HTTPException(status_code=404, detail="Not found")
    upload_key, _ = await resolve_upload_post(request, None)
    if not upload_key:
        return {"status": "unknown"}
    async with social_platforms.upload_post_client(20.0) as client:
        try:
            resp = await client.get("https://api.upload-post.com/api/uploadposts/status",
                                    headers={"Authorization": f"Apikey {upload_key}"},
                                    params={"request_id": request_id})
            data = resp.json()
        except Exception:
            return {"status": "unknown"}
    results = [
        {"platform": r.get("platform"), "success": r.get("success"),
         "message": r.get("message") or r.get("error")}
        for r in (data.get("results") or []) if isinstance(r, dict)
    ] if isinstance(data, dict) else []
    status = (data or {}).get("status") or "unknown"
    _log_post_failures(request_id, status, results)
    return {"status": status, "results": results}


# request_ids whose failure is already in the log: the dashboard polls every
# 5 s, so the same failed post would otherwise be logged a dozen times.
_logged_post_failures: set = set()


def _log_post_failures(request_id, status, results):
    """One greppable line per failed platform of a post ("[social-post-failed]").

    The dashboard shows the reason to the user, but nothing kept it server-side:
    12 failures in a day (mostly TikTok, 25-oct-2026) left no trace of why.
    Platform, Upload-Post status and its message only: no user data.
    """
    failed = [r for r in results if r.get("success") is False]
    if status == "failed" and not failed:
        failed = [{"platform": "all", "message": "status failed without per-platform results"}]
    if not failed or request_id in _logged_post_failures:
        return
    if len(_logged_post_failures) > 5000:
        _logged_post_failures.clear()
    _logged_post_failures.add(request_id)
    for r in failed:
        msg = " ".join(str(r.get("message") or "").split())[:300]
        print(f"[social-post-failed] request={request_id} platform={r.get('platform')} "
              f"status={status} message={msg!r}")

SOCIAL_PLATFORMS = social_platforms.PLATFORMS


@app.get("/api/social/user")
async def get_social_user(request: Request):
    """Proxy to fetch the profiles of the caller's Upload-Post account."""
    api_key, _ = await resolve_upload_post(request, None)
    if not api_key:
         raise HTTPException(status_code=400, detail="Missing X-Upload-Post-Key header")


    url = "https://api.upload-post.com/api/uploadposts/users"
    print(f"🔍 Fetching User ID from: {url}")
    headers = {"Authorization": f"Apikey {api_key}"}
    
    async with social_platforms.upload_post_client(30.0) as client:
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                print(f"❌ Upload-Post User Fetch Error: {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail=f"Failed to fetch user: {resp.text}")
            
            data = resp.json()
            # Never log the body: it lists every profile on the account.
            _n = len(data.get('profiles') or []) if isinstance(data, dict) else 0
            print(f"🔍 Upload-Post users: {_n} profiles")
            
            # The structure is {'success': True, 'profiles': [{'username': '...'}, ...]}
            profiles_list = []
            if isinstance(data, dict):
                 raw_profiles = data.get('profiles', [])
                 if isinstance(raw_profiles, list):
                     for p in raw_profiles:
                         username = p.get('username')
                         if username:
                             # Determine connected platforms
                             socials = p.get('social_accounts', {})
                             connected = []
                             # Check typical platforms
                             for platform in SOCIAL_PLATFORMS:
                                 account_info = socials.get(platform)
                                 # If it's a dict and typically has data, or just not empty string
                                 if isinstance(account_info, dict):
                                     connected.append(platform)
                             
                             profiles_list.append({
                                 "username": username,
                                 "connected": connected
                             })
            
            if not profiles_list:
                # Fallback if no profiles found
                return {"profiles": [], "error": "No profiles found"}

            return {"profiles": profiles_list}
            
            
        except Exception as e:
             raise HTTPException(status_code=500, detail=str(e))




# --- Thumbnail Studio Endpoints ---


async def _probe_thumbnail_url(request, url, user):
    """Check a Thumbnail Studio link before anything downloads it.

    Returns ``(None, True)`` once the URL passes the same validation as
    /api/process."""
    if DISABLE_YOUTUBE_URL:
        raise HTTPException(status_code=403, detail="YouTube URL ingest is disabled on this deployment. Please upload a file you own.")
    await _validate_source_url(url)
    return None, True


async def _download_thumbnail_source(url, session_id, paid_allowed=True, cap_minutes=None):
    """Download a Thumbnail Studio link in a worker thread (never on the event
    loop) and record any per-GB proxy bytes in the ledger, like a job does."""
    from main import download_youtube_video
    route = {}
    loop = asyncio.get_event_loop()
    try:
        vpath, _ = await loop.run_in_executor(None, functools.partial(
            download_youtube_video, url, UPLOAD_DIR, allow_paid=paid_allowed,
            cap_minutes=cap_minutes, route_out=route))
    finally:
        pass
    return vpath


async def _save_thumbnail_upload(file, session_id):
    """Stream an uploaded file to UPLOAD_DIR (size-capped). Returns
    ``(path, None)``; the file is deleted when it is too big."""
    # basename() stops a "../../x" filename from escaping UPLOAD_DIR; the
    # chunked read caps memory so a huge body can't OOM.
    safe_name = _clamp_upload_name(os.path.basename(file.filename or "upload") or "upload")
    video_path = os.path.join(UPLOAD_DIR, f"thumb_{session_id}_{safe_name}")
    size = 0
    limit_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    with open(video_path, "wb") as buffer:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit_bytes:
                buffer.close()
                os.remove(video_path)
                raise HTTPException(status_code=413, detail=f"File too large. Max size {MAX_FILE_SIZE_MB}MB")
            buffer.write(chunk)
    return video_path, None


def _remove_quietly(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


@app.post("/api/thumbnail/upload")
async def thumbnail_upload(
    request: Request,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
):
    """Upload video and start background Whisper transcription immediately."""
    user = None
    url = (url or "").strip() or None
    if not url and not file:
        raise HTTPException(status_code=400, detail="Must provide URL or File")

    session_id = str(uuid.uuid4())
    transcript_event = asyncio.Event()

    # Everything is checked and reserved BEFORE the background download or
    # Whisper start: the URL, the source length and the balance.
    video_path = None
    source_minutes = None
    paid_allowed = True
    if file:
        url = None
        video_path, source_minutes = await _save_thumbnail_upload(file, session_id)
    else:
        source_minutes, paid_allowed = await _probe_thumbnail_url(request, url, user)

    # Meter the background download + Whisper transcription by the source's
    # length so an entitled user can't loop it for free. Settled when the job
    # finishes.
    try:
        pass
    except BaseException:
        _remove_quietly(video_path)
        raise

    # Initialize session
    _thumb_owner = None
    thumbnail_sessions[session_id] = {
        "user_id": _thumb_owner,
        "video_path": video_path,
        "transcript_event": transcript_event,
        "transcript_ready": False,
        "transcript": None,
        "transcript_segments": [],
        "video_duration": 0,
        "language": "en",
        "context": "",
        "titles": [],
        "conversation": [],
        "_url": url,  # Store URL for deferred download
    }

    async def run_background_whisper():
        try:
            vpath = video_path
            # Download the video if a URL was provided
            if not vpath and url:
                vpath = await _download_thumbnail_source(
                    url, session_id, paid_allowed=paid_allowed, cap_minutes=source_minutes)
                thumbnail_sessions[session_id]["video_path"] = vpath

            from main import transcribe_video
            loop = asyncio.get_event_loop()
            try:
                transcript = await loop.run_in_executor(None, transcribe_video, vpath)
            finally:
                # See transcribe_backends.release_models: the API process is
                # long-lived and the GPU is shared with every running job.
                import transcribe_backends
                await loop.run_in_executor(None, transcribe_backends.release_models)
            segments = transcript.get("segments", [])
            duration = segments[-1]["end"] if segments else 0

            thumbnail_sessions[session_id].update({
                "transcript_ready": True,
                "transcript": transcript,
                "transcript_segments": segments,
                "video_duration": duration,
                "language": transcript.get("language", "en"),
                "metered": True,  # the transcription was paid for
            })
            print(f"✅ [Thumbnail] Background Whisper complete for session {session_id}")
        except Exception as e:
            print(f"❌ [Thumbnail] Background Whisper failed: {e}")
            thumbnail_sessions[session_id]["transcript_error"] = str(e)
        finally:
            transcript_event.set()

    asyncio.create_task(run_background_whisper())

    return {"session_id": session_id}


@app.post("/api/thumbnail/analyze")
async def thumbnail_analyze(
    request: Request,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    session_id: Optional[str] = Form(None),
):
    """Analyze a video and suggest viral YouTube titles."""

    pre_transcript = None
    url = (url or "").strip() or None
    download_url = None
    paid_allowed = True
    source_minutes = None
    video_path = None

    # Check for pre-existing session with background Whisper
    if session_id and session_id in thumbnail_sessions:
        session = thumbnail_sessions[session_id]

        # Wait for background Whisper to complete
        transcript_event = session.get("transcript_event")
        if transcript_event:
            print(f"⏳ [Thumbnail] Waiting for background Whisper to finish...")
            await transcript_event.wait()

        if session.get("transcript_error"):
            raise HTTPException(status_code=500, detail=f"Transcription failed: {session['transcript_error']}")

        video_path = session["video_path"]
        if not video_path or not os.path.exists(video_path):
            raise HTTPException(status_code=404, detail="Video file not found in session")

        if session.get("transcript_ready"):
            pre_transcript = session["transcript"]
    else:
        # No pre-existing session — need file or URL
        if not url and not file:
            raise HTTPException(status_code=400, detail="Must provide URL, File, or session_id")

        session_id = str(uuid.uuid4())

        if file:
            video_path, source_minutes = await _save_thumbnail_upload(file, session_id)
        else:
            user = None
            source_minutes, paid_allowed = await _probe_thumbnail_url(request, url, user)
            download_url = url

    try:
        pass
    except BaseException:
        if not pre_transcript and video_path and session_id not in thumbnail_sessions:
            _remove_quietly(video_path)
        raise

    try:
        if download_url:
            video_path = await _download_thumbnail_source(
                download_url, session_id, paid_allowed=paid_allowed, cap_minutes=source_minutes)

        # Run analysis in thread pool (skips Whisper if pre_transcript is available)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, analyze_video_for_titles, video_path, pre_transcript)

        # Store/update session context
        if session_id not in thumbnail_sessions:
            thumbnail_sessions[session_id] = {"user_id": None}

        thumbnail_sessions[session_id].update({
            "context": result.get("transcript_summary", ""),
            "titles": result.get("titles", []),
            "thumbnail_texts": result.get("thumbnail_texts", []),
            "language": result.get("language", "en"),
            "conversation": thumbnail_sessions[session_id].get("conversation", []),
            "video_path": video_path,
            "transcript_segments": result.get("segments", []),
            "video_duration": result.get("video_duration", 0),
            # A metered analysis ran on this session: its follow-up text calls
            # (refine, describe) are part of what was paid for.
            "metered": True,
        })

        return {
            "session_id": session_id,
            "titles": result.get("titles", []),
            "thumbnail_texts": result.get("thumbnail_texts", []),
            "context": result.get("transcript_summary", ""),
            "language": result.get("language", "en"),
            "recommended": result.get("recommended", [])
        }

    except Exception as e:
        print(f"❌ Thumbnail Analyze Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Thumbnail Studio input limits (0 disables one).
THUMBNAIL_REFINES_PER_SESSION = _env_int("THUMBNAIL_REFINES_PER_SESSION", 40)
THUMBNAIL_MESSAGE_MAX_CHARS = _env_int("THUMBNAIL_MESSAGE_MAX_CHARS", 2000)
THUMBNAIL_TITLE_MAX_CHARS = _env_int("THUMBNAIL_TITLE_MAX_CHARS", 300)
THUMBNAIL_CONVERSATION_MAX_TURNS = _env_int("THUMBNAIL_CONVERSATION_MAX_TURNS", 20)
DESCRIBE_TRANSCRIPT_MAX_CHARS = _env_int("DESCRIBE_TRANSCRIPT_MAX_CHARS", 200000)


def _check_text_length(value, limit, field):
    if value and limit > 0 and len(value) > limit:
        raise HTTPException(status_code=413, detail=(
            f"The {field} is {len(value)} characters long; the limit is {limit}."))


class ThumbnailTitlesRequest(BaseModel):
    session_id: Optional[str] = None
    message: Optional[str] = None
    title: Optional[str] = None

@app.post("/api/thumbnail/titles")
async def thumbnail_titles(
    req: ThumbnailTitlesRequest,
    request: Request,
):
    """Refine title suggestions or accept a manual title."""
    _check_text_length(req.title, THUMBNAIL_TITLE_MAX_CHARS, "title")
    _check_text_length(req.message, THUMBNAIL_MESSAGE_MAX_CHARS, "message")

    # Manual title mode - just create a session with the user's title
    if req.title:
        session_id = _claim_thumbnail_session_id(req.session_id)
        if session_id not in thumbnail_sessions:
            thumbnail_sessions[session_id] = {
                "user_id": None,
                "context": "",
                "titles": [req.title],
                "language": "en",
                "conversation": []
            }
        else:
            pass
        return {"session_id": session_id, "titles": [req.title]}

    # Refinement mode
    if not req.session_id or req.session_id not in thumbnail_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    if not req.message:
        raise HTTPException(status_code=400, detail="Must provide message or title")

    session = thumbnail_sessions[req.session_id]
    refines = int(session.get("refines", 0))
    if refines >= THUMBNAIL_REFINES_PER_SESSION:
        raise HTTPException(status_code=429, detail=(
            f"This video has had {refines} title refinements, the most per analysis. "
            "Analyze the video again to keep refining."))
    session["refines"] = refines + 1

    # Add user message to conversation history; only the latest turns go back
    # to the model, so the prompt stays bounded however long the chat runs.
    session["conversation"].append({"role": "user", "content": req.message})
    del session["conversation"][:-THUMBNAIL_CONVERSATION_MAX_TURNS]

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            refine_titles,
            session["context"],
            req.message,
            session["conversation"]
        )

        new_titles = result.get("titles", [])
        session["titles"] = new_titles
        session["thumbnail_texts"] = result.get("thumbnail_texts", [])
        # The user may have asked for another language ("in English"): the
        # thumbnail text must follow the titles, not the transcript.
        if result.get("language"):
            session["language"] = result["language"]
        session["conversation"].append({"role": "assistant", "content": json.dumps(new_titles)})
        del session["conversation"][:-THUMBNAIL_CONVERSATION_MAX_TURNS]

        return {"titles": new_titles, "thumbnail_texts": session["thumbnail_texts"],
                "language": session["language"]}

    except Exception as e:
        print(f"❌ Thumbnail Titles Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/thumbnail/generate")
async def thumbnail_generate(
    request: Request,
    session_id: str = Form(...),
    title: str = Form(...),
    extra_prompt: str = Form(""),
    count: int = Form(3),
    burn_text: bool = Form(True),
    frame: str = Form(""),
    face: Optional[UploadFile] = File(None),
    background: Optional[UploadFile] = File(None),
):
    """Generate YouTube thumbnails with Gemini image generation.

    burn_text: the image model paints the scene with clean negative space and
    PIL sets the hook text (Anton, black stroke). Off = the model renders the
    text itself, which is prettier when it works and misspelled when it does
    not. frame: url of a frame from /api/thumbnail/frames to use as the
    person reference when no face photo is uploaded."""

    # The id becomes output/thumbnails/<id>: an unknown or malformed one
    # ("manual", "../x") gets a fresh id of its own instead of a shared folder.
    session_id = _claim_thumbnail_session_id(session_id)

    # Clamp count
    count = min(max(1, count), 6)

    # Save optional uploaded images. basename() on the session id and filenames
    # keeps everything inside UPLOAD_DIR (no "../" escape from client input).
    face_path = None
    bg_path = None
    safe_session = os.path.basename(session_id) or "session"
    thumb_upload_dir = os.path.join(UPLOAD_DIR, f"thumb_{safe_session}")
    os.makedirs(thumb_upload_dir, exist_ok=True)

    try:
        if face and face.filename:
            face_name = os.path.basename(face.filename)
            face_path = os.path.join(thumb_upload_dir, f"face_{face_name}")
            with open(face_path, "wb") as f:
                f.write(await face.read())

        if background and background.filename:
            bg_name = os.path.basename(background.filename)
            bg_path = os.path.join(thumb_upload_dir, f"bg_{bg_name}")
            with open(bg_path, "wb") as f:
                f.write(await background.read())

        # Session context: transcript summary, the thumbnail text the critic
        # paired with this title, the language, and the chosen video frame.
        video_context, text_hint, language, frame_reference = "", "", "en", None
        session = thumbnail_sessions.get(session_id)
        if session:
            video_context = session.get("context", "")
            language = session.get("language", "en")
            titles = session.get("titles", [])
            texts = session.get("thumbnail_texts", [])
            if title in titles and titles.index(title) < len(texts):
                text_hint = texts[titles.index(title)]
            if frame:
                frame_reference = next((f for f in session.get("frames", []) if f["url"] == frame), None)
            if frame_reference is None and not face_path:
                # No image model to invent a scene: build on the video's first face frame.
                frame_reference = next(iter(session.get("frames") or []), None)

        loop = asyncio.get_event_loop()
        thumbnails = await loop.run_in_executor(
            None,
            functools.partial(
                generate_thumbnail, title, session_id, face_path, bg_path,
                extra_prompt, count, video_context, burn_text=burn_text,
                thumbnail_text_hint=text_hint, language=language,
                frame_reference=frame_reference),
        )

        if not thumbnails:
            raise HTTPException(status_code=500, detail="Thumbnail generation failed. Please check your Gemini API key has access to image generation (gemini-3.1-flash-image-preview model).")

        # Success — charge the reserved minutes.
        return {"thumbnails": thumbnails}

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Thumbnail Generate Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/thumbnail/frames/{session_id}")
async def thumbnail_frames(session_id: str, request: Request):
    """Frames of the session's video with a large, sharp face, as candidates
    for the thumbnail's person reference. Computed once and cached."""
    session = thumbnail_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if "frames" in session:
        return {"frames": session["frames"]}

    # A URL session downloads in the background before transcribing; the
    # event fires after both, so waiting on it means the file is on disk.
    transcript_event = session.get("transcript_event")
    if transcript_event:
        await transcript_event.wait()
    video_path = session.get("video_path")
    if not video_path or not os.path.exists(video_path):
        return {"frames": []}

    lock = session.setdefault("_frames_lock", asyncio.Lock())
    async with lock:
        if "frames" not in session:
            loop = asyncio.get_event_loop()
            try:
                frames = await loop.run_in_executor(None, extract_face_frames, video_path, session_id)
            except Exception as e:
                print(f"❌ [Thumbnail] Frame extraction failed: {e}")
                frames = []
            session["frames"] = frames
    return {"frames": session["frames"]}


class ThumbnailDescribeRequest(BaseModel):
    session_id: str
    title: str

@app.post("/api/thumbnail/describe")
async def thumbnail_describe(
    req: ThumbnailDescribeRequest,
    request: Request,
):
    """Generate a YouTube description with chapters from the transcript."""

    _check_text_length(req.title, THUMBNAIL_TITLE_MAX_CHARS, "title")
    if req.session_id not in thumbnail_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = thumbnail_sessions[req.session_id]
    segments = session.get("transcript_segments", [])
    if not segments:
        raise HTTPException(status_code=400, detail="No transcript segments available. Please analyze a video first.")

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            generate_youtube_description,
            req.title,
            segments,
            session.get("language", "en"),
            session.get("video_duration", 0),
            DESCRIBE_TRANSCRIPT_MAX_CHARS,
        )
        return {"description": result.get("description", "")}

    except Exception as e:
        print(f"❌ Thumbnail Describe Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/thumbnail/publish")
async def thumbnail_publish(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: str = Form(...),
    title: str = Form(...),
    description: str = Form(...),
    thumbnail_url: str = Form(...),
    api_key: Optional[str] = Form(None),   # Upload-Post key (else the header or env)
    user_id: Optional[str] = Form(None),   # Upload-Post profile
):
    """Kick off a background upload to YouTube via Upload-Post and return immediately."""
    if session_id not in thumbnail_sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    upload_key, forced_profile = await resolve_upload_post(request, api_key)
    if not upload_key:
        raise HTTPException(status_code=400, detail="Missing Upload-Post API key")
    post_user = resolve_post_profile(forced_profile, user_id)

    session = thumbnail_sessions[session_id]
    video_path = session.get("video_path")
    if not video_path or not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="Original video file not found")

    # Resolve thumbnail path from URL — sanitize against path traversal so a
    # crafted thumbnail_url (e.g. "thumbnails/../../.env") can't read server
    # files and exfiltrate them via the Upload-Post multipart body.
    # Only an image of THIS session may be attached: resolving under all of
    # OUTPUT_DIR let "thumbnails/../<other job>/x.jpg" pick another tenant's file.
    thumb_relative = thumbnail_url.split("?", 1)[0].lstrip("/")
    if thumb_relative.startswith("thumbnails/"):
        thumb_relative = thumb_relative[len("thumbnails/"):]
    session_prefix = f"{session_id}/"
    thumb_path = None
    if thumb_relative.startswith(session_prefix) and \
            thumb_relative.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        thumb_path = _safe_under(os.path.join(THUMBNAILS_DIR, session_id),
                                 thumb_relative[len(session_prefix):])

    if not thumb_path:
        raise HTTPException(status_code=400, detail="Invalid thumbnail path")
    if not os.path.exists(thumb_path):
        raise HTTPException(status_code=404, detail="Thumbnail file not found")

    # Generate a unique ID for this publish job so the frontend can poll
    publish_id = str(uuid.uuid4())
    publish_jobs[publish_id] = {"status": "uploading", "result": None, "error": None,
                                "user_id": session.get("user_id")}

    def do_upload():
        """Runs in a thread via BackgroundTasks — does the actual multipart upload."""
        try:
            upload_url = "https://api.upload-post.com/api/upload"
            headers = {"Authorization": f"Apikey {upload_key}"}
            data_payload = {
                "user": post_user,
                "platform[]": ["youtube"],
                "title": title,          # required base field (fallback)
                "async_upload": "true",
                "youtube_title": youtube_title(title),
                "youtube_description": description,
                "privacyStatus": "public",
            }
            video_filename = os.path.basename(video_path)
            thumb_filename = os.path.basename(thumb_path)

            print(f"📡 [Thumbnail] Publishing to YouTube via Upload-Post... (publish_id={publish_id})")
            with open(video_path, "rb") as vf, open(thumb_path, "rb") as tf:
                files = {
                    "video": (video_filename, vf.read(), "video/mp4"),
                    "thumbnail": (thumb_filename, tf.read(), "image/jpeg"),
                }

            # Use a long timeout — video uploads can take several minutes
            with httpx.Client(timeout=600.0) as client:
                response = client.post(upload_url, headers=headers, data=data_payload, files=files)

            if response.status_code not in [200, 201, 202]:
                err = f"Upload-Post API Error ({response.status_code}): {response.text}"
                print(f"❌ {err}")
                publish_jobs[publish_id]["status"] = "failed"
                publish_jobs[publish_id]["error"] = err
            else:
                print(f"✅ [Thumbnail] Published successfully (publish_id={publish_id})")
                publish_jobs[publish_id]["status"] = "done"
                publish_jobs[publish_id]["result"] = response.json()

        except Exception as e:
            err = str(e)
            print(f"❌ Thumbnail Publish Background Error: {err}")
            publish_jobs[publish_id]["status"] = "failed"
            publish_jobs[publish_id]["error"] = err

    background_tasks.add_task(do_upload)
    return {"publish_id": publish_id, "status": "uploading"}


@app.get("/api/thumbnail/publish/status/{publish_id}")
async def thumbnail_publish_status(publish_id: str, request: Request):
    """Poll the status of a background publish job."""
    if publish_id not in publish_jobs:
        raise HTTPException(status_code=404, detail="Publish job not found")
    record = publish_jobs[publish_id]
    return {k: v for k, v in record.items() if k != "user_id"}
