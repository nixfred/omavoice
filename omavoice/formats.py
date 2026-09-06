"""The five output formats Omavoice can write, with their ffmpeg settings."""

from dataclasses import dataclass


@dataclass(frozen=True)
class AudioFormat:
    key: str
    label: str
    ext: str
    ffmpeg_args: tuple


FORMATS = (
    AudioFormat("opus", "Opus (small, ideal for voice)", "opus",
                ("-c:a", "libopus", "-b:a", "48k", "-vbr", "on", "-application", "audio")),
    AudioFormat("mp3", "MP3 (plays everywhere)", "mp3",
                ("-c:a", "libmp3lame", "-q:a", "3")),
    AudioFormat("m4a", "M4A / AAC", "m4a",
                ("-c:a", "aac", "-b:a", "96k")),
    AudioFormat("flac", "FLAC (lossless)", "flac",
                ("-c:a", "flac")),
    AudioFormat("wav", "WAV (uncompressed)", "wav",
                ("-c:a", "pcm_s16le")),
)

DEFAULT_FORMAT = FORMATS[0]
EXTENSIONS = tuple(f.ext for f in FORMATS)


def by_key(key: str) -> AudioFormat:
    for fmt in FORMATS:
        if fmt.key == key:
            return fmt
    return DEFAULT_FORMAT


def index_of(key: str) -> int:
    for i, fmt in enumerate(FORMATS):
        if fmt.key == key:
            return i
    return 0
