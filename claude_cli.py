"""Claude through the logged-in ``claude`` CLI (headless ``claude -p``).

Runs on the Claude subscription the CLI is signed in to (``claude`` once,
interactively, or ``CLAUDE_CODE_OAUTH_TOKEN`` from ``claude setup-token``), so
no API key is needed. ``llm_backend`` sends its calls here when
``LLM_PROVIDER=claude``.

Each call is isolated: own system prompt, no tools, no settings, hooks, MCP
servers or slash commands, and a scratch working directory so no CLAUDE.md is
read. That keeps a call at ~1.5k input tokens instead of ~6k, which is what a
subscription's usage limit counts.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from typing import Optional, Sequence, Tuple, Type, Union

from pydantic import BaseModel

DEFAULT_MODEL = "sonnet"
# Frame questions are closed choices or short rewrites: the small model keeps
# them cheap on the subscription's usage limit.
VISION_MODEL = "haiku"
SYSTEM_PROMPT = "You answer with a single JSON object that matches the given schema, and nothing else."
FREE_SYSTEM_PROMPT = "You answer with exactly the output the request asks for, and nothing else."


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


def ask(contents: Union[str, Sequence[Union[str, bytes]]], schema: Optional[Type[BaseModel]],
        model: Optional[str] = None, timeout: float = 600.0) -> Tuple[Union[dict, str], dict]:
    """One headless call whose answer must match ``schema``. ``contents`` is a
    prompt, or a list of text strings and JPEG frames (bytes) in order. With
    ``schema=None`` the answer is returned as the model's text.

    Returns ``(parsed_dict, cost_analysis)`` like ``llm_backend.generate_json``.
    Raises RuntimeError on a CLI or API failure; the message carries the API
    status (``429``, ``529``...) so ``main._run_gemini_stage`` can tell
    transient errors apart. The retry policy stays with the caller.
    """
    model = model or DEFAULT_MODEL
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

    usage = data.get("usage") or {}
    cost = {
        "input_tokens": int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
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
