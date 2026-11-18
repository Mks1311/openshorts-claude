"""Every model call goes to Claude through the headless ``claude`` CLI
(llm_backend.py, claude_cli.py): the call is isolated and schema-bound, the
answer validated, errors carry what the retry policy needs, and frames go as
image blocks in order.
"""
import base64
import json
import os
import subprocess

import pytest

import claude_cli
import llm_prompts
import llm_backend


# --- the headless CLI ------------------------------------------------------

@pytest.fixture
def claude(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)


def _cli(monkeypatch, payload, returncode=0, stderr=""):
    """Stand in for the `claude -p` process; records its command, stdin and cwd."""
    seen = {}

    def fake_run(cmd, input=None, **kw):
        seen.update(cmd=cmd, input=input, cwd=kw.get("cwd"))
        out = payload if isinstance(payload, str) else json.dumps({"type": "result", **payload})
        return subprocess.CompletedProcess(cmd, returncode, out, stderr)

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    return seen


def test_describes_claude_and_its_model(claude):
    assert llm_backend.describe() == {"provider": "claude", "model": "sonnet", "baseUrl": ""}


def test_stage_retries_a_transient_error(claude, monkeypatch):
    main = pytest.importorskip("main")
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_generate(prompt, schema, model=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("claude CLI error (status 529, api_error): Overloaded")
        return {"windows": [{"id": "w0", "start": 0, "end": 1, "score": 50, "reason": ""}]}, {"total_cost": 0.0}

    monkeypatch.setattr(llm_backend, "generate_json", fake_generate)
    parsed, cost = main._run_llm_stage("sonnet", "prompt", llm_prompts.ScoreResponse)
    assert calls["n"] == 2
    assert parsed["windows"][0]["score"] == 50


def test_claude_call_is_isolated_schema_bound_and_validated(claude, monkeypatch):
    windows = [{"id": "w0", "start": 0, "end": 90, "score": 88, "reason": "hook"}]
    seen = _cli(monkeypatch, {"is_error": False, "structured_output": {"windows": windows},
                              "usage": {"input_tokens": 10, "cache_read_input_tokens": 1500,
                                        "output_tokens": 40}})
    parsed, cost = llm_backend.generate_json("score these", llm_prompts.ScoreResponse)

    cmd = seen["cmd"]
    message = json.loads(seen["input"])  # prompt over stdin as one stream-json turn, not argv
    assert message["message"]["content"] == [{"type": "text", "text": "score these"}]
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == \
        llm_prompts.ScoreResponse.model_json_schema()
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--setting-sources") + 1] == ""
    assert seen["cwd"] != os.getcwd()  # a scratch dir: this repo's CLAUDE.md stays out
    assert parsed["windows"] == windows
    assert cost["input_tokens"] == 1510 and cost["output_tokens"] == 40
    assert cost["total_cost"] == 0.0 and cost["model"] == "sonnet"


def test_claude_api_error_carries_its_status_for_the_retry_policy(claude, monkeypatch):
    _cli(monkeypatch, {"is_error": True, "api_error_status": 529,
                       "terminal_reason": "api_error", "result": "Overloaded"})
    with pytest.raises(RuntimeError, match="529"):
        llm_backend.generate_json("x", llm_prompts.ScoreResponse)


def test_claude_cli_without_json_output_raises_its_stderr(claude, monkeypatch):
    _cli(monkeypatch, "", returncode=1, stderr="Not logged in")
    with pytest.raises(RuntimeError, match="Not logged in"):
        llm_backend.generate_json("x", llm_prompts.ScoreResponse)


def test_claude_answer_missing_a_field_fails_validation(claude, monkeypatch):
    _cli(monkeypatch, {"is_error": False, "structured_output": {"windows": [{"id": "w0"}]}})
    with pytest.raises(ValueError, match="validation error"):
        llm_backend.generate_json("x", llm_prompts.ScoreResponse)


def test_claude_keeps_the_full_score_batch(claude, monkeypatch):
    main = pytest.importorskip("main")
    monkeypatch.delenv("LLM_SCORE_BATCH", raising=False)
    assert main.score_batch_size() == 8



# --- frames and free text (phase 2) ---------------------------------------

def _ok(answer, structured=True):
    key = "structured_output" if structured else "result"
    return "\n".join([json.dumps({"type": "system", "subtype": "init"}),
                      json.dumps({"type": "result", "is_error": False, key: answer})])


def test_frames_go_as_jpeg_blocks_in_order_on_the_vision_model(claude, monkeypatch):
    seen = _cli(monkeypatch, _ok({"layout": "split", "confidence": 0.9, "why": "two faces"}))
    answer = llm_backend.ask_frames(["Shot 0:", b"\xff\xd8jpg", "pick"], llm_prompts.LayoutChoice)

    blocks = json.loads(seen["input"])["message"]["content"]
    assert [b["type"] for b in blocks] == ["text", "image", "text"]
    assert blocks[1]["source"] == {"type": "base64", "media_type": "image/jpeg",
                                   "data": base64.b64encode(b"\xff\xd8jpg").decode()}
    cmd = seen["cmd"]
    assert cmd[cmd.index("--model") + 1] == claude_cli.VISION_MODEL
    assert answer["layout"] == "split"


def test_without_a_schema_the_answer_is_the_text(claude, monkeypatch):
    seen = _cli(monkeypatch, _ok("Chapters:\n0:00 Intro", structured=False))
    text, _ = claude_cli.ask(["write it"], None)
    assert text == "Chapters:\n0:00 Intro"
    assert "--json-schema" not in seen["cmd"]


def test_layout_picker_asks_claude_with_its_frames(claude, monkeypatch):
    import layout_picker
    monkeypatch.setattr(layout_picker, "ENABLED", True)
    monkeypatch.setattr(layout_picker, "sample_frames", lambda *a, **k: [b"f1", b"f2"])
    asked = {}

    def fake(contents, schema):
        asked.update(contents=contents, schema=schema)
        return {"layout": "split", "confidence": 0.8, "why": "two people"}

    monkeypatch.setattr(llm_backend, "ask_frames", fake)
    assert layout_picker.pick("v.mp4", 60) == "split"
    assert asked["contents"][:2] == [b"f1", b"f2"]
    assert asked["schema"] is llm_prompts.LayoutChoice


def test_thumbnail_text_goes_to_claude_without_a_schema(claude, monkeypatch):
    thumbnail = pytest.importorskip("thumbnail")
    seen = _cli(monkeypatch, _ok('{"titles": ["A"], "thumbnail_texts": ["B"], "language": "en"}',
                                 structured=False))
    out = thumbnail.refine_titles("ctx", "shorter please")
    assert out == {"titles": ["A"], "thumbnail_texts": ["B"], "language": "en"}
    assert "--json-schema" not in seen["cmd"]
