"""PipeWire input enumeration through pactl."""

import json
import subprocess
from dataclasses import dataclass

APP_PREFIX = "app:"


@dataclass(frozen=True)
class Source:
    name: str
    description: str
    is_monitor: bool
    index: int = 0
    state: str = ""
    kind: str = ""          # "mic", "monitor" or "app"; derived when empty

    @property
    def category(self) -> str:
        if self.kind:
            return self.kind
        return "monitor" if self.is_monitor else "mic"

    @property
    def is_app(self) -> bool:
        return self.category == "app"

    @property
    def label(self) -> str:
        if self.is_app:
            return f"App: {self.description}"
        if self.is_monitor:
            return f"System audio: {self.description.removeprefix('Monitor of ').strip()}"
        return self.description


def parse_sources(payload) -> list:
    """Turn `pactl -f json list sources` output into Source objects.

    Microphones come first, ordered by description; monitors follow.
    """
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    result = []
    for entry in payload or []:
        name = entry.get("name") or ""
        if not name:
            continue
        props = entry.get("properties") or {}
        is_monitor = props.get("device.class") == "monitor" or name.endswith(".monitor")
        result.append(Source(
            name=name,
            description=(entry.get("description") or name).strip(),
            is_monitor=is_monitor,
            index=int(entry.get("index") or 0),
            state=entry.get("state") or "",
        ))
    result.sort(key=lambda s: (s.is_monitor, s.description.lower()))
    return result


def list_sources() -> list:
    try:
        out = subprocess.run(
            ["pactl", "-f", "json", "list", "sources"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    try:
        return parse_sources(out)
    except ValueError:
        return []


def parse_app_streams(payload) -> list:
    """Turn `pw-dump` output into one Source per application playback stream.

    Each playing app owns a PipeWire node with media.class Stream/Output/Audio.
    Its object.serial is stable for the life of the stream and is what
    pw-record is pointed at, with stream.capture.sink so only that app's
    audio is captured.
    """
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    result = []
    for node in payload or []:
        if node.get("type") != "PipeWire:Interface:Node":
            continue
        info = node.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        serial = props.get("object.serial")
        if serial is None:
            continue
        app = (props.get("application.name") or props.get("node.name") or "Unknown app").strip()
        media = (props.get("media.name") or "").strip()
        if media and media.lower() not in (app.lower(), "playback", "audio stream", "audio playback"):
            description = f"{app} \u2014 {media[:60]}"
        else:
            description = app
        result.append(Source(
            name=f"{APP_PREFIX}{serial}",
            description=description,
            is_monitor=False,
            index=int(node.get("id") or 0),
            state=str(info.get("state") or ""),
            kind="app",
        ))
    result.sort(key=lambda s: s.description.lower())
    return result


def list_app_streams() -> list:
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    try:
        return parse_app_streams(out)
    except ValueError:
        return []


def list_all() -> list:
    """Microphones, then playing apps, then system-audio monitors."""
    devices = list_sources()
    mics = [s for s in devices if not s.is_monitor]
    monitors = [s for s in devices if s.is_monitor]
    return mics + list_app_streams() + monitors


def default_source_name() -> str:
    try:
        return subprocess.run(
            ["pactl", "get-default-source"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
