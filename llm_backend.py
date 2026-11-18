"""The model behind every AI step: Claude through the headless ``claude`` CLI.

``claude_cli.py`` runs ``claude -p`` on the subscription the CLI is signed in
to, so no API key is needed. Text questions (moment picking, titles) use
``LLM_MODEL`` (default ``sonnet``); frame questions (layout, screen check,
burned-in subtitles, hooks, edit plans) use ``claude_cli.VISION_MODEL``.
Answers are validated with the caller's pydantic schema, so ``main.py`` sees
one shape everywhere.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple, Type

from pydantic import BaseModel

DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT = 600.0  # a long scoring batch can take minutes


def model_name() -> str:
    return (os.environ.get("LLM_MODEL") or "").strip() or DEFAULT_MODEL


def describe() -> dict:
    """What ``/api/config`` tells the dashboard."""
    return {"provider": "claude", "model": model_name(), "baseUrl": ""}


def _timeout() -> float:
    try:
        return float(os.environ.get("LLM_TIMEOUT") or DEFAULT_TIMEOUT)
    except ValueError:
        return DEFAULT_TIMEOUT


def generate_json(prompt: str, schema: Type[BaseModel], model: Optional[str] = None,
                  ) -> Tuple[dict, Optional[dict]]:
    """One text call that must come back as JSON matching ``schema``:
    ``(parsed_dict, cost_analysis)``. Raises on failure; the retry policy
    lives in the caller."""
    import claude_cli
    return claude_cli.ask(prompt, schema, model=model or model_name(), timeout=_timeout())


def ask_frames(contents, schema: Type[BaseModel]) -> dict:
    """One call over text strings and JPEG frames (bytes), in order, answered
    as a dict matching ``schema``. Raises on any failure; each caller has its
    own fallback."""
    import claude_cli
    return claude_cli.ask(contents, schema, model=claude_cli.VISION_MODEL,
                          timeout=_timeout())[0]
