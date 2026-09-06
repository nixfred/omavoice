import json
import os
import struct
import tempfile
import unittest
from datetime import datetime
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
        self.assertEqual(slugify("x" * 200), "x" * 60)

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
        self.assertIn("{ stream.capture.sink = true }", cmd)
        self.assertEqual(cmd[-1], "-")

    def test_default_has_no_target(self):
        from omavoice.recorder import build_command
        self.assertNotIn("--target", build_command("default"))
        self.assertEqual(build_command("alsa_input.x")[-3:], ["--target", "alsa_input.x", "-"])


class SpeechGateTests(unittest.TestCase):
    def test_click_in_silence_is_not_speech(self):
        data = silence(2.0) + tone(0.05, amplitude=0.9) + silence(2.0)
        self.assertFalse(pcm.has_speech(data))

    def test_sustained_signal_is_speech(self):
        data = silence(1.0) + tone(1.0, amplitude=0.2) + silence(1.0)
        self.assertTrue(pcm.has_speech(data))

    def test_dedupe(self):
        from omavoice.whisper import dedupe_segments
        segs = [Segment(0, 1, "Same."), Segment(1, 2, "same."), Segment(2, 3, "Other."), Segment(3, 4, "Same.")]
        out = dedupe_segments(segs)
        self.assertEqual([s.text for s in out], ["Same.", "Other.", "Same."])
        self.assertEqual(out[0].end, 2)


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
        self.assertIn("{ stream.capture.sink = true }", cmd)


class PauseMediaTests(unittest.TestCase):
    def test_only_microphones_pause_players(self):
        from omavoice.session import is_microphone
        self.assertTrue(is_microphone("default"))
        self.assertTrue(is_microphone("alsa_input.pci-0000_00_1f.3.analog-stereo"))
        self.assertFalse(is_microphone("app:3904"))
        self.assertFalse(is_microphone("bluez_output.aa.1.monitor"))
