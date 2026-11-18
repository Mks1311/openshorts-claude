"""The OpenAI-compatible moment-picker backend (llm_backend.py).

Self-hosters asked for a pipeline that never calls Google. The transcript
passes go to any /chat/completions server when LLM_BASE_URL is set; these
tests pin the contract main.py relies on: same (parsed, cost) shape as the
Gemini stage, schema validation, and the response_format fallback ladder.
"""
import base64
import json
import os
import subprocess

import httpx
import pytest

import claude_cli
import gemini_worker
import llm_backend


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen2.5:14b")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)


def _serve(handler, monkeypatch):
    """Route llm_backend's httpx client through an in-process handler."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(llm_backend, "_client",
                        lambda **kw: httpx.Client(transport=transport, **kw))


def _completion(payload, usage=None):
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}],
        "usage": usage or {"prompt_tokens": 120, "completion_tokens": 40},
    })


# --- activation -----------------------------------------------------------

def test_inactive_without_a_base_url(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert llm_backend.active() is False
    assert llm_backend.describe() is None


def test_base_url_alone_activates_and_describes(local):
    assert llm_backend.active() is True
    assert llm_backend.describe() == {
        "provider": "openai", "model": "qwen2.5:14b", "baseUrl": "http://llm.test/v1"}


def test_explicit_gemini_provider_wins_over_base_url(local, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert llm_backend.active() is False


# --- generate_json ---------------------------------------------------------

def test_returns_validated_payload_and_zero_cost(local, monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return _completion({"windows": [{"id": "w0", "start": 0, "end": 90, "score": 88, "reason": "hook"}]})

    _serve(handler, monkeypatch)
    parsed, cost = llm_backend.generate_json("prompt", gemini_worker.ScoreResponse)

    assert seen["url"] == "http://llm.test/v1/chat/completions"
    assert seen["body"]["model"] == "qwen2.5:14b"
    assert seen["body"]["response_format"]["type"] == "json_schema"
    assert seen["auth"] == "Bearer ollama"  # documented placeholder when no key is set
    assert parsed["windows"][0]["score"] == 88
    assert cost["total_cost"] == 0.0 and cost["local"] is True
    assert cost["input_tokens"] == 120 and cost["output_tokens"] == 40


def test_falls_back_to_json_object_when_schema_mode_is_rejected(local, monkeypatch):
    formats = []

    def handler(request):
        body = json.loads(request.content)
        fmt = (body.get("response_format") or {}).get("type")
        formats.append(fmt)
        if fmt == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format json_schema not supported"}})
        return _completion({"windows": []})

    _serve(handler, monkeypatch)
    parsed, _ = llm_backend.generate_json("prompt", gemini_worker.ScoreResponse)
    assert formats == ["json_schema", "json_object"]
    assert parsed == {"windows": []}


def test_code_fenced_json_is_accepted(local, monkeypatch):
    """Small models wrap the object in ```json fences even when told not to."""
    def handler(request):
        text = "```json\n" + json.dumps({"windows": []}) + "\n```"
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}], "usage": {}})

    _serve(handler, monkeypatch)
    parsed, _ = llm_backend.generate_json("prompt", gemini_worker.ScoreResponse)
    assert parsed == {"windows": []}


def test_schema_violation_raises_instead_of_leaking_into_the_pipeline(local, monkeypatch):
    _serve(lambda r: _completion({"windows": [{"id": "w0"}]}), monkeypatch)
    with pytest.raises(Exception) as exc:
        llm_backend.generate_json("prompt", gemini_worker.ScoreResponse)
    assert "validation error" in str(exc.value)


def test_server_errors_surface_with_status_and_url(local, monkeypatch):
    _serve(lambda r: httpx.Response(503, text="loading model"), monkeypatch)
    with pytest.raises(RuntimeError) as exc:
        llm_backend.generate_json("prompt", gemini_worker.ScoreResponse)
    assert "503" in str(exc.value) and "loading model" in str(exc.value)


# --- main.py routing (needs the heavy deps; skipped on minimal CI) ------------

def test_stage_routes_to_local_backend_and_retries_transient(local, monkeypatch):
    main = pytest.importorskip("main")
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_generate(prompt, schema, model=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("ConnectError: connection refused")
        return {"windows": [{"id": "w0", "start": 0, "end": 1, "score": 50, "reason": ""}]}, {"total_cost": 0.0}

    monkeypatch.setattr(llm_backend, "generate_json", fake_generate)
    parsed, cost = main._run_gemini_stage(None, "qwen2.5:14b", "prompt", gemini_worker.ScoreResponse)
    assert calls["n"] == 2
    assert parsed["windows"][0]["score"] == 50
    assert cost["total_cost"] == 0.0


def test_score_batch_shrinks_for_local_models(local, monkeypatch):
    main = pytest.importorskip("main")
    monkeypatch.delenv("LLM_SCORE_BATCH", raising=False)
    assert main.score_batch_size() == 3
    monkeypatch.setenv("LLM_SCORE_BATCH", "5")
    assert main.score_batch_size() == 5
    monkeypatch.delenv("LLM_BASE_URL")
    monkeypatch.delenv("LLM_SCORE_BATCH")
    assert main.score_batch_size() == 8


# --- Claude through the headless CLI (LLM_PROVIDER=claude) ----------------

@pytest.fixture
def claude(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
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


def test_claude_provider_activates_without_a_base_url(claude):
    assert llm_backend.active() is True
    assert llm_backend.describe() == {"provider": "claude", "model": "sonnet", "baseUrl": ""}


def test_claude_call_is_isolated_schema_bound_and_validated(claude, monkeypatch):
    windows = [{"id": "w0", "start": 0, "end": 90, "score": 88, "reason": "hook"}]
    seen = _cli(monkeypatch, {"is_error": False, "structured_output": {"windows": windows},
                              "usage": {"input_tokens": 10, "cache_read_input_tokens": 1500,
                                        "output_tokens": 40}})
    parsed, cost = llm_backend.generate_json("score these", gemini_worker.ScoreResponse)

    cmd = seen["cmd"]
    message = json.loads(seen["input"])  # prompt over stdin as one stream-json turn, not argv
    assert message["message"]["content"] == [{"type": "text", "text": "score these"}]
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == \
        gemini_worker.ScoreResponse.model_json_schema()
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
        llm_backend.generate_json("x", gemini_worker.ScoreResponse)


def test_claude_cli_without_json_output_raises_its_stderr(claude, monkeypatch):
    _cli(monkeypatch, "", returncode=1, stderr="Not logged in")
    with pytest.raises(RuntimeError, match="Not logged in"):
        llm_backend.generate_json("x", gemini_worker.ScoreResponse)


def test_claude_answer_missing_a_field_fails_validation(claude, monkeypatch):
    _cli(monkeypatch, {"is_error": False, "structured_output": {"windows": [{"id": "w0"}]}})
    with pytest.raises(ValueError, match="validation error"):
        llm_backend.generate_json("x", gemini_worker.ScoreResponse)


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
    answer = llm_backend.ask_frames(["Shot 0:", b"\xff\xd8jpg", "pick"], gemini_worker.LayoutChoice)

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


def test_claude_sees_frames_without_a_gemini_key(claude, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert llm_backend.sees() is True
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert llm_backend.sees() is False


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
    assert asked["schema"] is gemini_worker.LayoutChoice


def test_thumbnail_text_goes_to_claude_without_a_schema(claude, monkeypatch):
    thumbnail = pytest.importorskip("thumbnail")
    seen = _cli(monkeypatch, _ok('{"titles": ["A"], "thumbnail_texts": ["B"], "language": "en"}',
                                 structured=False))
    out = thumbnail.refine_titles(None, "ctx", "shorter please")
    assert out == {"titles": ["A"], "thumbnail_texts": ["B"], "language": "en"}
    assert "--json-schema" not in seen["cmd"]
