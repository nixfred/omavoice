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
