"""Silent-video clipping, the effects editor and thumbnails on Claude.

Claude cannot take a video file, so both send timestamped frames instead:
these tests pin the frame sampling, the per-stretch time offsets and the
editor's frames-instead-of-upload contract.
"""
import shutil
import subprocess

import pytest

import claude_cli
import frame_sampler


@pytest.fixture
def claude(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_jpegs_every_samples_on_the_step_from_the_start(tmp_path):
    video = str(tmp_path / "v.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=320x180:r=10:d=10",
                    "-pix_fmt", "yuv420p", video], check=True)
    frames = frame_sampler.jpegs_every(video, 2, 8, 2, width=160)
    assert [t for t, _ in frames] == [0, 2, 4]
    assert all(jpg[:2] == b"\xff\xd8" for _, jpg in frames)


def test_unreadable_video_gives_no_frames(tmp_path):
    assert frame_sampler.jpegs_every(str(tmp_path / "missing.mp4"), 0, 10, 2) == []


def test_timed_labels_each_frame_before_it():
    assert frame_sampler.timed([(0, b"a"), (2.5, b"b")]) == ["t=0s", b"a", "t=2.5s", b"b"]


def test_silent_video_stretches_are_asked_by_frames_and_shifted(claude, monkeypatch):
    main = pytest.importorskip("main")
    asked = []
    monkeypatch.setattr(frame_sampler, "jpegs_every",
                        lambda path, s0, s1, step, width=512: asked.append((s0, s1)) or [(0, b"j")])

    def fake_ask(contents, schema, model=None, timeout=600.0, task=None):
        assert contents[:2] == ["t=0s", b"j"]
        return {"shorts": [{"start": 10, "end": 40, "predicted_score": 80,
                            "video_description_for_tiktok": "", "video_description_for_instagram": "",
                            "video_title_for_youtube_short": "t", "viral_hook_text": "h"}]}, {}

    monkeypatch.setattr(claude_cli, "ask", fake_ask)
    result = main.get_visual_clips("v.mp4", 400)

    assert len(asked) == 2 and asked[0][0] == 0 and asked[-1][1] == 400
    starts = sorted(s["start"] for s in result["shorts"])
    assert starts == [10, asked[1][0] + 10]  # the second stretch's pick, in source seconds


def test_editor_sends_frames_not_an_upload(claude, monkeypatch):
    editor_mod = pytest.importorskip("editor")
    monkeypatch.setattr(frame_sampler, "jpegs_every",
                        lambda path, s0, s1, step, width=512: [(0, b"j"), (2, b"k")])
    monkeypatch.setattr(editor_mod.os.path, "exists", lambda p: True)
    seen = {}

    def fake_ask(contents, schema, model=None, timeout=600.0, task=None):
        seen.update(contents=contents, schema=schema, model=model, task=task)
        return {"edits": [{"type": "punch_in", "start": 1.0, "end": 3.0,
                           "strength": 0.1, "reason": "punchline"}]}, {}

    monkeypatch.setattr(claude_cli, "ask", fake_ask)
    ed = editor_mod.VideoEditor()
    frames = ed.sample_frames("clip.mp4")
    out = ed.get_ffmpeg_filter(frames, 10.0, width=1080, height=1920)

    assert seen["contents"][:4] == ["t=0s", b"j", "t=2s", b"k"]
    assert seen["schema"] is editor_mod.EditPlan and seen["task"] == "edits"
    assert out["edits"] and out["filter_string"]


def test_thumbnails_are_the_frame_with_each_concepts_text(claude, monkeypatch, tmp_path):
    thumbnail = pytest.importorskip("thumbnail")
    from PIL import Image
    monkeypatch.chdir(tmp_path)
    frame = tmp_path / "frame.jpg"
    Image.new("RGB", (1920, 1080), (30, 60, 90)).save(frame)
    concepts = [{"text": "STOP HOURLY", "text_position": "left", "text_color": "yellow", "scene": "s", "why": "a"},
                {"text": "", "text_position": "right", "text_color": "white", "scene": "s", "why": "b"}]
    monkeypatch.setattr(thumbnail, "plan_thumbnail_concepts", lambda *a, **k: concepts)

    out = thumbnail.generate_thumbnail("title", "sess", count=2,
                                       frame_reference={"path": str(frame)})

    assert [t["text"] for t in out] == ["STOP HOURLY", ""]
    for t in out:
        img = Image.open(tmp_path / "output" / "thumbnails" / "sess" / t["url"].rsplit("/", 1)[1])
        assert img.size == (1280, 720)
    with pytest.raises(RuntimeError, match="Pick a frame"):
        thumbnail.generate_thumbnail("title", "sess", count=1)
