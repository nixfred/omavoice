"""ffmpeg encoding of the raw PCM master into the chosen container."""

import subprocess
from pathlib import Path

from omavoice import pcm
from omavoice.formats import AudioFormat


def encode(raw_path: Path, out_path: Path, fmt: AudioFormat) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(pcm.SAMPLE_RATE),
           "-ac", str(pcm.CHANNELS), "-i", str(raw_path), *fmt.ffmpeg_args, str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-400:] or f"ffmpeg exited with {proc.returncode}")
