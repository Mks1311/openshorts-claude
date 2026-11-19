import os
import uuid
import json

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# Frames sent with the transcript instead of the whole video: ten 1024px
# frames cost a few thousand tokens however long the source runs.
TITLE_FRAMES = 10
TITLE_FRAME_WIDTH = 1024

# YouTube shows ~50 characters of a title on a phone before cutting it.
TITLE_MOBILE_CHARS = 50
TITLE_MAX_CHARS = 65

THUMB_W, THUMB_H = 1280, 720
THUMB_MAX_BYTES = 2 * 1024 * 1024  # YouTube's upload limit
THUMB_FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Anton-Regular.ttf")
TEXT_POSITIONS = ("left", "right", "top", "bottom")


def _parse_json(text):
    """Model JSON, tolerant of code fences and prose around the object."""
    text = (text or "").strip()
    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("no object", text, 0)
    # raw_decode stops at the end of the first complete object, so a model
    # that appends a stray "]}" (seen with flash) does not break the parse.
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    return obj


def _frame_parts(video_path, n=TITLE_FRAMES, width=TITLE_FRAME_WIDTH):
    """Evenly spaced frames as JPEG bytes (empty list on any failure)."""
    try:
        from layout_picker import sample_frames
        return list(sample_frames(video_path, n=n, width=width))
    except Exception as e:
        print(f"⚠️ [Thumbnail] Could not sample frames: {e}")
        return []


def _ask_text(contents):
    """Claude's answer as text, for text strings and JPEG frames (bytes)."""
    import claude_cli
    return claude_cli.ask(contents, None, task="studio")[0]


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

def analyze_video_for_titles(video_path, transcript=None):
    """
    Suggests YouTube titles from the transcript plus a handful of frames.
    Two calls: a wide brainstorm, then a critic that scores and picks a
    diverse top 10 and pairs each title with a 1-4 word thumbnail hook.
    Returns: { "titles", "thumbnail_texts", "transcript_summary", "language",
               "segments", "video_duration", "recommended" }
    """
    if transcript is None:
        from main import transcribe_video
        print("🎬 [Thumbnail] Transcribing video...")
        transcript = transcribe_video(video_path)
    else:
        print("🎬 [Thumbnail] Using pre-computed transcript (Whisper already done)...")

    frames = _frame_parts(video_path)
    language = transcript.get("language", "en")
    segments = transcript.get("segments", [])
    video_duration = segments[-1]["end"] if segments else 0
    # Enough transcript for a title; a 3-hour podcast does not need all of it.
    transcript_text = transcript.get("text", "")[:60000]

    brainstorm_prompt = f"""You are a YouTube packaging expert (titles + thumbnails) for a channel that wants maximum CTR without lying.

The images are frames sampled evenly across the video. The transcript is below.

TRANSCRIPT (language: {language}):
{transcript_text}

TASK 1: Summarize the video in 2-3 sentences (what it is, who it is for, the single most surprising or valuable point).

TASK 2: Brainstorm 25 candidate titles. Cover ALL of these styles, at least 3 each:
- specific outcome / result ("I did X and Y happened")
- curiosity gap (withhold the key fact, never clickbait the video does not pay off)
- contrarian / myth-busting
- how-to with a concrete promise
- number / list
- question the viewer already asks themselves
- story / confession

TITLE RULES:
- Write in the SAME LANGUAGE as the transcript ({language}). Never translate.
- Ideal length {TITLE_MOBILE_CHARS} characters or less; hard maximum {TITLE_MAX_CHARS}. Phones cut titles after ~{TITLE_MOBILE_CHARS} characters, so the payoff must land inside the first {TITLE_MOBILE_CHARS}.
- Put the main keyword / subject in the first 3 words.
- Be specific to THIS video: names, numbers, tools, places from the transcript. Nothing generic.
- No ALL CAPS words, at most one exclamation mark, no emojis.

OUTPUT JSON:
{{
  "transcript_summary": "...",
  "candidates": ["title", ...]
}}"""

    print("🤖 [Thumbnail] Brainstorming titles...")
    text = _ask_text(frames + [brainstorm_prompt])
    try:
        draft = _parse_json(text)
    except (json.JSONDecodeError, AttributeError):
        print(f"❌ [Thumbnail] Failed to parse brainstorm JSON: {text}")
        return {
            "titles": ["Could not generate titles - please try again"],
            "thumbnail_texts": [],
            "transcript_summary": transcript.get("text", "")[:500],
            "language": language,
            "segments": segments,
            "video_duration": video_duration,
            "recommended": [],
        }

    summary = draft.get("transcript_summary", "")
    candidates = [c for c in draft.get("candidates", []) if isinstance(c, str) and c.strip()]

    critic_prompt = f"""You are a ruthless YouTube title critic. Below are candidate titles for one video.

VIDEO SUMMARY: {summary}
LANGUAGE: {language}

CANDIDATES:
{json.dumps(candidates, ensure_ascii=False, indent=2)}

Score every candidate 1-10 on each of: specificity (concrete to this video), curiosity (makes you need to click), clarity (understood in 1 second on a phone), honesty (the video delivers it). Reject anything over {TITLE_MAX_CHARS} characters or whose hook lands after character {TITLE_MOBILE_CHARS}.

Then return the 10 best, ordered best first, with NO two titles using the same angle (different styles, different hooks). You may lightly edit a title to fix length or sharpen it, keeping the language ({language}).

For each of the 10, write the THUMBNAIL TEXT: 1-4 words, ALL CAPS, in {language}, that COMPLEMENTS the title instead of repeating it (the title says what, the thumbnail says the emotion, the number or the twist). Examples of the relationship: title "How I got 10k subscribers in 30 days" -> thumbnail "0 TO 10K"; title "Por qué dejé mi trabajo en Google" -> thumbnail "ME FUI".

Pick the top 2 and explain in one sentence each why they will win the click.

OUTPUT JSON:
{{
  "titles": ["...", ... 10 items],
  "thumbnail_texts": ["...", ... 10 items, same order],
  "recommended": [
    {{"index": 0, "reason": "..."}},
    {{"index": 3, "reason": "..."}}
  ]
}}"""

    print("🧐 [Thumbnail] Scoring titles...")
    text = _ask_text([critic_prompt])
    try:
        picked = _parse_json(text)
        titles = [t for t in picked.get("titles", []) if isinstance(t, str) and t.strip()]
        if not titles:
            raise ValueError("no titles")
    except (json.JSONDecodeError, AttributeError, ValueError):
        print(f"⚠️ [Thumbnail] Critic failed, falling back to the brainstorm: {(text or '')[:300]}")
        titles = candidates[:10]
        picked = {"thumbnail_texts": [], "recommended": []}

    thumbnail_texts = [str(t) for t in picked.get("thumbnail_texts", [])][:len(titles)]
    thumbnail_texts += [""] * (len(titles) - len(thumbnail_texts))
    return {
        "titles": titles,
        "thumbnail_texts": thumbnail_texts,
        "transcript_summary": summary,
        "language": language,
        "segments": segments,
        "video_duration": video_duration,
        "recommended": [r for r in picked.get("recommended", [])
                        if isinstance(r, dict) and isinstance(r.get("index"), int)
                        and 0 <= r["index"] < len(titles)],
    }


def refine_titles(context, user_message, conversation_history=None):
    """
    Takes video context + user feedback and returns refined title suggestions.
    """

    history_text = ""
    if conversation_history:
        for msg in conversation_history:
            role = msg.get("role", "user")
            history_text += f"\n{role.upper()}: {msg['content']}"

    prompt = f"""You are a YouTube title expert. Based on the video context and the user's feedback, suggest 8 new refined YouTube titles.

VIDEO CONTEXT:
{context}

CONVERSATION HISTORY:{history_text}

USER'S NEW REQUEST:
{user_message}

RULES:
- Ideal length {TITLE_MOBILE_CHARS} characters or less, hard maximum {TITLE_MAX_CHARS} (phones cut after ~{TITLE_MOBILE_CHARS})
- Main keyword / subject in the first 3 words
- Incorporate the user's feedback/direction; if they ask for a style, follow it
- Specific to this video (names, numbers, tools), never generic
- Same language as the original content unless the user asks for another language: then write ALL titles in the requested language

For each title also write its THUMBNAIL TEXT: 1-4 words, ALL CAPS, same language as the title, complementing it (emotion, number or twist), never repeating it.

OUTPUT JSON:
{{
    "titles": ["title1", "title2", ...],
    "thumbnail_texts": ["...", ... same order],
    "language": "ISO 639-1 code of the language the titles are written in"
}}"""

    text = _ask_text([prompt])
    try:
        result = _parse_json(text)
        titles = [t for t in result.get("titles", []) if isinstance(t, str) and t.strip()]
        texts = [str(t) for t in result.get("thumbnail_texts", [])][:len(titles)]
        texts += [""] * (len(titles) - len(texts))
        return {"titles": titles, "thumbnail_texts": texts,
                "language": str(result.get("language") or "")[:5]}
    except (json.JSONDecodeError, AttributeError):
        print(f"❌ [Thumbnail] Failed to parse refined titles: {text}")
        return {"titles": ["Could not refine titles - please try again"], "thumbnail_texts": [], "language": ""}


# ---------------------------------------------------------------------------
# Frames from the video
# ---------------------------------------------------------------------------

def extract_face_frames(video_path, session_id, n=5, samples=40):
    """
    Frames from the video worth building a thumbnail on: a large, sharp face,
    spread across the runtime so the user gets different moments, not five
    near-duplicates of one second. Saved as JPEGs under the session's
    thumbnails dir. Returns [{"url", "path", "time", "face": [x, y, w, h]}].
    """
    import cv2
    from main import detect_face_candidates

    out_dir = os.path.join("output", "thumbnails", session_id, "frames")
    os.makedirs(out_dir, exist_ok=True)

    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    scored = []
    try:
        if total <= 0:
            return []
        for i in range(samples):
            idx = int((i + 0.5) * total / samples)
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok:
                continue
            faces = detect_face_candidates(frame)
            if not faces:
                continue
            x, y, w, h = max(faces, key=lambda f: f["score"])["box"]
            fh, fw = frame.shape[:2]
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            scored.append(rank_frame(idx, idx / fps, frame, [x, y, w, h], (fw, fh),
                                     cv2.Laplacian(gray, cv2.CV_64F).var()))
    finally:
        cap.release()

    picked = pick_spread(scored, n, total)

    results = []
    for k, cand in enumerate(picked):
        frame = cand["frame"]
        fh, fw = frame.shape[:2]
        # Keep native resolution up to 1920 wide: the face crop sent as the
        # person reference comes from this file, and a 500px face gives the
        # image model too little to keep the likeness.
        scale = min(1.0, 1920 / float(fw))
        resized = cv2.resize(frame, (int(fw * scale), max(2, int(fh * scale))), interpolation=cv2.INTER_AREA) if scale < 1.0 else frame
        path = os.path.join(out_dir, f"frame_{k + 1}.jpg")
        cv2.imwrite(path, resized, [cv2.IMWRITE_JPEG_QUALITY, 90])
        results.append({
            "url": f"/thumbnails/{session_id}/frames/frame_{k + 1}.jpg",
            "path": path,
            "time": round(cand["time"], 1),
            "face": [int(v * scale) for v in cand["face"]],
        })
    print(f"🖼️ [Thumbnail] {len(results)} face frames picked from {len(scored)} candidates")
    return results


def rank_frame(idx, time, frame, face_box, frame_size, sharpness):
    """Score one candidate frame: face area, boosted by sharpness (capped so a
    noisy frame cannot outrank a bigger face). None below 1% face area."""
    x, y, w, h = face_box
    fw, fh = frame_size
    area = (w * h) / float(fw * fh)
    if area < 0.01:
        return None
    return {
        "idx": idx, "time": time, "frame": frame,
        "face": [int(x), int(y), int(w), int(h)],
        "score": area * (1.0 + min(sharpness, 500.0) / 500.0),
    }


def pick_spread(scored, n, total_frames, min_gap_fraction=0.04):
    """Best-first, but every pick at least `min_gap_fraction` of the runtime
    away from the others, returned in timeline order."""
    scored = sorted((s for s in scored if s), key=lambda s: -s["score"])
    picked = []
    min_gap = total_frames * min_gap_fraction
    for cand in scored:
        if all(abs(cand["idx"] - p["idx"]) >= min_gap for p in picked):
            picked.append(cand)
        if len(picked) >= n:
            break
    picked.sort(key=lambda s: s["idx"])
    return picked


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------

def plan_thumbnail_concepts(title, count, video_context="", extra_prompt="",
                            thumbnail_text_hint="", has_person=False, language="en"):
    """
    One text call that designs `count` DIFFERENT thumbnails before any pixel is
    drawn. Asking the image model to invent the concept and render it in one
    go gives N variations of one idea; splitting it gives N ideas.
    """
    person_line = ("A real photo of the presenter is provided and MUST be the focal point, "
                   "with a clear, natural expression that reads at small size (no caricature)."
                   if has_person else
                   "No photo of the presenter is available: build the thumbnail around an object, "
                   "a scene, a before/after, or a symbol; do NOT invent a specific person's face "
                   "unless the user asks for one.")
    hint_line = (f'\nSUGGESTED THUMBNAIL TEXT (use it for the first concept unless it is poor): "{thumbnail_text_hint}"'
                 if thumbnail_text_hint else "")
    extra_line = f"\nMANDATORY USER INSTRUCTIONS (override everything else):\n{extra_prompt}\n" if extra_prompt else ""
    prompt = f"""You are a YouTube thumbnail art director. Design {count} DIFFERENT thumbnail concepts for this video.

VIDEO TITLE: "{title}"
LANGUAGE OF THE AUDIENCE: {language}
VIDEO CONTEXT: {video_context or "(none)"}
{person_line}{hint_line}{extra_line}
The thumbnail must show WHAT the video is about, not only how the presenter feels: name the concrete subject (the product, the tool, the result, the numbers) and build the picture around it. A face alone with two words is the lazy default; use it at most once, and only combined with a strong prop or scene.

Each concept must use a different device, e.g.: the presenter + a prop that IS the topic (a phone showing vertical clips, a laptop with the app, a counter/badge with the number); before/after or "long video -> short clips" split; product/UI hero shot with the presenter small; a giant number or stat as the hero with the subject behind; "X vs Y" comparison; a red circle/arrow on a detail; a dramatic reveal with depth (foreground object, blurred background). Layered, cinematic, premium: real props, depth of field, rim light, particles, glow on the key object. Never flat, never a plain wall behind the person. No two concepts may share the same text or the same layout.

Per concept give:
- "text": 1-4 words, ALL CAPS, in {language}. Complements the title (the title says what, the text says the emotion, the number or the twist). Never repeat the title. No emoji.
- "text_position": one of "left", "right", "top", "bottom" (where the text block sits; the subject goes on the opposite side).
- "text_color": "white" or "yellow" (yellow for money/energy, white otherwise).
- "scene": a precise image-generation prompt in English, 2-4 sentences: subject, expression/pose, background, lighting, colours, camera. Say that the text_position side is clean negative space (simple, dark or plain) so a headline can sit there. It must NOT ask for any text, letters, captions or logos in the image.
- The image model REFUSES prompts that name or depict real people, companies or brands. Never write a real name (person, company, product, logo) in "scene". Refer to the presenter only as "the person in the provided photo" when a photo is provided, otherwise as a generic, unnamed person or use objects and symbols.
- "why": one short sentence on why this earns the click.

OUTPUT JSON:
{{"concepts": [{{"text": "...", "text_position": "left", "text_color": "yellow", "scene": "...", "why": "..."}}, ...]}}"""

    text = _ask_text([prompt])
    try:
        concepts = _parse_json(text).get("concepts", [])
    except (json.JSONDecodeError, AttributeError):
        print(f"⚠️ [Thumbnail] Concept JSON unreadable, using a generic concept: {(text or '')[:200]}")
        concepts = []
    return normalise_concepts(concepts, count, title, thumbnail_text_hint)


def normalise_concepts(concepts, count, title, thumbnail_text_hint=""):
    """Clamp model output to what the renderer accepts and pad to `count`."""
    cleaned = []
    for c in concepts:
        if not isinstance(c, dict) or not c.get("scene"):
            continue
        cleaned.append({
            "text": str(c.get("text") or thumbnail_text_hint or "").strip().upper()[:40],
            "text_position": c.get("text_position") if c.get("text_position") in TEXT_POSITIONS else "left",
            "text_color": "yellow" if c.get("text_color") == "yellow" else "white",
            "scene": str(c["scene"]),
            "why": str(c.get("why", "")),
        })
    while len(cleaned) < count:
        cleaned.append({
            "text": thumbnail_text_hint.upper()[:40],
            "text_position": "left",
            "text_color": "white",
            "scene": (f"A bold, high-contrast YouTube thumbnail scene for a video titled '{title}'. "
                      "Dramatic lighting, vibrant colours, the left third is clean dark negative space."),
            "why": "fallback",
        })
    return cleaned[:count]


def _wrap_lines(draw, text, font, max_width):
    words = text.split()
    lines, cur = [], []
    for w in words:
        test = " ".join(cur + [w])
        if draw.textlength(test, font=font) <= max_width or not cur:
            cur.append(w)
        else:
            lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    return lines


def burn_thumbnail_text(img, text, position="left", color="white"):
    """
    Draw the hook text with PIL: Anton, heavy black stroke, drop shadow,
    filling the chosen side of the canvas. Image models misspell accents and
    split words; this never does.
    """
    text = (text or "").strip()
    if not text:
        return img
    img = img.convert("RGBA")
    W, H = img.size
    horizontal = position in ("left", "right")
    box_w = int(W * (0.46 if horizontal else 0.92))
    box_h = int(H * (0.84 if horizontal else 0.40))
    fill = (255, 221, 0, 255) if color == "yellow" else (255, 255, 255, 255)

    draw = ImageDraw.Draw(img)
    size = int(H * 0.30)
    while True:
        font = ImageFont.truetype(THUMB_FONT, size)
        lines = _wrap_lines(draw, text, font, box_w)
        # Anton's accents sit above the cap height: 1.15 keeps a tilde on
        # line 2 off the baseline of line 1.
        line_h = int(size * 1.15)
        widest = max(draw.textlength(l, font=font) for l in lines)
        if (len(lines) <= 3 and widest <= box_w and line_h * len(lines) <= box_h) or size <= 24:
            break
        size = int(size * 0.92)
    stroke = max(4, size // 12)
    total_h = line_h * len(lines)
    # PIL draws from the ascender line; shift so the block is measured from the
    # tallest glyph actually present (caps sit lower than accents).
    top_pad = min(font.getbbox(l)[1] for l in lines)

    if position == "left":
        x0, y0 = int(W * 0.05), (H - total_h) // 2 - top_pad
    elif position == "right":
        x0, y0 = W - box_w - int(W * 0.05), (H - total_h) // 2 - top_pad
    elif position == "top":
        x0, y0 = int(W * 0.04), int(H * 0.05) - top_pad
    else:
        x0, y0 = int(W * 0.04), H - total_h - int(H * 0.06) - top_pad

    def line_x(line):
        return x0 if horizontal else (W - draw.textlength(line, font=font)) // 2

    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    for i, line in enumerate(lines):
        sdraw.text((line_x(line) + stroke, y0 + i * line_h + stroke * 1.5), line, font=font,
                   fill=(0, 0, 0, 170), stroke_width=stroke, stroke_fill=(0, 0, 0, 170))
    shadow = shadow.filter(ImageFilter.GaussianBlur(stroke))
    img = Image.alpha_composite(img, shadow)
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((line_x(line), y0 + i * line_h), line, font=font, fill=fill,
                  stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    return img


# AI Act art. 50(2): a synthetic image has to be marked in a machine-readable
# way, and these thumbnails are exactly the case the article is about — an
# image model painting a scene, often around a reference photo of the real
# presenter. Videos get the mp4 `comment` tag (ffmpeg_utils.mark_ai_generated);
# a JPEG's equivalent is XMP, and the interoperable vocabulary is IPTC's
# DigitalSourceType, whose `trainedAlgorithmicMedia` value is what Google,
# LinkedIn, TikTok and Adobe already read. Written alongside the EXIF Software
# tag because the two survive different pipelines: a re-encode usually keeps
# EXIF and drops XMP, a metadata scrub keeps neither, and this is the honest
# ceiling of what a JPEG can carry without a C2PA signing key.
AI_XMP_PACKET = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about=""'
    ' xmlns:Iptc4xmpExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/"'
    ' xmlns:xmp="http://ns.adobe.com/xap/1.0/"'
    ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
    ' Iptc4xmpExt:DigitalSourceType='
    '"http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"'
    ' xmp:CreatorTool="Shorts Maxxing">'
    '<dc:description><rdf:Alt><rdf:li xml:lang="x-default">'
    'AI-generated image produced with Shorts Maxxing'
    '</rdf:li></rdf:Alt></dc:description>'
    '</rdf:Description></rdf:RDF></x:xmpmeta>'
    '<?xpacket end="w"?>'
).encode("utf-8")

AI_SOFTWARE_TAG = "Shorts Maxxing — AI-generated image"


def _ai_save_kwargs(img):
    """XMP + EXIF marking for a generated image. Never raises.

    Pillow grew the ``xmp=`` save argument for JPEG relatively late, so an
    older Pillow simply gets the EXIF half rather than failing the render.
    """
    kwargs = {}
    try:
        exif = img.getexif()
        exif[0x0131] = AI_SOFTWARE_TAG                    # Software
        exif[0x010E] = AI_SOFTWARE_TAG                    # ImageDescription
        kwargs["exif"] = exif.tobytes()
    except Exception:
        pass
    kwargs["xmp"] = AI_XMP_PACKET
    return kwargs


def _cover(img):
    """Cover-crop to THUMB_W x THUMB_H (RGB)."""
    img = img.convert("RGB")
    scale = max(THUMB_W / img.width, THUMB_H / img.height)
    img = img.resize((max(THUMB_W, int(img.width * scale + 0.5)),
                      max(THUMB_H, int(img.height * scale + 0.5))), Image.LANCZOS)
    left = (img.width - THUMB_W) // 2
    top = (img.height - THUMB_H) // 2
    return img.crop((left, top, left + THUMB_W, top + THUMB_H))


def finalize_thumbnail(img, out_path):
    """Cover-crop to 1280x720 and save a JPEG under YouTube's 2 MB limit.

    Every file that leaves here is machine-marked as AI-generated — see
    AI_XMP_PACKET.
    """
    img = _cover(img)
    marks = _ai_save_kwargs(img)
    for q in (92, 88, 84, 78, 70, 60):
        try:
            img.save(out_path, "JPEG", quality=q, optimize=True, **marks)
        except (TypeError, ValueError):
            # An older Pillow without the xmp= argument: keep the EXIF mark
            # rather than losing the thumbnail.
            marks.pop("xmp", None)
            img.save(out_path, "JPEG", quality=q, optimize=True, **marks)
        if os.path.getsize(out_path) <= THUMB_MAX_BYTES:
            break
    return out_path


def generate_thumbnail(title, session_id, face_image_path=None, bg_image_path=None,
                       extra_prompt="", count=3, video_context="", burn_text=True,
                       thumbnail_text_hint="", language="en", frame_reference=None):
    """
    Claude cannot draw: it plans `count` concepts and each one's text, side
    and colour is burned on the uploaded photo, else the picked frame
    (frame_reference: {"path", "face"} from extract_face_frames), else the
    background image. Returns [{"url", "text", "why", "fallback"}].
    """
    output_dir = os.path.join("output", "thumbnails", session_id)
    os.makedirs(output_dir, exist_ok=True)
    base_path = (face_image_path if face_image_path and os.path.exists(face_image_path)
                 else (frame_reference or {}).get("path") or bg_image_path)
    if not base_path or not os.path.exists(base_path):
        raise RuntimeError("Pick a frame or upload a photo: the thumbnail is built on it.")
    concepts = plan_thumbnail_concepts(
        title, count, video_context=video_context, extra_prompt=extra_prompt,
        thumbnail_text_hint=thumbnail_text_hint, has_person=True, language=language)
    base = _cover(Image.open(base_path))
    batch = uuid.uuid4().hex[:6]  # a regenerate never serves a stale cached file
    thumbnails = []
    for i, c in enumerate(concepts):
        out_path = os.path.join(output_dir, f"thumb_{batch}_{i + 1}.jpg")
        img = burn_thumbnail_text(base, c["text"], c["text_position"], c["text_color"]) if burn_text else base
        finalize_thumbnail(img, out_path)
        print(f"✅ [Thumbnail] Saved: {out_path} (\"{c['text']}\", {c['text_position']})")
        thumbnails.append({"url": f"/thumbnails/{session_id}/{os.path.basename(out_path)}",
                           "text": c["text"], "why": c["why"], "fallback": False})
    return thumbnails


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------

def generate_youtube_description(title, transcript_segments, language, video_duration,
                                 max_chars=200000):
    """
    Uses Claude to generate a YouTube description with chapter markers from transcript segments.
    Returns: { "description": "full description text with chapters" }
    """

    # Format segments for the prompt
    formatted_segments = []
    for seg in transcript_segments:
        start = seg.get("start", 0)
        mins = int(start // 60)
        secs = int(start % 60)
        timestamp = f"{mins}:{secs:02d}"
        formatted_segments.append(f"[{timestamp}] {seg.get('text', '').strip()}")

    # Bounded: the source length is capped upstream, this caps the prompt.
    segments_text = "\n".join(formatted_segments)[:max_chars]

    # Format total duration
    dur_mins = int(video_duration // 60)
    dur_secs = int(video_duration % 60)
    duration_str = f"{dur_mins}:{dur_secs:02d}"

    prompt = f"""You are a YouTube SEO expert. Generate a complete YouTube video description for the following video.

VIDEO TITLE: "{title}"
VIDEO LANGUAGE: {language}
VIDEO DURATION: {duration_str}

TRANSCRIPT WITH TIMESTAMPS:
{segments_text}

REQUIREMENTS:
1. Write the description in the SAME LANGUAGE as the video ({language})
2. Start with a compelling 2-3 sentence summary/hook
3. Add relevant CTAs (subscribe, like, comment)
4. Generate YouTube CHAPTERS based on the transcript timestamps:
   - First chapter MUST start at 0:00
   - Minimum 3 chapters, each at least 10 seconds apart
   - Chapter titles should be concise and descriptive
   - Format: 0:00 Chapter Title
   - Place chapters in their own section with a blank line before and after
5. Add 5-10 relevant hashtags at the end
6. Keep the total description under 5000 characters

OUTPUT: Return ONLY the description text (no JSON wrapper, no markdown code blocks). The description should be ready to paste directly into YouTube."""

    print("🤖 [Thumbnail] Generating YouTube description with chapters...")
    description = _ask_text([prompt]).strip()
    # Clean up any accidental markdown wrappers
    if description.startswith("```"):
        lines = description.split("\n")
        description = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    return {"description": description}
