"""Live captions: feed recorder chunks through the whisper server as you speak."""

import threading
import time

from omavoice import pcm
from omavoice.whisper import pcm_to_wav16k

MIN_FLUSH_SECONDS = 0.6


class LiveTranscriber:
    def __init__(self, recorder, server, chunk_seconds: float, on_text, on_error, gate=None):
        self.recorder = recorder
        self.server = server
        self.gate = gate
        self.chunk_bytes = pcm.seconds_to_bytes(max(3.0, chunk_seconds))
        self.on_text = on_text
        self.on_error = on_error
        self._stop = threading.Event()
        self._flush = True
        self._thread = None
        self.texts = []
        self.busy = False

    def start(self) -> None:
        self.recorder.set_buffering(True)
        thread = threading.Thread(target=self._run, name="omavoice-live", daemon=True)
        thread.start()
        self._thread = thread

    def stop(self, flush: bool = True) -> None:
        self._flush = flush
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=300 if flush else 5)
            self._thread = None
        self.recorder.set_buffering(False)

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
        if self._flush and len(rest) >= pcm.seconds_to_bytes(MIN_FLUSH_SECONDS):
            self._process(rest)

    def _process(self, chunk: bytes) -> None:
        # Cheap early-out for dead air only, which skips spawning ffmpeg and
        # the detector for a muted or unplugged input. Anything with signal in
        # it goes to Silero: judging speech by loudness drops short utterances,
        # because a one-word answer is mostly silence by duration.
        if pcm.is_silent(chunk):
            return
        self.busy = True
        try:
            wav = pcm_to_wav16k(chunk)
            # Signal is present, but is any of it voice? Room tone, music and
            # keystrokes get this far and whisper would invent words for them.
            if self.gate is not None and not self.gate.accepts(wav):
                return
            # No prompt. Feeding the previous chunk back in nudges whisper to
            # repeat it verbatim over a pause, and to drop real words it
            # decides are a repeat of what the prompt already said.
            text = self.server.transcribe(wav)
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            self.on_error(str(exc))
            return
        finally:
            self.busy = False
        if text:
            self.texts.append(text)
            self.on_text(text)

    def full_text(self) -> str:
        return " ".join(self.texts).strip()
