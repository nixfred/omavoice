"""Orchestrates one recording from first sample to saved files.

No GTK here. The window hands in callbacks; every callback is invoked from
a worker thread, so the window marshals them onto the main loop.
"""

import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from omavoice import mpris, vad, whisper
from omavoice.config import Settings
from omavoice.encoder import encode
from omavoice.formats import AudioFormat
from omavoice.live import LiveTranscriber
from omavoice.naming import basename, transcript_path_for, unique_basename
from omavoice.pcm import bytes_to_seconds
from omavoice.recorder import Recorder, RecorderError

MIN_SECONDS = 0.5


_media_lock = threading.Lock()
_media_owner = None          # the take that currently owns the paused players


def is_microphone(source_name: str) -> bool:
    return not (source_name.startswith("app:") or source_name.endswith(".monitor"))


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
        self.key = None
        self.vad_model = None
        self.closed = False
        self._lock = threading.Lock()
        self.error = None

    def ensure(self, settings: Settings) -> whisper.WhisperServer | None:
        with self._lock:
            model = whisper.resolve_model(settings.live_model)
            if model is None:
                self.error = "No whisper model found. Download one from Preferences."
                return None
            key = (model.path, settings.effective_threads(), settings.language)
            if self.server is not None and self.server.ready and self.key == key:
                return self.server
            if self.server is not None:
                self.server.stop()
            if self.vad_model is None:
                self.vad_model = whisper.ensure_vad_model()
            if self.vad_model is None:
                # Without voice activity detection every pause becomes invented
                # text. Refuse live captions rather than fill the pane with it.
                self.error = ("Live captions need the 1 MB voice activity model. "
                              "Download silero-v5.1.2 from Preferences, then try again.")
                return None
            self.server = whisper.WhisperServer(model, settings.effective_threads(), settings.language,
                                                vad_model=self.vad_model)
            self.model = model
            self.key = key
            server = self.server
            try:
                server.start()
            except RuntimeError as exc:
                self.error = str(exc)
                self.server = None
                return None
            if self.closed:      # shutdown happened while the model loaded
                server.stop()
                self.server = None
                return None
            self.error = None
            return server

    def shutdown(self) -> None:
        """Stop the server without taking the lock.

        ensure() holds the lock across a model download and server start, which
        can be a minute or more. Quitting must not wait for that, so the server
        is terminated directly and a start still in flight is told to stand down.
        """
        self.closed = True
        server, self.server = self.server, None
        if server is not None:
            server.stop()


class Session:
    """One take. `on_busy(delta)` lets the app hold itself alive while a take finishes."""

    def __init__(self, settings: Settings, engine: Engine, callbacks: dict):
        # A take keeps its own copy of the settings. Preferences changed while
        # this one is still encoding must not redirect where it saves or which
        # model it finishes with.
        self.settings = replace(settings)
        self.engine = engine
        self.cb = callbacks  # on_status, on_live_text, on_saved, on_error, on_engine
        self.recorder = Recorder()
        self.live = None
        self.live_model = None
        self._live_lock = threading.Lock()
        self._live_starting = False
        self._stopped = False
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
        try:
            tmp_root = root / ".omavoice-tmp"
            tmp_root.mkdir(parents=True, exist_ok=True)
            # Exclusive per-take directory: two takes in the same second must never share files.
            self.workdir = Path(tempfile.mkdtemp(prefix=basename(self.started_at, title) + "-", dir=tmp_root))
            master = self.workdir / "master.raw"
            self.recorder.start(source_name, master)
        except (RecorderError, OSError) as exc:
            self.cb["on_error"](f"Could not start recording: {exc}")
            if self.workdir is not None:
                shutil.rmtree(self.workdir, ignore_errors=True)
            raise RecorderError(str(exc)) from exc
        self._busy(+1)
        if self.settings.pause_media and is_microphone(source_name):
            # Only a mic take benefits from silencing players. Capturing an app
            # or the system output needs that audio to keep playing.
            self._take_media_ownership()
        if live:
            self.enable_live()

    def enable_live(self) -> None:
        """Bring up live captions. Safe to call twice; the second call is a no-op."""
        with self._live_lock:
            if self._stopped or self._live_starting or self.live is not None:
                return
            self._live_starting = True
        threading.Thread(target=self._start_live, name="omavoice-engine", daemon=True).start()

    def _start_live(self) -> None:
        try:
            self.cb["on_engine"]("starting")
            server = self.engine.ensure(self.settings)
            if server is None:
                self.cb["on_engine"]("failed")
                self.cb["on_error"](self.engine.error or "transcription engine unavailable")
                return
            self.live_model = self.engine.model
        finally:
            with self._live_lock:
                self._live_starting = False
        if server is None:
            return
        gate = vad.SpeechGate(self.engine.vad_model, threads=max(2, self.settings.effective_threads() // 2))
        worker = LiveTranscriber(self.recorder, server, self.settings.chunk_seconds,
                                 on_text=self.cb["on_live_text"], on_error=self.cb["on_error"],
                                 gate=gate)
        with self._live_lock:
            # Stop may have run while the model was loading. Publishing the
            # worker now would leave one nobody ever stops. Starting it inside
            # the lock also means stop() can never see a worker whose thread
            # has not been started yet, which join() refuses.
            if self._stopped or not self.recorder.running or self.live is not None:
                self.cb["on_engine"]("ready")
                return
            worker.start()
            self.live = worker
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
        """Return at once. Closing the capture and saving both run on a worker.

        recorder.stop() joins threads with timeouts and mpris.resume() makes
        D-Bus calls, neither of which belongs on the GTK main loop.
        """
        with self._live_lock:
            self._stopped = True
        threading.Thread(target=self._stop_and_finish, name="omavoice-finish", daemon=True).start()

    def _stop_and_finish(self) -> None:
        """Close the capture and save. Nothing here may escape.

        An error closing the file (a full disk, for instance) must still let
        the take be saved, restore the media players and release the hold that
        keeps the application alive.
        """
        total = self.recorder.bytes_written
        try:
            total = self.recorder.stop()
        except Exception as exc:  # noqa: BLE001 - a broken close must not lose the take
            self.cb["on_error"](f"Could not close the recording cleanly: {exc}")
        finally:
            self._release_media()
        self._finish_guarded(total)

    def discard(self) -> None:
        with self._live_lock:
            self._stopped = True
        try:
            self.recorder.stop()
        except Exception:  # noqa: BLE001 - discarding, so the error changes nothing
            pass
        self._release_media()
        if self.live is not None:
            self.live.stop(flush=False)
        shutil.rmtree(self.workdir, ignore_errors=True)
        self._busy(-1)

    def _take_media_ownership(self) -> None:
        """Pause players, inheriting any a previous take is still holding.

        Without the handover, a take that is still encoding would resume the
        players in the middle of the take that followed it.
        """
        global _media_owner
        with _media_lock:
            inherited = []
            if _media_owner is not None:
                inherited = _media_owner.paused_players
                _media_owner.paused_players = []
            fresh = [p for p in mpris.pause_playing() if p not in inherited]
            self.paused_players = inherited + fresh
            _media_owner = self

    def _release_media(self) -> None:
        global _media_owner
        with _media_lock:
            if _media_owner is not self:
                return          # a later take took over; it will resume them
            players, self.paused_players = self.paused_players, []
            _media_owner = None
        mpris.resume(players)

    def _busy(self, delta: int) -> None:
        hook = self.cb.get("on_busy")
        if hook is not None:
            hook(delta)

    # -- finishing -----------------------------------------------------

    def _finish_guarded(self, total_bytes: int) -> None:
        try:
            self._finish(total_bytes)
        except Exception as exc:  # noqa: BLE001 - the take must never vanish silently
            self.cb["on_error"](f"Saving failed: {exc}. Raw audio is in {self.workdir}.")
            self.cb["on_status"]("")
        finally:
            self._busy(-1)

    def _finish(self, total_bytes: int) -> None:
        seconds = bytes_to_seconds(total_bytes)
        if self.recorder.error:
            self.cb["on_error"](f"Capture ended early: {self.recorder.error}")
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
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            # Keep the raw audio rather than lose the take.
            fallback = root / f"{base}.raw-pcm-s16le-48k.bin"
            try:
                shutil.move(master, fallback)
                where = f"Raw audio kept at {fallback.name}."
            except OSError:
                where = f"Raw audio left in {self.workdir}."
            self.cb["on_error"](f"Encoding failed: {exc}. {where}")
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
            self.cb["on_saved"](saved, True)   # nothing more will happen to this take
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
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
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
        # This take's own live model, never whatever the shared engine has
        # moved on to for a later recording.
        live_model = self.live_model or whisper.resolve_model(self.settings.live_model)
        return whisper.resolve_model(self.settings.final_model, fallback=live_model)
