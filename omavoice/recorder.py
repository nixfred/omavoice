"""Capture from a PipeWire source with pw-record, streaming raw PCM to a file.

pw-record writes 16-bit mono 48 kHz PCM to stdout. A reader thread appends
it to the master file, keeps a pending buffer for live captions, and tracks
the peak level. Pausing simply drops incoming blocks, so there is nothing
to stitch back together on resume.
"""

import subprocess
import threading
from pathlib import Path

from omavoice import pcm

BLOCK_BYTES = pcm.seconds_to_bytes(0.1)
MONITOR_SUFFIX = ".monitor"
APP_PREFIX = "app:"


def build_command(source_name: str) -> list:
    """pw-record argv for a pactl source name.

    pactl lists a sink's monitor as "<sink>.monitor", but that is not a
    PipeWire node. To capture what a sink plays, target the sink itself and
    ask for a sink-capture stream. The same flag against an application's
    output stream captures just that application.
    """
    cmd = ["pw-record", "--rate", str(pcm.SAMPLE_RATE), "--channels", str(pcm.CHANNELS),
           "--format", "s16", "--raw"]
    if source_name and source_name != "default":
        if source_name.startswith(APP_PREFIX):
            # A single application's playback stream, addressed by object.serial.
            cmd += ["-P", "{ stream.capture.sink = true }", "--target", source_name[len(APP_PREFIX):]]
        elif source_name.endswith(MONITOR_SUFFIX):
            cmd += ["-P", "{ stream.capture.sink = true }", "--target", source_name[:-len(MONITOR_SUFFIX)]]
        else:
            cmd += ["--target", source_name]
    cmd.append("-")
    return cmd


class RecorderError(Exception):
    pass


class Recorder:
    def __init__(self):
        self._proc = None
        self._thread = None
        self._file = None
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._paused = False
        self._stopping = False
        self.bytes_written = 0
        self.last_peak = 0.0
        self.max_peak = 0.0
        self.error = None
        self.master_path = None
        self._stderr_path = None

    # -- lifecycle -----------------------------------------------------

    def start(self, source_name: str, master_path: Path) -> None:
        if self._proc is not None:
            raise RecorderError("already recording")
        master_path.parent.mkdir(parents=True, exist_ok=True)
        self.master_path = master_path
        self._stderr_path = master_path.with_suffix(".pw-record.log")
        self._file = open(master_path, "wb")
        self.bytes_written = 0
        self.last_peak = 0.0
        self.max_peak = 0.0
        self.error = None
        self._paused = False
        self._stopping = False
        with self._lock:
            self._pending = bytearray()

        cmd = build_command(source_name)
        stderr = open(self._stderr_path, "wb")
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr, stdin=subprocess.DEVNULL)
        except OSError as exc:
            stderr.close()
            self._file.close()
            self._file = None
            raise RecorderError(f"could not start pw-record: {exc}") from exc
        finally:
            stderr.close()
        self._thread = threading.Thread(target=self._reader, name="omavoice-recorder", daemon=True)
        self._thread.start()

    def pause(self) -> None:
        self._paused = True
        self.last_peak = 0.0

    def resume(self) -> None:
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and not self._stopping

    def stop(self) -> int:
        """Stop capture and return the number of PCM bytes in the master file."""
        self._stopping = True
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None
        self._proc = None
        self._thread = None
        return self.bytes_written

    def stderr_text(self) -> str:
        try:
            return Path(self._stderr_path).read_text(errors="replace").strip()
        except (OSError, TypeError):
            return ""

    # -- data access ---------------------------------------------------

    @property
    def recorded_seconds(self) -> float:
        return pcm.bytes_to_seconds(self.bytes_written)

    def pending_length(self) -> int:
        with self._lock:
            return len(self._pending)

    def take_pending(self) -> bytes:
        with self._lock:
            data = bytes(self._pending)
            self._pending = bytearray()
        return data

    def unshift_pending(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._pending[0:0] = data

    # -- internals -----------------------------------------------------

    def _reader(self) -> None:
        proc = self._proc
        stream = proc.stdout
        try:
            while True:
                block = stream.read(BLOCK_BYTES)
                if not block:
                    break
                if self._paused or self._stopping:
                    continue
                self._file.write(block)
                self.bytes_written += len(block)
                self.last_peak = pcm.peak(block)
                if self.last_peak > self.max_peak:
                    self.max_peak = self.last_peak
                with self._lock:
                    self._pending += block
        except (OSError, ValueError) as exc:
            self.error = str(exc)
        finally:
            code = proc.wait()
            if code not in (0, -15, -2) and not self._stopping:
                self.error = self.stderr_text() or f"pw-record exited with status {code}"
