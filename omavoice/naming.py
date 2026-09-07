"""File naming for recordings and their transcript sidecars."""

import re
from datetime import datetime
from pathlib import Path

# \w keeps unicode letters and digits, so an accented or Japanese title
# survives; separators, control characters and emoji do not.
_UNSAFE = re.compile(r"[^\w.-]+")
_DASHES = re.compile(r"-{2,}")
MAX_TITLE_BYTES = 80


def slugify(title: str) -> str:
    text = _UNSAFE.sub("-", (title or "").strip())
    text = _DASHES.sub("-", text).strip("-.")
    # Filesystems cap a name in bytes, not characters, and one character here
    # can be four bytes.
    trimmed = text.encode("utf-8")[:MAX_TITLE_BYTES].decode("utf-8", errors="ignore")
    return trimmed.strip("-.")


def basename(when: datetime, title: str = "") -> str:
    stamp = when.strftime("%Y-%m-%d_%H-%M-%S")
    slug = slugify(title)
    return f"{stamp}-{slug}" if slug else stamp


def rename_problem(new_stem: str):
    """Why this name cannot be used, or None when it is fine.

    Renaming is deliberately more permissive than the slug applied to a title:
    people should be able to name their own files. It still has to be a name
    the filesystem and the file list can live with.
    """
    if "/" in new_stem:
        return "A name cannot contain a slash."
    if new_stem.startswith("."):
        return "A name cannot start with a dot, or the recording becomes hidden."
    if any(ord(c) < 32 for c in new_stem):
        return "A name cannot contain control characters."
    if len(new_stem.encode("utf-8")) > 200:
        return "That name is too long."
    return None


def transcript_path_for(audio_path: Path) -> Path:
    return audio_path.with_suffix(".txt")


def unique_basename(directory: Path, base: str, ext: str) -> str:
    """Return `base` or `base-N` such that neither the audio nor the .txt exists."""
    candidate = base
    n = 2
    while (directory / f"{candidate}.{ext}").exists() or (directory / f"{candidate}.txt").exists():
        candidate = f"{base}-{n}"
        n += 1
    return candidate
