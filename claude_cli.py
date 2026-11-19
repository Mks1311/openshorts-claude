"""Claude through the logged-in ``claude`` CLI (headless ``claude -p``).

Runs on the Claude subscription the CLI is signed in to (``claude`` once,
interactively, or ``CLAUDE_CODE_OAUTH_TOKEN`` from ``claude setup-token``), so
no API key is needed. ``llm_backend`` sends its calls here when
``LLM_PROVIDER=claude``.

Each call is isolated: own system prompt, no tools, no settings, hooks, MCP
servers or slash commands, and a scratch working directory so no CLAUDE.md is
read. That keeps a call at ~1.5k input tokens instead of ~6k, which is what a
subscription's usage limit counts.

Every call belongs to a task (``TASKS``): the model it runs on can be picked
per task in Settings (``output/.models.json``), and its tokens are recorded in
the server's usage log and, inside a job, in the job's own (``USAGE_LOG``).
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
from typing import Optional, Sequence, Tuple, Type, Union

from pydantic import BaseModel

DEFAULT_MODEL = "sonnet"
# Frame questions are closed choices or short rewrites: the small model keeps
# them cheap on the subscription's usage limit.
VISION_MODEL = "haiku"

# Every kind of call: its label and help in Settings, and its default model.
# Text tasks fall back to LLM_MODEL before the default, as they always did.
TASKS = {
    "clips": ("Pick moments", "Reads the transcript and ranks the stretches worth a short.", DEFAULT_MODEL),
    "visual_clips": ("Pick moments (silent video)", "Watches frames when the video has no speech.", DEFAULT_MODEL),
    "layout": ("Layout", "Chooses single, split or screen layouts from frames.", VISION_MODEL),
    "hooks": ("Hooks", "Writes each clip's hook title from what it shows.", VISION_MODEL),
    "subtitles": ("Burned-in subtitle check", "Looks for captions already in the video.", VISION_MODEL),
    "edits": ("Auto edit", "Plans zooms, cuts and effects for a clip.", VISION_MODEL),
    "studio": ("YouTube Studio", "Titles, thumbnail text and descriptions.", DEFAULT_MODEL),
    "pro_mode": ("Pro mode", "Titles for the talking-head re-edit.", DEFAULT_MODEL),
}
TEXT_TASKS = {"clips", "visual_clips", "studio", "pro_mode"}
# What Settings offers. The aliases follow the newest model of each family.
MODEL_CHOICES = ["haiku", "sonnet", "opus",
                 "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"]
# Any model name the CLI might take, and nothing a command line could misread.
_MODEL_NAME = re.compile(r"^[a-z0-9][a-z0-9.\-\[\]]{0,63}$")

OUTPUT_DIR = "output"
# Server state, not a job: the sweeps and size caps skip dot entries of output/.
SETTINGS_FILE = os.path.join(OUTPUT_DIR, ".models.json")
USAGE_FILE = os.path.join(OUTPUT_DIR, ".usage.jsonl")
USAGE_MAX_BYTES = 5 * 1024 * 1024

SYSTEM_PROMPT = "You answer with a single JSON object that matches the given schema, and nothing else."
FREE_SYSTEM_PROMPT = "You answer with exactly the output the request asks for, and nothing else."


def valid_model(name) -> bool:
    return isinstance(name, str) and bool(_MODEL_NAME.match(name))


def model_overrides() -> dict:
    """task -> model picked in Settings (valid entries only)."""
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in TASKS and valid_model(v)}


def model_for(task: Optional[str]) -> str:
    """The model a task runs on: Settings, else LLM_MODEL for a text task, else its default."""
    picked = model_overrides().get(task)
    if picked:
        return picked
    env = (os.environ.get("LLM_MODEL") or "").strip()
    if task in TEXT_TASKS and valid_model(env):
        return env
    return TASKS[task][2] if task in TASKS else DEFAULT_MODEL


def _append(path: str, record: dict) -> None:
    try:
        if path == USAGE_FILE and os.path.getsize(path) > USAGE_MAX_BYTES:
            os.replace(path, path + ".1")  # keep one older file, drop the one before
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def record_usage(record: dict) -> None:
    """One call, to the server's usage log and, inside a job, to the job's."""
    _append(USAGE_FILE, record)
    job_log = os.environ.get("USAGE_LOG")
    if job_log:
        _append(job_log, record)


def read_usage(path: str, since: float = 0.0) -> list:
    """The usage records of one log from ``since`` (epoch seconds) on."""
    records = []
    for name in ((path + ".1", path) if path == USAGE_FILE else (path,)):
        try:
            with open(name) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        if isinstance(r, dict) and float(r.get("at") or 0) >= since:
                            records.append(r)
                    except (ValueError, TypeError):
                        continue
        except OSError:
            continue
    return records


def summarize(records) -> dict:
    """Totals and a per-task breakdown of usage records. Records may come from
    disk, so only numbers and known names get through."""
    out = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0, "tasks": {}}
    for r in records:
        if not isinstance(r, dict):
            continue
        try:
            tin, tout = int(r.get("input_tokens") or 0), int(r.get("output_tokens") or 0)
            secs = float(r.get("seconds") or 0)
        except (TypeError, ValueError):
            continue
        task = r.get("task") if r.get("task") in TASKS else "other"
        model = r.get("model") if valid_model(r.get("model")) else "unknown"
        t = out["tasks"].setdefault(task, {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                                           "seconds": 0.0, "failed": 0, "models": {}})
        for agg in (out, t):
            agg["calls"] += 1
            agg["input_tokens"] += tin
            agg["output_tokens"] += tout
            agg["seconds"] = round(agg["seconds"] + secs, 1)
        if r.get("ok") is False:
            t["failed"] += 1
        t["models"][model] = t["models"].get(model, 0) + 1
    return out


def command(schema: Optional[Type[BaseModel]], model: str) -> list:
    shape = (["--json-schema", json.dumps(schema.model_json_schema()), "--system-prompt", SYSTEM_PROMPT]
             if schema is not None else ["--system-prompt", FREE_SYSTEM_PROMPT])
    return [
        os.environ.get("CLAUDE_BIN") or "claude", "-p",
        "--model", model,
        "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose",  # stream-json output requires --verbose
        *shape,
        "--tools", "",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]


def user_message(contents: Union[str, Sequence[Union[str, bytes]]]) -> str:
    """One stream-json user turn: text strings and JPEG bytes, in order."""
    if isinstance(contents, str):
        contents = [contents]
    blocks = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                           "data": base64.b64encode(c).decode()}}
              if isinstance(c, bytes) else {"type": "text", "text": c}
              for c in contents]
    return json.dumps({"type": "user", "message": {"role": "user", "content": blocks}})


def _result_event(stdout: str) -> Optional[dict]:
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


def _log_call(task, model, usage, started, ok):
    """Record one call and print it for the job log; returns (input, output) tokens."""
    tokens_in = (int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
                 + int(usage.get("cache_read_input_tokens") or 0))
    tokens_out = int(usage.get("output_tokens") or 0)
    seconds = round(time.time() - started, 1)
    record_usage({"at": round(started, 3), "task": task or "other", "model": model,
                  "input_tokens": tokens_in, "output_tokens": tokens_out,
                  "seconds": seconds, "ok": ok})
    print(f"🤖 [claude] {task or 'other'} · {model} · {tokens_in:,} in / {tokens_out:,} out tokens"
          f" · {seconds:g}s{'' if ok else ' · failed'}", flush=True)
    return tokens_in, tokens_out


def ask(contents: Union[str, Sequence[Union[str, bytes]]], schema: Optional[Type[BaseModel]],
        model: Optional[str] = None, timeout: float = 600.0,
        task: Optional[str] = None) -> Tuple[Union[dict, str], dict]:
    """One headless call whose answer must match ``schema``. ``contents`` is a
    prompt, or a list of text strings and JPEG frames (bytes) in order. With
    ``schema=None`` the answer is returned as the model's text. ``model``
    defaults to the one Settings gives ``task``.

    Returns ``(parsed_dict, cost_analysis)`` like ``llm_backend.generate_json``.
    Raises RuntimeError on a CLI or API failure; the message carries the API
    status (``429``, ``529``...) so ``main._run_gemini_stage`` can tell
    transient errors apart. The retry policy stays with the caller. Every
    call, failed or not, is recorded under ``task``.
    """
    model = model or model_for(task)
    started = time.time()
    data = None
    try:
        with tempfile.TemporaryDirectory(prefix="claude-call-") as cwd:
            proc = subprocess.run(command(schema, model), input=user_message(contents),
                                  capture_output=True, text=True, cwd=cwd, timeout=timeout)
        data = _result_event(proc.stdout)
        if data is None:
            raise RuntimeError(f"claude CLI exited {proc.returncode}: "
                               f"{(proc.stderr or proc.stdout).strip()[:300]}")
        answer = data.get("structured_output") if schema is not None else data.get("result")
        if data.get("is_error") or answer is None:
            raise RuntimeError(
                f"claude CLI error (status {data.get('api_error_status')}, "
                f"{data.get('terminal_reason') or data.get('subtype')}): {str(data.get('result'))[:300]}")
        validated = schema.model_validate(answer).model_dump() if schema is not None else answer
    except Exception:
        _log_call(task, model, (data or {}).get("usage") or {}, started, ok=False)
        raise

    tokens_in, tokens_out = _log_call(task, model, data.get("usage") or {}, started, ok=True)
    cost = {
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "thinking_tokens": 0,
        # Counted against the subscription's usage limit, not billed.
        "input_cost": 0.0,
        "output_cost": 0.0,
        "total_cost": 0.0,
        "model": model,
        "price_estimated": False,
        "local": True,
    }
    return validated, cost
