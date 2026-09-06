"""Pure helpers for 16-bit mono PCM buffers. No GTK, no subprocesses."""

from array import array

SAMPLE_RATE = 48000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE


def _samples(block: bytes) -> array:
    usable = len(block) - (len(block) % BYTES_PER_SAMPLE)
    a = array("h")
    a.frombytes(block[:usable])
    return a


def peak(block: bytes) -> float:
    """Peak absolute amplitude in 0.0 .. 1.0."""
    s = _samples(block)
    if not s:
        return 0.0
    return max(max(s), -min(s)) / 32768.0


def rms(block: bytes) -> float:
    s = _samples(block)
    if not s:
        return 0.0
    total = 0
    for v in s:
        total += v * v
    return (total / len(s)) ** 0.5 / 32768.0


def is_silent(block: bytes, threshold: float = 0.01) -> bool:
    return peak(block) < threshold


def has_speech(block: bytes, window_ms: int = 100, threshold: float = 0.02, min_fraction: float = 0.08) -> bool:
    """True when at least `min_fraction` of the windows carry signal above `threshold`.

    A single click or a breath should not trigger a transcription of an
    otherwise empty chunk; whisper invents words on near-silence.
    """
    window = seconds_to_bytes(window_ms / 1000.0)
    if window <= 0 or len(block) < window:
        return not is_silent(block, threshold)
    loud = 0
    count = 0
    offset = 0
    while offset + window <= len(block):
        count += 1
        if peak(block[offset:offset + window]) >= threshold:
            loud += 1
        offset += window
    return count > 0 and loud / count >= min_fraction


def seconds_to_bytes(seconds: float) -> int:
    n = int(seconds * BYTES_PER_SECOND)
    return n - (n % BYTES_PER_SAMPLE)


def bytes_to_seconds(n: int) -> float:
    return n / BYTES_PER_SECOND


def find_cut_point(pcm: bytes, search_seconds: float = 1.5, window_ms: int = 60) -> int:
    """Byte offset of the quietest short window inside the tail of `pcm`.

    Live captions are produced from fixed-length chunks. Cutting at an
    arbitrary sample splits words; cutting at the quietest point in the last
    `search_seconds` usually lands in a pause between words.
    """
    total = len(pcm) - (len(pcm) % BYTES_PER_SAMPLE)
    if total <= 0:
        return 0
    window = seconds_to_bytes(window_ms / 1000.0)
    span = min(total, seconds_to_bytes(search_seconds))
    if window <= 0 or span <= window:
        return total
    start = total - span
    best_offset = total
    best_energy = None
    offset = start
    while offset + window <= total:
        energy = rms(pcm[offset:offset + window])
        if best_energy is None or energy < best_energy:
            best_energy = energy
            best_offset = offset + window // 2
        offset += window
    best_offset -= best_offset % BYTES_PER_SAMPLE
    return best_offset


def format_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
