# Changelog

All notable changes to Omavoice. Dates are the day the tag was cut.

## 0.3.10 — 2026-09-07

- A linter now runs over the code, in CI as well as locally, with a config that
  says which rules are deliberately off and why. The pass it flagged was mostly
  cosmetic, but it did find the speech server's warm-up swallowing its failure
  reason without recording it anywhere.
- The README explains that the speech server is a child process, that it is
  stopped when Omavoice exits however it exits, and that an orphan is cleared
  on next launch. It shows up in `ps`, so it should be documented.

## 0.3.9 — 2026-09-07

- The About dialog carries its links on its face: Source code, nixfred.com and
  Report an Issue, rather than a single unlabelled "Website" row one click deep
  on a Details subpage. `Adw.AboutDialog` cannot put named links on its front
  page, so this is a purpose-built dialog.
- **The speech server no longer outlives the app.** Closing the window always
  stopped it, but being killed did not: a `pkill`, a session logout or a crash
  left `whisper-server` running with its model resident, several hundred
  megabytes each, for the rest of the login session. Five had accumulated on
  the development machine holding 1.5 GB. The app now shuts the server down on
  SIGTERM and SIGHUP, and each launch clears up any server a previous run left
  behind, matched by recorded PID and verified to really be a whisper-server
  before anything is signalled.

## 0.3.8 — 2026-09-07

- The About dialog names its links. The repository was there but rendered as an
  unlabelled "Website" row that gave no clue where it went, and there was no
  link to nixfred.com at all. Details now lists "Source code" and "nixfred.com".

## 0.3.7 — 2026-09-07

- Renaming a recording now explains why a name is refused instead of appearing
  to ignore the button, and refuses names starting with a dot, which silently
  turned the recording into a hidden file.
- Continuous integration runs the test suite in a bare Arch container with no
  whisper.cpp, no models and no sound server, which is the shape of machine
  that a packaging failure hides on. The package build is checked on every tag.

## 0.3.6 — 2026-09-07

- The elapsed time is reset when a take stops. It had been leaving the finished
  duration on screen under a heading that said "Ready".

## 0.3.5 — 2026-09-07

- **Repeated speech is no longer deleted from the saved transcript.** Identical
  consecutive segments used to be collapsed, on the assumption they were
  whisper looping on silence. Gating on voice activity in 0.3.0 removed that
  cause, so all the de-duplication did afterwards was throw away real speech:
  a sentence said three times was saved once. Found by testing against clean
  first-generation audio, where the live captions scored a perfect word error
  rate and the saved transcript lost two thirds of the recording.

## 0.3.4 — 2026-09-07

- A failing capture close no longer skips the save, strands the media players
  or leaks the hold that keeps the application alive.
- Stopping while live captions were starting could abandon the encode.
- A take that is still encoding no longer resumes the media players in the
  middle of the take that followed it.
- Short utterances reach the detector. The live pre-filter judged speech by
  loudness across the whole chunk, so a one-word answer, which is mostly
  silence by duration, was dropped before Silero ever saw it.
- Unicode titles survive: "Ünïcödé 日本語" became "n-c-d" before.
- Model downloads report failure instead of stranding a part file and leaving
  the button stuck, and no longer die when the window closes.
- Finishing a download no longer resets the models you had chosen.

## 0.3.3 — 2026-09-07

- Input enumeration moved off the UI thread, where a stalled PipeWire froze
  the window.
- Each take carries an identity, so one that is still saving cannot write into
  the next take's transcript pane or clear a title you have started typing.
- A take keeps its own settings: changing the folder or format in Preferences
  while one is finishing no longer redirects where it saves.
- Rename and trash are disabled while the final transcript is still being written.

## 0.3.2 — 2026-09-07

- The package builds the tagged release under `$srcdir`, so a clean chroot
  build works and the PKGBUILD is fit for the AUR.

## 0.3.1 — 2026-09-07

- Fixed an install failure: two tests assumed whisper.cpp was already on the
  machine, so `check()` failed during `makepkg` for anyone who did not have it.
- Depend on `pulse-native-provider` rather than `pipewire-pulse`, which
  conflicts with `pulseaudio` and made installing ask people to remove their
  sound server.

## 0.3.0 — 2026-09-06

- **Live chunks are gated on Silero voice activity detection.** Handed audio
  with no speech in it, whisper does not return nothing, it returns fluent
  invented text.
- The previous chunk is no longer sent as a prompt. Over a silent chunk it made
  whisper replay that text verbatim, and on real speech it deleted words it
  judged a repeat of the prompt.
- Live captions refuse to start without the detector rather than run unguarded.

## 0.2.0 — 2026-09-05

- Hardened the recording lifecycle: bounded caption buffer, no silent
  reconnection to a different input, exclusive temp directory per take, and
  the application stays alive until a take has finished saving.

## 0.1.0 — 2026-09-05

- First release. Record any microphone, playing application or system output to
  `~/Recordings` in Opus, MP3, M4A, FLAC or WAV, with live captions and a
  transcript saved beside every recording.
