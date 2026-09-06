"""whisper.cpp integration.

Two paths:
  * `WhisperServer` keeps a model loaded in `whisper-server` for low-latency
    live captions (about 2-3 s per chunk on a laptop CPU).
  * `transcribe_file` runs `whisper-cli` over a finished recording for the
    accurate final transcript, so the server stays free for the next take.
"""

import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

from omavoice import pcm
from omavoice.config import cache_dir, data_dir

MODEL_DIRS = (
    data_dir() / "models",
    Path(os.path.expanduser("~/.local/share/voxtype/models")),
    Path("/usr/share/whisper.cpp/models"),
)
PREFERRED_ORDER = ("base.en", "base", "small.en", "small", "tiny.en", "tiny", "medium.en", "medium",
                   "large-v3-turbo", "large-v3")
MODEL_DOWNLOAD_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
VAD_MODEL_NAME = "silero-v5.1.2"
VAD_DOWNLOAD_URL = "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin"
DOWNLOADABLE = (
    ("tiny.en", "75 MB", MODEL_DOWNLOAD_BASE + "ggml-tiny.en.bin"),
    ("base.en", "142 MB", MODEL_DOWNLOAD_BASE + "ggml-base.en.bin"),
    ("small.en", "466 MB", MODEL_DOWNLOAD_BASE + "ggml-small.en.bin"),
    ("medium.en", "1.5 GB", MODEL_DOWNLOAD_BASE + "ggml-medium.en.bin"),
    ("large-v3-turbo", "1.6 GB", MODEL_DOWNLOAD_BASE + "ggml-large-v3-turbo.bin"),
    (VAD_MODEL_NAME, "1 MB, voice activity detection", VAD_DOWNLOAD_URL),
)
HALLUCINATIONS = {
    "you", "you.", "thank you.", "thank you", "thanks for watching.", "thanks for watching",
    "bye.", "the end.", "so", "um", "uh",
}


@dataclass(frozen=True)
class Model:
    path: Path

    @property
    def name(self) -> str:
        stem = self.path.stem
        return stem[len("ggml-"):] if stem.startswith("ggml-") else stem

    @property
    def english_only(self) -> bool:
        return self.name.endswith(".en")

    @property
    def label(self) -> str:
        try:
            mb = self.path.stat().st_size / (1024 * 1024)
            size = f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"
        except OSError:
            size = "?"
        return f"{self.name}  ({size}, {self.path.parent})"


def find_models() -> list:
    seen = {}
    for directory in MODEL_DIRS:
        try:
            entries = sorted(directory.glob("ggml-*.bin"))
        except OSError:
            continue
        for path in entries:
            name = path.stem[len("ggml-"):]
            if name.startswith("silero") or name.endswith("-encoder"):
                continue
            if "-q" in name and name.split("-q")[-1][:1].isdigit():
                continue
            seen.setdefault(name, Model(path))

    def rank(model: Model):
        try:
            return PREFERRED_ORDER.index(model.name)
        except ValueError:
            return len(PREFERRED_ORDER)

    return sorted(seen.values(), key=lambda m: (rank(m), m.name))


def find_vad_model() -> Path | None:
    for directory in MODEL_DIRS:
        try:
            hits = sorted(directory.glob("ggml-silero-*.bin"))
        except OSError:
            continue
        if hits:
            return hits[0]
    return None


def ensure_vad_model() -> Path | None:
    """Return the VAD model, fetching it once (about 1 MB) when missing."""
    found = find_vad_model()
    if found is not None:
        return found
    try:
        return download_model(VAD_MODEL_NAME, timeout=30)
    except RuntimeError:
        return None


def vad_args(model_path: Path | None) -> list:
    if model_path is None:
        return []
    return ["--vad", "-vm", str(model_path), "-vt", "0.5", "-vsd", "300", "-vp", "60"]


def resolve_model(setting: str, fallback: Model | None = None) -> Model | None:
    if setting and setting not in ("auto", "same"):
        path = Path(os.path.expanduser(setting))
        if path.is_file():
            return Model(path)
    if setting == "same" and fallback is not None:
        return fallback
    models = find_models()
    return models[0] if models else None


def have_binaries() -> tuple:
    return shutil.which("whisper-server") is not None, shutil.which("whisper-cli") is not None


def pcm_to_wav16k(pcm48: bytes) -> bytes:
    """Resample 48 kHz mono s16 PCM to a 16 kHz WAV blob via ffmpeg."""
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "s16le", "-ar", str(pcm.SAMPLE_RATE), "-ac", "1",
         "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
        input=pcm48, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip() or "ffmpeg failed")
    return proc.stdout


def raw_to_wav16k_file(raw_path: Path, wav_path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(pcm.SAMPLE_RATE), "-ac", "1",
         "-i", str(raw_path), "-ar", "16000", "-ac", "1", str(wav_path)],
        check=True, capture_output=True,
    )


def _silent_wav(seconds: float = 1.0) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _multipart(fields: dict, file_field: str, filename: str, payload: bytes) -> tuple:
    boundary = "omavoice-" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
             f"filename=\"{filename}\"\r\nContent-Type: audio/wav\r\n\r\n").encode()
    body += payload
    body += f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", bytes(body)


class WhisperServer:
    """Owns one whisper-server process. Thread safe; requests are serialised."""

    def __init__(self, model: Model, threads: int, language: str = "en", vad_model: Path | None = None):
        self.model = model
        self.threads = threads
        self.vad_model = vad_model
        self.language = "en" if model.english_only else (language or "auto")
        self.port = None
        self._proc = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._failed = None
        self.log_path = cache_dir() / "whisper-server.log"

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self._proc is not None and self._proc.poll() is None

    @property
    def failure(self):
        return self._failed

    def start(self, timeout: float = 90.0) -> None:
        """Start and block until the server answers. Raises RuntimeError on failure."""
        if self.ready:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        cmd = ["whisper-server", "-m", str(self.model.path), "--host", "127.0.0.1",
               "--port", str(self.port), "-t", str(self.threads), "-l", self.language, "-sns",
               *vad_args(self.vad_model)]
        log = open(self.log_path, "wb")
        try:
            self._proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except OSError as exc:
            self._failed = f"whisper-server not found ({exc})"
            raise RuntimeError(self._failed) from exc
        finally:
            log.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                self._failed = f"whisper-server exited with status {self._proc.returncode}; see {self.log_path}"
                raise RuntimeError(self._failed)
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
                conn.request("GET", "/")
                conn.getresponse().read()
                conn.close()
                break
            except (OSError, http.client.HTTPException):
                time.sleep(0.2)
        else:
            self._failed = "whisper-server did not answer in time"
            self.stop()
            raise RuntimeError(self._failed)
        # First inference is slow while buffers are allocated; do it on silence now.
        try:
            self._post(_silent_wav(), {"response_format": "json", "temperature": "0.0"}, timeout=120)
        except Exception:
            pass
        self._ready.set()

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        self._ready.clear()
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

    def transcribe(self, wav_bytes: bytes, prompt: str = "", timeout: float = 120.0) -> str:
        fields = {"response_format": "json", "temperature": "0.0", "no_timestamps": "true"}
        if prompt:
            fields["prompt"] = prompt[-200:]
        data = self._post(wav_bytes, fields, timeout=timeout)
        text = clean_text(data.get("text", ""))
        if text.lower().strip() in HALLUCINATIONS:
            return ""
        return text

    def _post(self, wav_bytes: bytes, fields: dict, timeout: float) -> dict:
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError("whisper-server is not running")
        ctype, body = _multipart(fields, "file", "chunk.wav", wav_bytes)
        with self._lock:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
            try:
                conn.request("POST", "/inference", body=body,
                             headers={"Content-Type": ctype, "Content-Length": str(len(body))})
                resp = conn.getresponse()
                raw = resp.read()
            finally:
                conn.close()
        if resp.status != 200:
            raise RuntimeError(f"whisper-server returned {resp.status}: {raw[:200]!r}")
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return {"text": raw.decode("utf-8", errors="replace")}


NON_SPEECH_MARKERS = ("[", "(", "*", "♪")


def clean_text(text: str) -> str:
    """Trim whisper output and drop bracketed non-speech annotations."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        stripped = line.strip("♪ ").strip()
        if stripped and stripped[0] in NON_SPEECH_MARKERS and stripped[-1] in ("]", ")", "*", "♪"):
            continue
        out.append(line)
    return " ".join(out).strip()


@dataclass
class Segment:
    start: float
    end: float
    text: str


def transcribe_file(raw_path: Path, model: Model, threads: int, language: str, workdir: Path,
                    vad_model: Path | None = None) -> list:
    """Run whisper-cli over a whole raw master. Returns a list of Segment."""
    wav_path = workdir / "final16k.wav"
    raw_to_wav16k_file(raw_path, wav_path)
    out_prefix = workdir / "final"
    lang = "en" if model.english_only else (language or "auto")
    cmd = ["whisper-cli", "-m", str(model.path), "-t", str(threads), "-l", lang, "-np", "-sns",
           *vad_args(vad_model), "-oj", "-of", str(out_prefix), "-f", str(wav_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    json_path = out_prefix.with_suffix(".json")
    if proc.returncode != 0 or not json_path.exists():
        raise RuntimeError(proc.stderr.strip()[-500:] or f"whisper-cli exited with {proc.returncode}")
    data = json.loads(json_path.read_text())
    segments = []
    for item in data.get("transcription", []):
        text = clean_text(item.get("text", ""))
        if not text:
            continue
        offsets = item.get("offsets", {})
        segments.append(Segment(
            start=float(offsets.get("from", 0)) / 1000.0,
            end=float(offsets.get("to", 0)) / 1000.0,
            text=text,
        ))
    return dedupe_segments(segments)


def dedupe_segments(segments: list) -> list:
    """Collapse runs of identical text, whisper's signature on silence."""
    out = []
    for seg in segments:
        if out and out[-1].text.strip().lower() == seg.text.strip().lower():
            out[-1].end = seg.end
            continue
        out.append(seg)
    return out


def render_transcript(segments: list, timestamps: bool) -> str:
    if timestamps:
        lines = [f"[{pcm.format_clock(s.start)}] {s.text}" for s in segments]
        return "\n".join(lines) + ("\n" if lines else "")
    # Paragraph every ~60 seconds of speech, or at a long pause.
    paragraphs, current, para_start, last_end = [], [], None, None
    for seg in segments:
        if current and (seg.start - para_start > 60 or (last_end is not None and seg.start - last_end > 2.5)):
            paragraphs.append(" ".join(current))
            current = []
        if not current:
            para_start = seg.start
        current.append(seg.text)
        last_end = seg.end
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs) + ("\n" if paragraphs else "")


def download_model(name: str, timeout: float | None = None) -> Path:
    """Fetch ggml-<name>.bin into the Omavoice model directory. Blocking."""
    url = next((u for n, _, u in DOWNLOADABLE if n == name), MODEL_DOWNLOAD_BASE + f"ggml-{name}.bin")
    target_dir = data_dir() / "models"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"ggml-{name}.bin"
    tmp = target.with_suffix(".bin.part")
    cmd = ["curl", "-L", "--fail", "--silent", "--show-error", "-o", str(tmp), url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("download timed out")
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(proc.stderr.strip()[-300:] or "download failed")
    os.replace(tmp, target)
    return target
