"""What the ``/videos`` and ``/thumbnails`` mounts may serve.

The working directories also hold resume manifests, transcripts, metadata JSON
and checkpoints; only deliverables leave.
"""
from __future__ import annotations

import posixpath


# Nothing outside this list is a deliverable: the working directories also hold
# transcripts, metadata JSON, resume manifests and ownership sidecars, and an
# allowlist is the only version of this rule that stays correct when the
# pipeline starts writing a new kind of scratch file.
SERVABLE_EXTENSIONS = frozenset({
    ".mp4", ".webm", ".mov", ".m4v", ".mkv", ".ogv",
    ".mp3", ".m4a", ".wav", ".aac", ".flac", ".opus",
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif",
    ".srt", ".vtt", ".ass", ".zip",
})


class MediaPathError(ValueError):
    """The requested path is not something these prefixes may ever serve."""


def normalise_relpath(rel_path: str) -> str:
    """Return ``rel_path`` as a clean forward-slash relative path.

    Raises ``MediaPathError`` for absolute paths, traversal, and any segment
    starting with a dot — which is what keeps ``.resume.json`` (webhook secret),
    ``.owner`` (user uuid), ``.instance`` and ``.transcript_checkpoint.json``
    unreachable no matter who is asking.
    """
    if not rel_path or not isinstance(rel_path, str):
        raise MediaPathError("Empty path")
    cleaned = rel_path.replace("\\", "/").strip("/")
    if not cleaned:
        raise MediaPathError("Empty path")
    if "\x00" in cleaned:
        raise MediaPathError("Invalid path")
    segments = [s for s in cleaned.split("/") if s != ""]
    if not segments:
        raise MediaPathError("Empty path")
    for segment in segments:
        if segment.startswith("."):
            # Covers "." and ".." as well as every dotfile the pipeline writes.
            raise MediaPathError(f"Hidden or relative segment: {segment!r}")
    return posixpath.join(*segments)


def is_servable(rel_path: str) -> bool:
    """True if this relative path is both visible and of a deliverable type."""
    try:
        cleaned = normalise_relpath(rel_path)
    except MediaPathError:
        return False
    return posixpath.splitext(cleaned)[1].lower() in SERVABLE_EXTENSIONS
