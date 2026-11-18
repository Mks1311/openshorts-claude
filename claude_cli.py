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

import json
import os
import subprocess
import tempfile
from typing import Optional, Tuple, Type

from pydantic import BaseModel

DEFAULT_MODEL = "sonnet"
SYSTEM_PROMPT = "You answer with a single JSON object that matches the given schema, and nothing else."


def command(schema: Type[BaseModel], model: str) -> list:
    return [
        os.environ.get("CLAUDE_BIN") or "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(schema.model_json_schema()),
        "--system-prompt", SYSTEM_PROMPT,
        "--tools", "",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]


def ask(prompt: str, schema: Type[BaseModel], model: Optional[str] = None,
        timeout: float = 600.0) -> Tuple[dict, dict]:
    """One headless call whose answer must match ``schema``.

    Returns ``(parsed_dict, cost_analysis)`` like ``llm_backend.generate_json``.
    Raises RuntimeError on a CLI or API failure; the message carries the API
    status (``429``, ``529``...) so ``main._run_gemini_stage`` can tell
    transient errors apart. The retry policy stays with the caller.
    """
    model = model or DEFAULT_MODEL
    with tempfile.TemporaryDirectory(prefix="claude-call-") as cwd:
        proc = subprocess.run(command(schema, model), input=prompt, capture_output=True,
                              text=True, cwd=cwd, timeout=timeout)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(f"claude CLI exited {proc.returncode}: "
                           f"{(proc.stderr or proc.stdout).strip()[:300]}") from None
    if data.get("is_error") or data.get("structured_output") is None:
        raise RuntimeError(
            f"claude CLI error (status {data.get('api_error_status')}, "
            f"{data.get('terminal_reason') or data.get('subtype')}): {str(data.get('result'))[:300]}")
    validated = schema.model_validate(data["structured_output"]).model_dump()

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
