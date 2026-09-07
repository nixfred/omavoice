import json
import os
import struct
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from pathlib import Path

from omavoice import pcm
from omavoice.config import Settings
from omavoice.formats import FORMATS, by_key, index_of
from omavoice.naming import basename, slugify, unique_basename
from omavoice.sources import parse_app_streams, parse_sources
from omavoice.whisper import Segment, clean_text, render_transcript


def tone(seconds, amplitude=0.5, freq=440):
    n = int(pcm.SAMPLE_RATE * seconds)
    import math
    return b"".join(struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / pcm.SAMPLE_RATE)))
                    for i in range(n))


def silence(seconds):
    return b"\x00\x00" * int(pcm.SAMPLE_RATE * seconds)


class PcmTests(unittest.TestCase):
    def test_peak_and_silence(self):
        self.assertEqual(pcm.peak(b""), 0.0)
        self.assertTrue(pcm.is_silent(silence(0.1)))
        self.assertAlmostEqual(pcm.peak(tone(0.05)), 0.5, places=2)
        self.assertFalse(pcm.is_silent(tone(0.05)))

    def test_cut_point_lands_in_the_quiet_gap(self):
        data = tone(2.0) + silence(0.3) + tone(0.4)
        cut = pcm.find_cut_point(data, search_seconds=1.0)
        gap_start = len(tone(2.0))
        gap_end = gap_start + len(silence(0.3))
        self.assertTrue(gap_start <= cut <= gap_end, (cut, gap_start, gap_end))
        self.assertEqual(cut % 2, 0)

    def test_cut_point_on_short_buffer_is_end(self):
        data = tone(0.02)
        self.assertEqual(pcm.find_cut_point(data), len(data))

    def test_clock(self):
        self.assertEqual(pcm.format_clock(0), "00:00:00")
        self.assertEqual(pcm.format_clock(3725.9), "01:02:05")


class NamingTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("  Stand-up: notes / plan!  "), "Stand-up-notes-plan")
        self.assertEqual(slugify(""), "")
        self.assertEqual(slugify("x" * 200), "x" * 80)

    def test_slugify_keeps_unicode_but_drops_separators(self):
        self.assertEqual(slugify("Ünïcödé ✨ 日本語"), "Ünïcödé-日本語")
        self.assertEqual(slugify("../../etc/passwd"), "etc-passwd")
        self.assertEqual(slugify("a\x00b"), "a-b")
        self.assertEqual(slugify("...."), "")
        for hostile in ("../../etc/passwd", "a/b", "x" * 300, "日" * 200, "\n\t"):
            slug = slugify(hostile)
            self.assertNotIn("/", slug)
            self.assertFalse(slug.startswith("."))
            self.assertLessEqual(len(slug.encode("utf-8")), 80)

    def test_basename(self):
        when = datetime(2026, 9, 5, 14, 32, 10)
        self.assertEqual(basename(when), "2026-09-05_14-32-10")
        self.assertEqual(basename(when, "Weekly sync"), "2026-09-05_14-32-10-Weekly-sync")

    def test_unique_basename_avoids_audio_and_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "a.opus").touch()
            (d / "a-2.txt").touch()
            self.assertEqual(unique_basename(d, "a", "opus"), "a-3")
            self.assertEqual(unique_basename(d, "b", "opus"), "b")


class FormatTests(unittest.TestCase):
    def test_five_formats(self):
        self.assertEqual(len(FORMATS), 5)
        self.assertEqual({f.key for f in FORMATS}, {"opus", "mp3", "m4a", "flac", "wav"})

    def test_lookup(self):
        self.assertEqual(by_key("flac").ext, "flac")
        self.assertEqual(by_key("nope").key, "opus")
        self.assertEqual(index_of("wav"), 4)


class SourceTests(unittest.TestCase):
    PAYLOAD = json.dumps([
        {"index": 57, "name": "alsa_output.x.monitor", "description": "Monitor of Built-in Audio",
         "state": "SUSPENDED", "properties": {"device.class": "monitor"}},
        {"index": 58, "name": "alsa_input.x", "description": "Built-in Audio Analog Stereo",
         "state": "SUSPENDED", "properties": {"device.class": "sound"}},
        {"index": 2985, "name": "bluez_input.aa", "description": "AirPods Pro 3", "state": "SUSPENDED",
         "properties": {}},
    ])

    def test_mics_first_then_monitors(self):
        srcs = parse_sources(self.PAYLOAD)
        self.assertEqual([s.name for s in srcs], ["bluez_input.aa", "alsa_input.x", "alsa_output.x.monitor"])
        self.assertTrue(srcs[-1].is_monitor)
        self.assertEqual(srcs[-1].label, "System audio: Built-in Audio")
        self.assertEqual(srcs[0].label, "AirPods Pro 3")

    def test_garbage_is_empty(self):
        self.assertEqual(parse_sources([]), [])
        self.assertEqual(parse_sources([{"description": "no name"}]), [])


class ConfigTests(unittest.TestCase):
    def test_roundtrip_and_tolerance(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            s = Settings(format="flac", chunk_seconds=5.0)
            s.save(path)
            loaded = Settings.load(path)
            self.assertEqual(loaded.format, "flac")
            self.assertEqual(loaded.chunk_seconds, 5.0)
            path.write_text("{not json")
            self.assertEqual(Settings.load(path).format, "opus")
            path.write_text(json.dumps({"format": "mp3", "unknown_key": 1}))
            self.assertEqual(Settings.load(path).format, "mp3")

    def test_threads(self):
        self.assertGreaterEqual(Settings().effective_threads(), 2)
        self.assertEqual(Settings(threads=3).effective_threads(), 3)


class TranscriptTests(unittest.TestCase):
    def test_clean_text_drops_annotations(self):
        self.assertEqual(clean_text(" (eerie music)\n"), "")
        self.assertEqual(clean_text("[BLANK_AUDIO]"), "")
        self.assertEqual(clean_text(" Hello there.\n General Kenobi. "), "Hello there. General Kenobi.")

    def test_render_plain_and_timestamps(self):
        segs = [Segment(0.0, 2.0, "One."), Segment(2.2, 4.0, "Two."), Segment(70.0, 72.0, "Three.")]
        self.assertEqual(render_transcript(segs, False), "One. Two.\n\nThree.\n")
        self.assertEqual(render_transcript(segs, True), "[00:00:00] One.\n[00:00:02] Two.\n[00:01:10] Three.\n")
        self.assertEqual(render_transcript([], False), "")


if __name__ == "__main__":
    unittest.main()


class RecorderCommandTests(unittest.TestCase):
    def test_monitor_targets_the_sink(self):
        from omavoice.recorder import build_command
        cmd = build_command("bluez_output.aa.1.monitor")
        self.assertIn("--target", cmd)
        self.assertEqual(cmd[cmd.index("--target") + 1], "bluez_output.aa.1")
        self.assertIn("stream.capture.sink = true", cmd[cmd.index("-P") + 1])
        self.assertEqual(cmd[-1], "-")

    def test_default_has_no_target(self):
        from omavoice.recorder import build_command
        self.assertNotIn("--target", build_command("default"))
        self.assertEqual(build_command("alsa_input.x")[-3:], ["--target", "alsa_input.x", "-"])
        self.assertIn("-P", build_command("alsa_input.x"))


class SpeechGateTests(unittest.TestCase):
    def test_dead_air_is_skipped_before_the_detector(self):
        self.assertTrue(pcm.is_silent(silence(2.0)))

    def test_a_short_utterance_still_reaches_the_detector(self):
        # Judging by loudness alone would drop this: it is mostly silence by
        # duration, but it is exactly what a one-word answer looks like.
        data = silence(3.0) + tone(0.4, amplitude=0.3) + silence(3.0)
        self.assertFalse(pcm.is_silent(data))

    def test_repeated_speech_is_kept(self):
        # Somebody saying the same thing three times must appear three times.
        # This used to be collapsed as if it were a whisper hallucination.
        from omavoice.whisper import render_transcript
        segs = [Segment(0, 2, "No."), Segment(2, 4, "No."), Segment(4, 6, "No.")]
        self.assertEqual(render_transcript(segs, False), "No. No. No.\n")


class AppStreamTests(unittest.TestCase):
    DUMP = json.dumps([
        {"id": 75, "type": "PipeWire:Interface:Node", "info": {"state": "running", "props": {
            "media.class": "Stream/Output/Audio", "application.name": "pw-play", "node.name": "pw-play",
            "media.name": "pw-play", "object.serial": 3897}}},
        {"id": 77, "type": "PipeWire:Interface:Node", "info": {"state": "running", "props": {
            "media.class": "Stream/Output/Audio", "application.name": "Brave", "node.name": "brave",
            "media.name": "Why anchors are insane", "object.serial": 3904}}},
        {"id": 58, "type": "PipeWire:Interface:Node", "info": {"props": {
            "media.class": "Audio/Source", "node.name": "alsa_input.x", "object.serial": 100}}},
        {"id": 5, "type": "PipeWire:Interface:Port", "info": {"props": {"media.class": "Stream/Output/Audio"}}},
    ])

    def test_only_playback_streams(self):
        apps = parse_app_streams(self.DUMP)
        self.assertEqual([a.name for a in apps], ["app:3904", "app:3897"])
        self.assertEqual(apps[0].label, "App: Brave \u2014 Why anchors are insane")
        self.assertEqual(apps[1].label, "App: pw-play")
        self.assertTrue(all(a.is_app for a in apps))

    def test_recorder_targets_serial(self):
        from omavoice.recorder import build_command
        cmd = build_command("app:3904")
        self.assertEqual(cmd[cmd.index("--target") + 1], "3904")
        self.assertIn("stream.capture.sink = true", cmd[cmd.index("-P") + 1])


class PauseMediaTests(unittest.TestCase):
    def test_only_microphones_pause_players(self):
        from omavoice.session import is_microphone
        self.assertTrue(is_microphone("default"))
        self.assertTrue(is_microphone("alsa_input.pci-0000_00_1f.3.analog-stereo"))
        self.assertFalse(is_microphone("app:3904"))
        self.assertFalse(is_microphone("bluez_output.aa.1.monitor"))


class HardeningTests(unittest.TestCase):
    def test_config_rejects_wrong_types(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps({"recordings_dir": None, "threads": "8", "chunk_seconds": 5,
                                        "timestamps": "yes", "format": "flac"}))
            s = Settings.load(path)
            self.assertTrue(s.recordings_dir.endswith("Recordings"))
            self.assertEqual(s.threads, 0)
            self.assertEqual(s.chunk_seconds, 5.0)
            self.assertFalse(s.timestamps)
            self.assertEqual(s.format, "flac")

    def test_explicit_targets_never_fall_back(self):
        from omavoice.recorder import build_command
        for name in ("alsa_input.x", "sink.monitor", "app:12"):
            props = build_command(name)[build_command(name).index("-P") + 1]
            self.assertIn("node.dont-fallback = true", props, name)
            self.assertIn("node.dont-reconnect = true", props, name)
        self.assertNotIn("-P", build_command("default"))

    def test_pending_buffer_is_opt_in_and_capped(self):
        from omavoice.recorder import Recorder, PENDING_CAP_BYTES
        r = Recorder()
        self.assertFalse(r.buffer_pending)
        r.set_buffering(True)
        with r._lock:
            r._pending += b"\x00" * (PENDING_CAP_BYTES + 10)
        self.assertGreater(r.pending_length(), PENDING_CAP_BYTES)
        r.set_buffering(False)
        self.assertEqual(r.pending_length(), 0)

    def test_engine_key_includes_language_and_threads(self):
        from omavoice.session import Engine
        e = Engine()
        self.assertIsNone(e.key)


class VadTests(unittest.TestCase):
    NONE = "Detected 0 speech segments: \n"
    ONE = "Detected 1 speech segments: \nSpeech segment 0: start = 0.00, end = 109.00\n"
    THREE = ("Detected 3 speech segments: \nSpeech segment 0: start = 0.00, end = 150.00\n"
             "Speech segment 1: start = 186.00, end = 402.00\n"
             "Speech segment 2: start = 455.00, end = 690.00\n")

    def test_no_segments(self):
        from omavoice import vad
        self.assertEqual(vad.analyze(self.NONE), [])
        self.assertEqual(vad.speech_seconds(vad.analyze(self.NONE)), 0.0)

    def test_unreadable_output_is_not_silence(self):
        from omavoice import vad
        # The binary exits zero when it rejects an argument. "no answer" must
        # never be mistaken for "no speech", or every chunk gets dropped.
        self.assertIsNone(vad.analyze(""))
        self.assertIsNone(vad.analyze("error: unknown argument: -vspd"))
        self.assertEqual(vad.analyze(self.NONE), [])

    @staticmethod
    def _detector(stdout, returncode=0, stderr=""):
        """Stand in for the detector binary, so these tests need nothing installed."""
        proc = mock.Mock(stdout=stdout, returncode=returncode, stderr=stderr)
        return mock.patch.object(vad_module().subprocess, "run", return_value=proc)

    def test_gate_fails_open_on_unreadable_output(self):
        vad = vad_module()
        with mock.patch.object(vad, "available", return_value=True), \
                self._detector("error: unknown argument: -vspd"):
            gate = vad.SpeechGate(__file__)
            self.assertTrue(gate.armed)
            self.assertTrue(gate.accepts(b"RIFF"))
            self.assertEqual(gate.rejected, 0)
            self.assertIsNotNone(gate.last_error)

    def test_centiseconds_become_seconds(self):
        from omavoice import vad
        self.assertEqual(vad.analyze(self.ONE), [(0.0, 1.09)])
        self.assertAlmostEqual(vad.speech_seconds(vad.analyze(self.ONE)), 1.09)
        self.assertEqual(len(vad.analyze(self.THREE)), 3)
        self.assertAlmostEqual(vad.speech_seconds(vad.analyze(self.THREE)), 1.50 + 2.16 + 2.35)
        self.assertEqual(vad.speech_seconds(None), 0.0)

    def test_gate_fails_open_without_a_model(self):
        from omavoice.vad import SpeechGate
        gate = SpeechGate(None)
        self.assertFalse(gate.armed)
        self.assertTrue(gate.accepts(b"anything"))
        self.assertEqual(gate.checked, 0)

    def test_gate_rejects_a_chunk_with_no_speech(self):
        vad = vad_module()
        with mock.patch.object(vad, "available", return_value=True), \
                self._detector(self.NONE):
            gate = vad.SpeechGate(__file__)   # any existing file satisfies `armed`
            self.assertFalse(gate.accepts(b"RIFF"))
            self.assertEqual(gate.rejected, 1)

    def test_gate_passes_a_chunk_that_holds_speech(self):
        vad = vad_module()
        with mock.patch.object(vad, "available", return_value=True), \
                self._detector(self.ONE):
            gate = vad.SpeechGate(__file__)
            self.assertTrue(gate.accepts(b"RIFF"))
            self.assertEqual(gate.rejected, 0)

    def test_gate_is_inert_when_the_detector_is_not_installed(self):
        vad = vad_module()
        with mock.patch.object(vad, "available", return_value=False):
            gate = vad.SpeechGate(__file__)
            self.assertFalse(gate.armed)
            self.assertTrue(gate.accepts(b"RIFF"))


class NoPromptTests(unittest.TestCase):
    def test_server_transcribe_takes_no_prompt(self):
        import inspect
        from omavoice.whisper import WhisperServer
        params = inspect.signature(WhisperServer.transcribe).parameters
        self.assertNotIn("prompt", params)

    def test_live_transcriber_keeps_no_carry_forward_text(self):
        from omavoice.live import LiveTranscriber
        live = LiveTranscriber.__new__(LiveTranscriber)
        self.assertFalse(hasattr(live, "last_text"))
        src = inspect_source()
        self.assertNotIn("prompt=", src)


def inspect_source():
    import inspect
    from omavoice import live
    return inspect.getsource(live)


def vad_module():
    from omavoice import vad
    return vad


def _null_callbacks():
    names = ("on_status", "on_live_text", "on_saved", "on_error", "on_engine")
    return {n: (lambda *a, **k: None) for n in names}


class SessionIsolationTests(unittest.TestCase):
    def _session(self, settings=None):
        from omavoice.config import Settings
        from omavoice.session import Engine, Session
        return Session(settings or Settings(), Engine(), _null_callbacks())

    def test_a_take_keeps_its_own_settings(self):
        from omavoice.config import Settings
        settings = Settings(format="opus", recordings_dir="/tmp/first")
        session = self._session(settings)
        settings.format = "flac"                  # user opens Preferences mid-take
        settings.recordings_dir = "/tmp/second"
        self.assertEqual(session.settings.format, "opus")
        self.assertEqual(session.settings.recordings_dir, "/tmp/first")

    def test_enable_live_does_nothing_once_stopped(self):
        session = self._session()
        session._stopped = True
        session.enable_live()
        self.assertFalse(session._live_starting)
        self.assertIsNone(session.live)

    def test_enable_live_only_starts_one_worker(self):
        session = self._session()
        session._live_starting = True             # a startup is already in flight
        session.enable_live()
        self.assertIsNone(session.live)

    def test_final_model_ignores_a_later_takes_engine(self):
        from omavoice.whisper import Model
        session = self._session()
        mine = Model(Path("/models/ggml-base.en.bin"))
        session.live_model = mine
        session.engine.model = Model(Path("/models/ggml-large-v3.bin"))
        self.assertEqual(session._final_model(), mine)


class NonBlockingTests(unittest.TestCase):
    def test_engine_shutdown_does_not_need_the_lock(self):
        from omavoice.session import Engine
        engine = Engine()
        engine._lock.acquire()          # as if ensure() were loading a model
        try:
            engine.shutdown()           # must not deadlock
            self.assertTrue(engine.closed)
        finally:
            engine._lock.release()

    def test_stop_returns_without_waiting_for_the_save(self):
        import inspect
        from omavoice.session import Session
        src = inspect.getsource(Session.stop)
        self.assertIn("Thread", src)
        self.assertNotIn("self.recorder.stop()", src)


class DownloadTests(unittest.TestCase):
    def test_a_missing_curl_becomes_a_clean_error_and_leaves_nothing(self):
        from omavoice import whisper
        with tempfile.TemporaryDirectory() as d:
            models = Path(d) / "models"
            with mock.patch.object(whisper, "data_dir", return_value=Path(d)), \
                    mock.patch.object(whisper.subprocess, "run", side_effect=OSError("no curl")):
                with self.assertRaises(RuntimeError):
                    whisper.download_model("tiny.en")
            self.assertEqual(list(models.glob("*.part")), [])

    def test_a_failed_commit_becomes_a_clean_error_and_leaves_nothing(self):
        from omavoice import whisper
        with tempfile.TemporaryDirectory() as d:
            models = Path(d) / "models"
            ok = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(whisper, "data_dir", return_value=Path(d)), \
                    mock.patch.object(whisper.subprocess, "run", return_value=ok), \
                    mock.patch.object(whisper.os, "replace", side_effect=OSError("read-only")):
                with self.assertRaises(RuntimeError):
                    whisper.download_model("tiny.en")
            self.assertEqual(list(models.glob("*.part")), [])


class MediaOwnershipTests(unittest.TestCase):
    def setUp(self):
        from omavoice import session as session_module
        self.mod = session_module
        self.mod._media_owner = None
        self.addCleanup(setattr, self.mod, "_media_owner", None)

    def _session(self):
        from omavoice.config import Settings
        from omavoice.session import Engine, Session
        return Session(Settings(), Engine(), _null_callbacks())

    def test_a_later_take_inherits_the_paused_players(self):
        from omavoice import mpris
        first, second = self._session(), self._session()
        with mock.patch.object(mpris, "pause_playing", return_value=["org.mpris.a"]):
            first._take_media_ownership()
            self.assertEqual(first.paused_players, ["org.mpris.a"])
            with mock.patch.object(mpris, "pause_playing", return_value=[]):
                second._take_media_ownership()
        # the handover moved the players, so the first take cannot resume them
        self.assertEqual(first.paused_players, [])
        self.assertEqual(second.paused_players, ["org.mpris.a"])

    def test_only_the_owner_resumes(self):
        from omavoice import mpris
        first, second = self._session(), self._session()
        with mock.patch.object(mpris, "pause_playing", return_value=["org.mpris.a"]):
            first._take_media_ownership()
            with mock.patch.object(mpris, "pause_playing", return_value=[]):
                second._take_media_ownership()
        with mock.patch.object(mpris, "resume") as resumed:
            first._release_media()          # the older take finishing mid-recording
            resumed.assert_not_called()
            second._release_media()
            resumed.assert_called_once_with(["org.mpris.a"])


class ClockTests(unittest.TestCase):
    def test_the_idle_clock_reads_zero(self):
        # The window resets the label through the same formatter it ticks with.
        self.assertEqual(pcm.format_clock(0), "00:00:00")
