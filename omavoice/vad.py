"""Voice activity detection gate for live caption chunks.

whisper invents fluent, confident text when it is handed audio that contains
no speech, and a live chunk that lands in a pause is exactly that. Amplitude
is not enough to tell the difference: room tone, a fan, music and keyboard
noise all clear a loudness threshold while containing no voice. The only
reliable defence is to ask a voice activity detector first and never send the
chunk to whisper at all.

This wraps `whisper-vad-speech-segments`, which ships with whisper-cpp and
runs the Silero model in roughly 200 ms for a seven second chunk.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

BINARY = "whisper-vad-speech-segments"
THRESHOLD = "0.5"
MIN_SILENCE_MS = "100"
# Note: the binary advertises -vspd in its help text but rejects it, and exits
# zero when it does, so an unsupported flag would look like "no speech found".
# Minimum speech length is enforced here through `min_seconds` instead.
TIMEOUT_SECONDS = 20.0
CENTISECONDS = 100.0

_COUNT = re.compile(r"Detected\s+(\d+)\s+speech segments?", re.IGNORECASE)
_SPAN = re.compile(r"start\s*=\s*(-?[0-9.]+)\s*,\s*end\s*=\s*(-?[0-9.]+)")


def available() -> bool:
    return shutil.which(BINARY) is not None


def analyze(output: str):
    """Speech spans as (start, end) seconds, or None when the output made no sense.

    None and [] mean different things. [] is the detector saying there is no
    speech here. None is the detector failing to answer, which must never be
    read as silence: the binary exits zero even when it rejects an argument.
    """
    match = _COUNT.search(output or "")
    if match is None:
        return None
    if int(match.group(1)) == 0:
        return []
    spans = []
    for start, end in _SPAN.findall(output):
        a, b = float(start) / CENTISECONDS, float(end) / CENTISECONDS
        if b > a:
            spans.append((a, b))
    return spans


def speech_seconds(spans) -> float:
    """Total speech in a span list from analyze(). None (no answer) counts as zero."""
    return sum(end - start for start, end in spans or ())


class SpeechGate:
    """Decides whether a wav chunk carries enough speech to be worth transcribing."""

    def __init__(self, model_path=None, threads: int = 2, min_seconds: float = 0.2):
        self.model_path = Path(model_path) if model_path else None
        self.threads = max(1, threads)
        self.min_seconds = min_seconds
        self.last_error = None
        self.checked = 0
        self.rejected = 0

    @property
    def armed(self) -> bool:
        return (self.model_path is not None and self.model_path.is_file() and available())

    def accepts(self, wav_bytes: bytes) -> bool:
        """True when the chunk should go to whisper.

        Fails open. If the detector cannot run we pass the chunk through and
        rely on the whisper server's own VAD, which stays enabled as a second
        line of defence, rather than silently dropping something the user said.
        """
        if not self.armed:
            return True
        self.checked += 1
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(prefix="omavoice-vad-", suffix=".wav", delete=False) as fh:
                tmp_name = fh.name      # recorded first: a failed write still needs unlinking
                fh.write(wav_bytes)
            proc = subprocess.run(
                [BINARY, "-vm", str(self.model_path), "-t", str(self.threads), "-np",
                 "-vt", THRESHOLD, "-vsd", MIN_SILENCE_MS, "-f", tmp_name],
                capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_error = str(exc)
            return True
        finally:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        if proc.returncode != 0:
            self.last_error = (proc.stderr or "").strip()[-200:]
            return True
        spans = analyze(proc.stdout)
        if spans is None:
            self.last_error = "could not read detector output"
            return True
        if speech_seconds(spans) >= self.min_seconds:
            return True
        self.rejected += 1
        return False
