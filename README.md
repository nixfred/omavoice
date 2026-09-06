# Omavoice

A voice recorder for [Omarchy](https://omarchy.org/) and other Wayland desktops.
Pick any microphone or system-audio source, hit record, watch the transcript
appear as you speak, and get an audio file plus a matching `.txt` transcript in
`~/Recordings` when you stop.

Built with Python, GTK4 and libadwaita. Audio comes from PipeWire, encoding
from ffmpeg, speech recognition from whisper.cpp running fully on your machine.
Nothing leaves the laptop.

## Features

- **Any input.** Microphones, system-audio monitors, and every application
  that is currently playing audio, each selectable on its own. Pick a browser
  tab's stream and nothing else on the system ends up in the file. The list
  refreshes itself as apps start and stop playing.
- **Five formats.** Opus (default), MP3, M4A/AAC, FLAC, WAV. Audio is captured
  losslessly and encoded when you stop, so the format never affects the transcript.
- **Live transcript** while you record, toggleable, in chunks of a few seconds.
- **Accurate final pass.** After you stop, the whole recording is transcribed
  again and the transcript file is replaced, optionally with a larger model.
- **Pause and resume**, a level meter, and a warning when the input goes silent.
- **Titles.** Type an optional title and it becomes part of the file name:
  `2026-09-05_14-32-10-Weekly-sync.opus` next to `2026-09-05_14-32-10-Weekly-sync.txt`.
- **Recent recordings** list with play, open transcript, copy transcript,
  rename, and move to trash.
- **Notifications** when a file is saved, media players paused while recording,
  and a `--toggle` flag for a global hotkey.
- **Voice activity detection** so whisper is not asked to transcribe silence,
  which is where it invents text.

Omavoice appears in the Omarchy menu (`Super+Space`) under Apps once installed.

## Install

On Arch or Omarchy:

```bash
git clone https://github.com/nixfred/omavoice.git
cd omavoice
makepkg -si
```

The package depends on `python-gobject`, `gtk4`, `libadwaita`, `pipewire-audio`,
`pipewire-pulse`, `libpulse`, `ffmpeg`, `whisper-cpp` and `ggml-cpu`. All are in the official
repositories.

To run from the checkout without installing:

```bash
python -m omavoice
```

## Models

Omavoice looks for whisper.cpp `ggml-*.bin` models in
`~/.local/share/omavoice/models` and, if you use Voxtype, in
`~/.local/share/voxtype/models`. With no model present, open Preferences and
download one. `base.en` is the right choice for live captions on a laptop CPU.
`small.en` makes a good final-pass model. The 1 MB Silero voice-activity model
is fetched automatically the first time the engine starts; you can also download
it from Preferences.

From the shell:

```bash
scripts/download-model.sh small.en
```

## Global hotkey

`omavoice --toggle` starts a recording, or stops the one in progress, in the
running instance. In Omarchy add a binding to `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER SHIFT, R", "exec", "omavoice --toggle", "Toggle voice recording")
```

Inside the window, `Ctrl+R` does the same.

## How it works

1. `pw-record` streams raw 48 kHz mono PCM to a master file in
   `~/Recordings/.omavoice-tmp/`. Pausing drops incoming blocks, so resume needs
   no stitching, and a crash leaves the raw audio intact. Microphones are
   targeted by node name. A sink monitor or a single application's playback
   stream is captured with `stream.capture.sink = true` against the sink or
   the app's stream serial, which is how PipeWire isolates one app's audio.
2. Every few seconds the newest audio is cut at its quietest point, resampled
   to 16 kHz with ffmpeg, and sent to a `whisper-server` that keeps the model
   loaded. The reply is appended to the live transcript.
3. On stop, ffmpeg encodes the master into the chosen format, the live text is
   written as a provisional transcript, and `whisper-cli` produces the final
   transcript in the background.

Settings live in `~/.config/omavoice/config.json`. The whisper server log is in
`~/.cache/omavoice/whisper-server.log`.

## Development

```bash
python -m unittest discover -s tests
```

## License

MIT
