"""Listing of past recordings in the recordings directory."""

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from omavoice.formats import EXTENSIONS
from omavoice.naming import transcript_path_for


@dataclass
class Recording:
    path: Path
    mtime: float
    size: int
    transcript: Path | None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def when(self) -> datetime:
        return datetime.fromtimestamp(self.mtime)

    @property
    def size_label(self) -> str:
        size = self.size
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"


def scan(directory: Path, limit: int = 30) -> list:
    items = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return items
    for entry in entries:
        if not entry.is_file():
            continue
        ext = entry.name.rsplit(".", 1)[-1].lower() if "." in entry.name else ""
        if ext not in EXTENSIONS:
            continue
        path = Path(entry.path)
        try:
            st = entry.stat()
        except OSError:
            continue
        transcript = transcript_path_for(path)
        items.append(Recording(path=path, mtime=st.st_mtime, size=st.st_size,
                               transcript=transcript if transcript.exists() else None))
    items.sort(key=lambda r: r.mtime, reverse=True)
    return items[:limit]
