"""Preferences dialog."""

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from omavoice import whisper  # noqa: E402


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, settings, on_changed):
        super().__init__(title="Preferences")
        self.settings = settings
        self.on_changed = on_changed
        self.models = whisper.find_models()

        page = Adw.PreferencesPage(title="General", icon_name="audio-input-microphone-symbolic")
        self.add(page)

        files = Adw.PreferencesGroup(title="Files")
        page.add(files)
        self.dir_row = Adw.EntryRow(title="Recordings folder", text=settings.recordings_dir)
        self.dir_row.connect("apply", self._apply_dir)
        self.dir_row.set_show_apply_button(True)
        files.add(self.dir_row)
        self.ts_row = Adw.SwitchRow(title="Timestamps in transcript",
                                    subtitle="One line per segment, prefixed with [hh:mm:ss]",
                                    active=settings.timestamps)
        self.ts_row.connect("notify::active", lambda r, _: self._set("timestamps", r.get_active()))
        files.add(self.ts_row)
        self.media_row = Adw.SwitchRow(title="Pause media players while recording",
                                       subtitle="Only for microphone takes. Players are left alone when recording an app or system audio.",
                                       active=settings.pause_media)
        self.media_row.connect("notify::active", lambda r, _: self._set("pause_media", r.get_active()))
        files.add(self.media_row)

        speech = Adw.PreferencesGroup(title="Transcription",
                                      description="Models are whisper.cpp ggml files. Omavoice looks in "
                                                  "~/.local/share/omavoice/models and voxtype's model folder.")
        page.add(speech)
        self.live_row = Adw.ComboRow(title="Live caption model", subtitle="Smaller is faster; base.en is a good CPU choice")
        self.final_row = Adw.ComboRow(title="Final transcript model", subtitle="Runs after you stop; can be larger")
        self._fill_model_rows()
        self.live_row.connect("notify::selected", self._on_live_model)
        self.final_row.connect("notify::selected", self._on_final_model)
        speech.add(self.live_row)
        speech.add(self.final_row)

        self.lang_row = Adw.EntryRow(title="Language code (en, de, auto…)", text=settings.language)
        self.lang_row.set_show_apply_button(True)
        self.lang_row.connect("apply", lambda r: self._set("language", r.get_text().strip() or "auto"))
        speech.add(self.lang_row)

        self.chunk_row = Adw.SpinRow.new_with_range(3, 20, 1)
        self.chunk_row.set_title("Live caption chunk length")
        self.chunk_row.set_subtitle("Seconds of audio per caption update. Shorter is snappier but less accurate.")
        self.chunk_row.set_value(settings.chunk_seconds)
        self.chunk_row.connect("notify::value", lambda r, _: self._set("chunk_seconds", float(r.get_value())))
        speech.add(self.chunk_row)

        self.threads_row = Adw.SpinRow.new_with_range(0, 64, 1)
        self.threads_row.set_title("CPU threads")
        self.threads_row.set_subtitle("0 picks automatically")
        self.threads_row.set_value(settings.threads)
        self.threads_row.connect("notify::value", lambda r, _: self._set("threads", int(r.get_value())))
        speech.add(self.threads_row)

        downloads = Adw.PreferencesGroup(title="Download a model",
                                         description="Fetched from the whisper.cpp Hugging Face repository.")
        page.add(downloads)
        for name, size, _url in whisper.DOWNLOADABLE:
            row = Adw.ActionRow(title=name, subtitle=size)
            have = any(m.name == name for m in self.models) or (
                name == whisper.VAD_MODEL_NAME and whisper.find_vad_model() is not None)
            button = Gtk.Button(label="Installed" if have else "Download", valign=Gtk.Align.CENTER,
                                sensitive=not have)
            button.connect("clicked", self._download, name)
            row.add_suffix(button)
            downloads.add(row)

    # -- helpers -------------------------------------------------------

    def _fill_model_rows(self):
        labels = [m.label for m in self.models] or ["No models found"]
        self.live_row.set_model(Gtk.StringList.new(["Automatic (first available)"] + labels))
        self.final_row.set_model(Gtk.StringList.new(["Same as live model"] + labels))
        self.live_row.set_selected(self._index_for(self.settings.live_model, "auto"))
        self.final_row.set_selected(self._index_for(self.settings.final_model, "same"))

    def _index_for(self, setting, sentinel):
        if setting == sentinel or not self.models:
            return 0
        for i, m in enumerate(self.models):
            if str(m.path) == setting:
                return i + 1
        return 0

    def _on_live_model(self, row, _):
        idx = row.get_selected()
        self._set("live_model", "auto" if idx == 0 or not self.models else str(self.models[idx - 1].path))

    def _on_final_model(self, row, _):
        idx = row.get_selected()
        self._set("final_model", "same" if idx == 0 or not self.models else str(self.models[idx - 1].path))

    def _apply_dir(self, row):
        text = row.get_text().strip()
        if text:
            self._set("recordings_dir", text)

    def _set(self, key, value):
        setattr(self.settings, key, value)
        self.settings.save()
        self.on_changed(key)

    def _download(self, button, name):
        button.set_sensitive(False)
        button.set_label("Downloading…")

        def work():
            try:
                whisper.download_model(name)
                GLib.idle_add(self._downloaded, button, name, None)
            except RuntimeError as exc:
                GLib.idle_add(self._downloaded, button, name, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _downloaded(self, button, name, error):
        if error:
            button.set_label("Download")
            button.set_sensitive(True)
            self.add_toast(Adw.Toast.new(f"Download of {name} failed: {error}"))
        else:
            button.set_label("Installed")
            self.models = whisper.find_models()
            self._fill_model_rows()
            self.add_toast(Adw.Toast.new(f"{name} installed"))
        return False
