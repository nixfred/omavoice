"""User settings, persisted as JSON under $XDG_CONFIG_HOME/omavoice."""

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "omavoice"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "omavoice"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "omavoice"


CONFIG_PATH = config_dir() / "config.json"


@dataclass
class Settings:
    source: str = "default"            # PipeWire node name, or "default"
    format: str = "opus"               # key into formats.FORMATS
    live_transcript: bool = True
    recordings_dir: str = field(default_factory=lambda: os.path.join(os.path.expanduser("~"), "Recordings"))
    live_model: str = "auto"           # path to a ggml model, or "auto"
    final_model: str = "same"          # path, "same" (as live) or "auto"
    language: str = "en"               # whisper language code, or "auto"
    chunk_seconds: float = 7.0         # live caption chunk length
    threads: int = 0                   # 0 = pick automatically
    timestamps: bool = False           # write [hh:mm:ss] per segment in the transcript
    pause_media: bool = True           # pause MPRIS players while recording
    window_width: int = 760
    window_height: int = 820

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Settings":
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        defaults = cls()
        clean = {}
        for f in fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            expected = type(getattr(defaults, f.name))
            if expected is float and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if expected is bool and not isinstance(value, bool):
                continue
            if expected is int and (isinstance(value, bool) or not isinstance(value, int)):
                continue
            if not isinstance(value, expected):
                continue          # wrong type: keep the default rather than crash later
            clean[f.name] = value
        return cls(**clean)

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
        os.replace(tmp, path)

    def recordings_path(self) -> Path:
        return Path(os.path.expanduser(self.recordings_dir))

    def effective_threads(self) -> int:
        if self.threads > 0:
            return self.threads
        return max(2, min(8, (os.cpu_count() or 4)))
