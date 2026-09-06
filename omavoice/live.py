"""Live captions: feed recorder chunks through the whisper server as you speak."""

import threading
import time

from omavoice import pcm
from omavoice.whisper import pcm_to_wav16k

MIN_FLUSH_SECONDS = 0.6


class LiveTranscriber:
    def __init__(self, recorder, server, chunk_seconds: float, on_text, on_error):
        self.recorder = recorder
        self.server = server
        self.chunk_bytes = pcm.seconds_to_bytes(max(3.0, chunk_seconds))
        self.on_text = on_text
        self.on_error = on_error
        self._stop = threading.Event()
        self._thread = None
        self.last_text = ""
        self.texts = []
        self.busy = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="omavoice-live", daemon=True)
        self._thread.start()

    def stop(self, flush: bool = True) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=180 if flush else 5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.recorder.pending_length() >= self.chunk_bytes:
                data = self.recorder.take_pending()
                cut = pcm.find_cut_point(data)
                chunk, rest = data[:cut], data[cut:]
                self.recorder.unshift_pending(rest)
                self._process(chunk)
            else:
                time.sleep(0.2)
        # Flush what is left once recording has stopped.
        rest = self.recorder.take_pending()
        if len(rest) >= pcm.seconds_to_bytes(MIN_FLUSH_SECONDS):
            self._process(rest)

    def _process(self, chunk: bytes) -> None:
        if not pcm.has_speech(chunk):
            return
        self.busy = True
        try:
            text = self.server.transcribe(pcm_to_wav16k(chunk), prompt=self.last_text)
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            self.on_error(str(exc))
            return
        finally:
            self.busy = False
        if text:
            self.last_text = text
            self.texts.append(text)
            self.on_text(text)

    def full_text(self) -> str:
        return " ".join(self.texts).strip()
