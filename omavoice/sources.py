"""PipeWire input enumeration through pactl."""

import json
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    name: str
    description: str
    is_monitor: bool
    index: int = 0
    state: str = ""

    @property
    def label(self) -> str:
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


def default_source_name() -> str:
    try:
        return subprocess.run(
            ["pactl", "get-default-source"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
