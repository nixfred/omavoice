"""Orchestrates one recording from first sample to saved files.

No GTK here. The window hands in callbacks; every callback is invoked from
a worker thread, so the window marshals them onto the main loop.
"""

import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from omavoice import mpris, whisper
from omavoice.config import Settings
from omavoice.encoder import encode
from omavoice.formats import AudioFormat
from omavoice.live import LiveTranscriber
from omavoice.naming import basename, transcript_path_for, unique_basename
from omavoice.pcm import bytes_to_seconds
from omavoice.recorder import Recorder, RecorderError

MIN_SECONDS = 0.5


@dataclass
class SavedRecording:
    audio_path: Path
    transcript_path: Path
    seconds: float


class Engine:
    """Lazily started whisper server shared across recordings."""

    def __init__(self):
        self.server = None
        self.model = None
        self.vad_model = None
        self._lock = threading.Lock()
        self.error = None

    def ensure(self, settings: Settings) -> whisper.WhisperServer | None:
        with self._lock:
            model = whisper.resolve_model(settings.live_model)
            if model is None:
                self.error = "No whisper model found. Download one from Preferences."
                return None
            if self.server is not None and self.server.ready and self.model == model:
                return self.server
            if self.server is not None:
                self.server.stop()
            if self.vad_model is None:
                self.vad_model = whisper.ensure_vad_model()
            self.server = whisper.WhisperServer(model, settings.effective_threads(), settings.language,
                                                vad_model=self.vad_model)
            self.model = model
            try:
                self.server.start()
                self.error = None
            except RuntimeError as exc:
                self.error = str(exc)
                self.server = None
                return None
            return self.server

    def shutdown(self) -> None:
        with self._lock:
            if self.server is not None:
                self.server.stop()
                self.server = None


class Session:
    def __init__(self, settings: Settings, engine: Engine, callbacks: dict):
        self.settings = settings
        self.engine = engine
        self.cb = callbacks  # on_status, on_live_text, on_saved, on_error, on_engine
        self.recorder = Recorder()
        self.live = None
        self.fmt = None
        self.title = ""
        self.started_at = None
        self.workdir = None
        self.paused_players = []
        self.live_wanted = False

    # -- controls ------------------------------------------------------

    def start(self, source_name: str, fmt: AudioFormat, title: str, live: bool) -> None:
        self.fmt = fmt
        self.title = title
        self.live_wanted = live
        self.started_at = datetime.now()
        root = self.settings.recordings_path()
        self.workdir = root / ".omavoice-tmp" / basename(self.started_at, title)
        self.workdir.mkdir(parents=True, exist_ok=True)
        master = self.workdir / "master.raw"
        try:
            self.recorder.start(source_name, master)
        except RecorderError as exc:
            self.cb["on_error"](str(exc))
            raise
        if self.settings.pause_media:
            self.paused_players = mpris.pause_playing()
        if live:
            threading.Thread(target=self._start_live, name="omavoice-engine", daemon=True).start()

    def _start_live(self) -> None:
        self.cb["on_engine"]("starting")
        server = self.engine.ensure(self.settings)
        if server is None:
            self.cb["on_engine"]("failed")
            self.cb["on_error"](self.engine.error or "transcription engine unavailable")
            return
        if not self.recorder.running:
            self.cb["on_engine"]("ready")
            return
        self.live = LiveTranscriber(self.recorder, server, self.settings.chunk_seconds,
                                    on_text=self.cb["on_live_text"], on_error=self.cb["on_error"])
        self.live.start()
        self.cb["on_engine"]("ready")

    def pause(self) -> None:
        self.recorder.pause()

    def resume(self) -> None:
        self.recorder.resume()

    @property
    def paused(self) -> bool:
        return self.recorder.paused

    @property
    def seconds(self) -> float:
        return self.recorder.recorded_seconds

    @property
    def peak(self) -> float:
        return self.recorder.last_peak

    @property
    def recorder_error(self):
        return self.recorder.error

    def stop(self) -> None:
        """Stop capture; encoding and transcription continue on a worker thread."""
        total = self.recorder.stop()
        mpris.resume(self.paused_players)
        self.paused_players = []
        threading.Thread(target=self._finish, args=(total,), name="omavoice-finish", daemon=True).start()

    def discard(self) -> None:
        self.recorder.stop()
        mpris.resume(self.paused_players)
        if self.live is not None:
            self.live.stop(flush=False)
        shutil.rmtree(self.workdir, ignore_errors=True)

    # -- finishing -----------------------------------------------------

    def _finish(self, total_bytes: int) -> None:
        seconds = bytes_to_seconds(total_bytes)
        live_text = ""
        if self.live is not None:
            self.cb["on_status"]("Finishing live captions…")
            self.live.stop(flush=True)
            live_text = self.live.full_text()
        if seconds < MIN_SECONDS:
            self._cleanup()
            self.cb["on_error"]("Recording was too short and was discarded.")
            self.cb["on_status"]("")
            return

        root = self.settings.recordings_path()
        root.mkdir(parents=True, exist_ok=True)
        base = unique_basename(root, basename(self.started_at, self.title), self.fmt.ext)
        audio_path = root / f"{base}.{self.fmt.ext}"
        transcript_path = transcript_path_for(audio_path)
        master = self.workdir / "master.raw"

        self.cb["on_status"](f"Encoding {self.fmt.ext.upper()}…")
        try:
            encode(master, audio_path, self.fmt)
        except RuntimeError as exc:
            # Keep the raw audio rather than lose the take.
            fallback = root / f"{base}.raw-pcm-s16le-48k.bin"
            shutil.move(master, fallback)
            self.cb["on_error"](f"Encoding failed: {exc}. Raw audio kept at {fallback.name}.")
            self.cb["on_status"]("")
            return

        # A provisional transcript from live captions appears immediately.
        provisional = live_text + "\n" if live_text else ""
        transcript_path.write_text(provisional)
        saved = SavedRecording(audio_path, transcript_path, seconds)
        self.cb["on_saved"](saved, False)

        final_model = self._final_model()
        if final_model is None or not whisper.have_binaries()[1]:
            self._cleanup()
            self.cb["on_status"]("")
            return
        if self.recorder.max_peak < 0.01:
            # Nothing but silence was captured; whisper would only invent words.
            self._cleanup()
            self.cb["on_saved"](saved, True)
            self.cb["on_status"]("")
            return

        self.cb["on_status"](f"Transcribing {audio_path.name} with {final_model.name}…")
        if self.engine.vad_model is None:
            self.engine.vad_model = whisper.ensure_vad_model()
        try:
            segments = whisper.transcribe_file(master, final_model, self.settings.effective_threads(),
                                               self.settings.language, self.workdir,
                                               vad_model=self.engine.vad_model)
            text = whisper.render_transcript(segments, self.settings.timestamps)
            if text.strip():
                transcript_path.write_text(text)
            elif not provisional:
                transcript_path.write_text("")
        except (RuntimeError, OSError, ValueError) as exc:
            self.cb["on_error"](f"Final transcription failed: {exc}")
        finally:
            self._cleanup()
        self.cb["on_saved"](saved, True)
        self.cb["on_status"]("")

    def _cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)
        try:
            self.workdir.parent.rmdir()   # only succeeds when no other take is in flight
        except OSError:
            pass

    def _final_model(self):
        live_model = self.engine.model or whisper.resolve_model(self.settings.live_model)
        return whisper.resolve_model(self.settings.final_model, fallback=live_model)
