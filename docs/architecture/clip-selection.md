# Clip selection

## How many clips a job returns (`clip_selection.py`, `main.get_viral_clips`)

The count is not a setting, it is derived. `get_viral_clips` builds ~90 s scoring
windows over the transcript, scores them in batches, shortlists the best
`shortlist_target` (`min(10, duration//90 + 2)`, floor 3) and asks the detail pass
for `clip_count_targets(len(shortlist))` clips. The floor matters: jobs that
return only 1-3 clips retain far worse than jobs that return 4-9.

Two invariants keep that chain from leaking:

- **The scoring pass ranks, it does not select.** The prompt scores **every**
  window and the shortlist is the global top N. A prompt that says "choose up to
  3 windows from this batch" caps the shortlist at `3 * n_batches` instead of the
  target and starves every source under ~30 min. Batches are near-equal
  (`score_batches`): a trailing batch of one window would return that window
  whatever its score.
- **The clip floor is enforced in code, not just in the prompt.** When the detail
  pass comes back under `min_clips`, the shortlist windows that produced nothing
  get one more call for the difference. An empty answer is accepted: padding is
  worse than a short list.

`target_clips` on `/api/process` (dashboard: advanced options) pins both ends via
`CLIP_TARGET_MIN` / `CLIP_TARGET_MAX` when the user wants an exact number.

## Silent footage: the vision fallback (`main.get_visual_clips`)

`transcribe_video` raising `NoAudioError`, **or** `speech_is_sparse()` returning
true, both set `transcript = None` and route to `get_visual_clips`. Sparse means
under `MIN_SPEECH_WORDS` (8) in total or under `MIN_SPEECH_WORDS_PER_MIN` (5).
Music-only footage and mic-muted recordings transcribe to a handful of stray
words; without the second test the picker scores those words and cuts around
them.

The vision pass sends timestamped frames, Claude returns the same `{"shorts"}`
shape (`llm_prompts.VisualResponse`) in the same 15-60 s band, so every later
stage is identical. `CLIP_TARGET_MIN`/`MAX` apply directly (there are no scoring
windows). The transcript is stored as `{"language": "none", "segments": []}`, so
the clips have no subtitles, which is correct.

The source is split by `clip_selection.vision_segments` into near-equal
stretches of at most 300 s (100 frames, one every 3 s), each watched on its own,
offset back to source time, then merged with `dedupe_overlapping` +
`trim_to_best` to `CLIP_TARGET_MAX`. One failed stretch is logged and skipped.
`VISION_MAX_SEGMENTS` (8, 40 min) bounds one job; past it only the first
stretches are watched. Claude gets no audio: a gameplay capture's explosions
and music do not help pick moments.

## The model: Claude through the `claude` CLI (`llm_backend.py`, `claude_cli.py`)

Every model call runs `claude -p` on the signed-in Claude subscription (no API
key). `--json-schema` enforces the shape (`llm_prompts.py` holds the schemas
and prompts) and the answer is validated again with the pydantic schema. Each
call runs isolated (own system prompt, no tools/settings/MCP, scratch cwd so no
CLAUDE.md is read) to keep it near 1.5k input tokens. Text questions use
`LLM_MODEL` (default `sonnet`), frame questions `claude_cli.VISION_MODEL`
(`haiku`). `main._run_llm_stage` retries an overloaded or rate-limited call
(429/5xx/529) and an answer that missed a field.

- Transcript passes of `get_viral_clips`: text, 8 windows per scoring call.
- Frame stages (layout picker, screen check, burned-in subtitle band, hook
  grounding) send JPEG frames as image blocks through `llm_backend.ask_frames`.
- Silent footage: `get_visual_clips` sends one frame every `VISION_FRAME_STEP`
  (3 s), labelled with its time, at most `VISION_FRAMES_PER_CALL` (100) per
  call: stretches of 300 s through `vision_segments` (the first 40 min with the
  default `VISION_MAX_SEGMENTS`), and no audio.
- The effects editor (`editor.VideoEditor`) gets a frame every 2 s of the clip
  next to the transcript. Frames come from `frame_sampler.jpegs_every`.
- Thumbnail Studio's titles, concepts and description go through
  `thumbnail._ask_text`. Claude cannot draw, so `generate_thumbnail` burns each
  planned concept's text (side, colour) onto the uploaded photo, the picked
  frame, or else the session's first face frame.
