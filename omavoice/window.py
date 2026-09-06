"""Main window: input picker, transport controls, live transcript, recent list."""

import subprocess
import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from omavoice import APP_NAME, library, sources, whisper  # noqa: E402
from omavoice.formats import FORMATS, by_key, index_of  # noqa: E402
from omavoice.pcm import format_clock  # noqa: E402
from omavoice.session import Session  # noqa: E402

CSS = b"""
.record-button { min-width: 72px; min-height: 72px; border-radius: 36px; }
.record-button.recording { animation: omavoice-pulse 1.4s ease-in-out infinite; }
@keyframes omavoice-pulse { 0% { opacity: 1; } 50% { opacity: 0.55; } 100% { opacity: 1; } }
.transport-button { min-width: 52px; min-height: 52px; border-radius: 26px; }
.elapsed { font-size: 2.6em; font-weight: 300; font-variant-numeric: tabular-nums; }
.transcript { padding: 10px; }
.status-dim { opacity: 0.7; }
"""

SILENCE_PEAK = 0.004
SILENCE_SECONDS = 6.0


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, settings, engine):
        super().__init__(application=app, title=APP_NAME)
        self.settings = settings
        self.engine = engine
        self.session = None
        self.state = "idle"        # idle | recording | paused
        self.sources = []
        self.silent_since = None
        self.pending_final = set()
        self.set_default_size(settings.window_width, settings.window_height)
        self.set_icon_name("io.github.nixfred.omavoice")

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._build()
        self.refresh_sources()
        self.refresh_library()
        GLib.timeout_add(150, self._tick)
        self.connect("close-request", self._on_close_request)

    # -- construction --------------------------------------------------

    def _build(self):
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        view = Adw.ToolbarView()
        self.toast_overlay.set_child(view)

        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title=APP_NAME, subtitle="Ready")
        header.set_title_widget(self.window_title)
        menu = Gio.Menu()
        menu.append("Open recordings folder", "app.open-folder")
        menu.append("Preferences", "app.preferences")
        menu.append("About Omavoice", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        view.add_top_bar(header)

        self.banner = Adw.Banner()
        self.banner.set_button_label("Dismiss")
        self.banner.connect("button-clicked", lambda b: b.set_revealed(False))
        view.add_top_bar(self.banner)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        view.set_content(scroller)
        clamp = Adw.Clamp(maximum_size=860, tightening_threshold=600)
        scroller.set_child(clamp)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                       margin_top=18, margin_bottom=24, margin_start=18, margin_end=18)
        clamp.set_child(body)

        # Input group --------------------------------------------------
        group = Adw.PreferencesGroup(title="Input")
        body.append(group)
        self.source_row = Adw.ComboRow(title="Microphone")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text="Rescan inputs")
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_: self.refresh_sources())
        self.source_row.add_suffix(refresh)
        self.source_row.connect("notify::selected", self._on_source_changed)
        group.add(self.source_row)

        self.format_row = Adw.ComboRow(title="Format", subtitle="Applied when the recording is saved")
        self.format_row.set_model(Gtk.StringList.new([f.label for f in FORMATS]))
        self.format_row.set_selected(index_of(self.settings.format))
        self.format_row.connect("notify::selected", self._on_format_changed)
        group.add(self.format_row)

        self.title_row = Adw.EntryRow(title="Title (optional, added to the file name)")
        group.add(self.title_row)

        # Transport ----------------------------------------------------
        transport = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, halign=Gtk.Align.CENTER)
        body.append(transport)
        self.elapsed = Gtk.Label(label="00:00:00")
        self.elapsed.add_css_class("elapsed")
        transport.append(self.elapsed)

        buttons = Gtk.Box(spacing=18, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        transport.append(buttons)
        self.pause_button = Gtk.Button(icon_name="media-playback-pause-symbolic", tooltip_text="Pause",
                                       valign=Gtk.Align.CENTER, sensitive=False)
        self.pause_button.add_css_class("transport-button")
        self.pause_button.add_css_class("circular")
        self.pause_button.connect("clicked", lambda *_: self.toggle_pause())
        buttons.append(self.pause_button)

        self.record_button = Gtk.Button(icon_name="media-record-symbolic", tooltip_text="Start recording")
        self.record_button.add_css_class("record-button")
        self.record_button.add_css_class("circular")
        self.record_button.add_css_class("destructive-action")
        self.record_button.connect("clicked", lambda *_: self.toggle_record())
        buttons.append(self.record_button)

        self.stop_button = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop and save",
                                      valign=Gtk.Align.CENTER, sensitive=False)
        self.stop_button.add_css_class("transport-button")
        self.stop_button.add_css_class("circular")
        self.stop_button.connect("clicked", lambda *_: self.stop_recording())
        buttons.append(self.stop_button)

        self.level = Gtk.LevelBar(min_value=0.0, max_value=1.0, hexpand=True)
        self.level.set_size_request(320, -1)
        self.level.add_offset_value("low", 0.05)
        self.level.add_offset_value("high", 0.7)
        self.level.add_offset_value("full", 0.95)
        transport.append(self.level)

        self.status = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER)
        self.status.add_css_class("status-dim")
        self.status.set_ellipsize(Pango.EllipsizeMode.NONE)
        transport.append(self.status)

        # Live transcript ---------------------------------------------
        head = Gtk.Box(spacing=12)
        body.append(head)
        label = Gtk.Label(label="Live transcript", xalign=0, hexpand=True)
        label.add_css_class("heading")
        head.append(label)
        self.engine_label = Gtk.Label(label="")
        self.engine_label.add_css_class("dim-label")
        head.append(self.engine_label)
        self.live_switch = Gtk.Switch(active=self.settings.live_transcript, valign=Gtk.Align.CENTER)
        self.live_switch.connect("notify::active", self._on_live_toggled)
        head.append(self.live_switch)

        self.transcript_revealer = Gtk.Revealer(reveal_child=self.settings.live_transcript,
                                                transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        body.append(self.transcript_revealer)
        frame = Gtk.Frame()
        self.transcript_revealer.set_child(frame)
        tscroll = Gtk.ScrolledWindow(min_content_height=200, max_content_height=360,
                                     propagate_natural_height=True)
        frame.set_child(tscroll)
        self.transcript = Gtk.TextView(editable=False, cursor_visible=False,
                                       wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.transcript.add_css_class("transcript")
        self.transcript.set_left_margin(8)
        self.transcript.set_right_margin(8)
        tscroll.set_child(self.transcript)
        self._end_mark = self.transcript.get_buffer().create_mark(None, self.transcript.get_buffer().get_end_iter(), False)

        # Recent recordings -------------------------------------------
        self.library_group = Adw.PreferencesGroup(title="Recent recordings",
                                                  description=str(self.settings.recordings_path()))
        open_button = Gtk.Button(label="Open folder", valign=Gtk.Align.CENTER)
        open_button.connect("clicked", lambda *_: self.open_folder())
        self.library_group.set_header_suffix(open_button)
        body.append(self.library_group)
        self.library_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.library_list.add_css_class("boxed-list")
        self.library_group.add(self.library_list)
        self.empty_row = Adw.ActionRow(title="No recordings yet", subtitle="Press the red button to start.")
        self.empty_row.set_sensitive(False)

    # -- sources -------------------------------------------------------

    def refresh_sources(self):
        self.sources = sources.list_sources()
        default = sources.default_source_name()
        labels = ["System default" + (f" ({self._describe(default)})" if default else "")]
        labels += [s.label for s in self.sources]
        self.source_row.handler_block_by_func(self._on_source_changed)
        self.source_row.set_model(Gtk.StringList.new(labels))
        selected = 0
        for i, s in enumerate(self.sources):
            if s.name == self.settings.source:
                selected = i + 1
        self.source_row.set_selected(selected)
        self.source_row.handler_unblock_by_func(self._on_source_changed)

    def _describe(self, name):
        for s in self.sources:
            if s.name == name:
                return s.description
        return name

    def selected_source_name(self):
        idx = self.source_row.get_selected()
        if idx <= 0 or idx > len(self.sources):
            return "default"
        return self.sources[idx - 1].name

    def _on_source_changed(self, *_):
        self.settings.source = self.selected_source_name()
        self.settings.save()

    def _on_format_changed(self, *_):
        self.settings.format = FORMATS[self.format_row.get_selected()].key
        self.settings.save()

    def _on_live_toggled(self, switch, _):
        self.settings.live_transcript = switch.get_active()
        self.settings.save()
        self.transcript_revealer.set_reveal_child(switch.get_active())
        if switch.get_active() and self.state != "idle" and self.session and self.session.live is None:
            threading.Thread(target=self.session._start_live, daemon=True).start()

    # -- recording -----------------------------------------------------

    def toggle_record(self):
        if self.state == "idle":
            self.start_recording()
        else:
            self.stop_recording()

    def start_recording(self):
        if self.state != "idle":
            return
        fmt = by_key(self.settings.format)
        title = self.title_row.get_text().strip()
        self._clear_transcript()
        callbacks = {
            "on_status": lambda text: GLib.idle_add(self._set_status, text),
            "on_live_text": lambda text: GLib.idle_add(self._append_transcript, text),
            "on_saved": lambda saved, final: GLib.idle_add(self._on_saved, saved, final),
            "on_error": lambda text: GLib.idle_add(self._on_error, text),
            "on_engine": lambda state: GLib.idle_add(self._on_engine, state),
        }
        self.session = Session(self.settings, self.engine, callbacks)
        try:
            self.session.start(self.selected_source_name(), fmt, title, self.live_switch.get_active())
        except Exception:  # error already reported through on_error
            self.session = None
            return
        self.state = "recording"
        self.silent_since = None
        self._apply_state()

    def toggle_pause(self):
        if self.session is None:
            return
        if self.state == "recording":
            self.session.pause()
            self.state = "paused"
        elif self.state == "paused":
            self.session.resume()
            self.state = "recording"
        self._apply_state()

    def stop_recording(self):
        if self.session is None or self.state == "idle":
            return
        session = self.session
        self.session = None
        self.state = "idle"
        self._apply_state()
        self._set_status("Saving…")
        session.stop()

    def _apply_state(self):
        recording = self.state != "idle"
        self.record_button.set_icon_name("media-playback-stop-symbolic" if recording else "media-record-symbolic")
        self.record_button.set_tooltip_text("Stop and save" if recording else "Start recording")
        if self.state == "recording":
            self.record_button.add_css_class("recording")
        else:
            self.record_button.remove_css_class("recording")
        self.pause_button.set_sensitive(recording)
        self.pause_button.set_icon_name("media-playback-start-symbolic" if self.state == "paused"
                                        else "media-playback-pause-symbolic")
        self.pause_button.set_tooltip_text("Resume" if self.state == "paused" else "Pause")
        self.stop_button.set_sensitive(recording)
        self.source_row.set_sensitive(not recording)
        self.format_row.set_sensitive(not recording)
        subtitle = {"idle": "Ready", "recording": "Recording", "paused": "Paused"}[self.state]
        self.window_title.set_subtitle(subtitle)
        if not recording:
            self.level.set_value(0)
            self.banner.set_revealed(False)
            self.engine_label.set_label("")

    def _tick(self):
        if self.session is not None and self.state != "idle":
            self.elapsed.set_label(format_clock(self.session.seconds))
            peak = self.session.peak
            self.level.set_value(min(1.0, peak))
            err = self.session.recorder_error
            if err:
                self._on_error(f"Capture stopped: {err}")
                self.stop_recording()
                return True
            now = GLib.get_monotonic_time() / 1e6
            if self.state == "recording":
                if peak < SILENCE_PEAK:
                    if self.silent_since is None:
                        self.silent_since = now
                    elif now - self.silent_since > SILENCE_SECONDS and not self.banner.get_revealed():
                        self.banner.set_title(f"No audio from “{self._describe(self.selected_source_name())}”. "
                                              "Check the input or pick another one.")
                        self.banner.set_revealed(True)
                else:
                    self.silent_since = None
                    if self.banner.get_revealed():
                        self.banner.set_revealed(False)
        return True

    # -- callbacks from the session -----------------------------------

    def _set_status(self, text):
        self.status.set_label(text or "")
        return False

    def _on_engine(self, state):
        labels = {"starting": "Loading speech model…", "ready": "", "failed": "Transcription unavailable"}
        self.engine_label.set_label(labels.get(state, ""))
        return False

    def _on_error(self, text):
        self.toast_overlay.add_toast(Adw.Toast.new(text))
        return False

    def _on_saved(self, saved, final):
        if final:
            self.pending_final.discard(saved.audio_path)
            self.refresh_library()
            self._set_status("")
            return False
        self.pending_final.add(saved.audio_path)
        self.refresh_library()
        toast = Adw.Toast.new(f"Saved {saved.audio_path.name} ({format_clock(saved.seconds)})")
        toast.set_button_label("Open folder")
        toast.set_action_name("app.open-folder")
        self.toast_overlay.add_toast(toast)
        self.get_application().notify_saved(saved)
        self.title_row.set_text("")
        return False

    def _clear_transcript(self):
        self.transcript.get_buffer().set_text("")

    def _append_transcript(self, text):
        buf = self.transcript.get_buffer()
        end = buf.get_end_iter()
        buf.insert(end, ("" if buf.get_char_count() == 0 else " ") + text)
        buf.move_mark(self._end_mark, buf.get_end_iter())
        self.transcript.scroll_mark_onscreen(self._end_mark)
        return False

    # -- library -------------------------------------------------------

    def refresh_library(self):
        while (row := self.library_list.get_row_at_index(0)) is not None:
            self.library_list.remove(row)
        items = library.scan(self.settings.recordings_path())
        self.library_group.set_description(str(self.settings.recordings_path()))
        if not items:
            self.library_list.append(self.empty_row)
            return
        for rec in items:
            self.library_list.append(self._make_row(rec))

    def _make_row(self, rec):
        row = Adw.ActionRow(title=rec.name, title_lines=1)
        parts = [rec.when.strftime("%a %d %b %Y, %H:%M"), rec.size_label]
        if rec.path in self.pending_final:
            parts.append("transcribing…")
        elif rec.transcript is not None:
            parts.append("transcript")
        row.set_subtitle("  ·  ".join(parts))

        play = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                          tooltip_text="Play")
        play.add_css_class("flat")
        play.connect("clicked", lambda *_: self._xdg_open(rec.path))
        row.add_suffix(play)

        if rec.transcript is not None:
            copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Copy transcript")
            copy.add_css_class("flat")
            copy.connect("clicked", lambda *_: self._copy_transcript(rec))
            row.add_suffix(copy)
            show = Gtk.Button(icon_name="text-x-generic-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Open transcript")
            show.add_css_class("flat")
            show.connect("clicked", lambda *_: self._xdg_open(rec.transcript))
            row.add_suffix(show)

        more_menu = Gio.Menu()
        more_menu.append("Rename…", f"win.rename::{rec.path}")
        more_menu.append("Move to trash", f"win.trash::{rec.path}")
        more = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER, menu_model=more_menu)
        more.add_css_class("flat")
        row.add_suffix(more)
        return row

    def install_actions(self):
        for name, handler in (("rename", self._rename), ("trash", self._trash)):
            action = Gio.SimpleAction.new(name, GLib.VariantType("s"))
            action.connect("activate", handler)
            self.add_action(action)

    def _copy_transcript(self, rec):
        try:
            text = rec.transcript.read_text()
        except OSError as exc:
            self._on_error(f"Could not read transcript: {exc}")
            return
        self.get_clipboard().set(text)
        self.toast_overlay.add_toast(Adw.Toast.new("Transcript copied"))

    def _xdg_open(self, path):
        try:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self._on_error(str(exc))

    def open_folder(self):
        folder = self.settings.recordings_path()
        folder.mkdir(parents=True, exist_ok=True)
        self._xdg_open(folder)

    def _rename(self, action, param):
        path = Path(param.get_string())
        dialog = Adw.AlertDialog(heading="Rename recording", body="The transcript is renamed with it.")
        entry = Gtk.Entry(text=path.stem, activates_default=True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_default_response("rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)

        def done(d, result):
            if d.choose_finish(result) != "rename":
                return
            new_stem = entry.get_text().strip()
            if not new_stem or new_stem == path.stem or "/" in new_stem:
                return
            target = path.with_name(new_stem + path.suffix)
            if target.exists() or target.with_suffix(".txt").exists():
                self._on_error("A recording with that name already exists.")
                return
            try:
                path.rename(target)
                old_txt = path.with_suffix(".txt")
                if old_txt.exists():
                    old_txt.rename(target.with_suffix(".txt"))
            except OSError as exc:
                self._on_error(f"Rename failed: {exc}")
            self.refresh_library()

        dialog.choose(self, None, done)

    def _trash(self, action, param):
        path = Path(param.get_string())
        dialog = Adw.AlertDialog(heading=f"Move “{path.name}” to trash?",
                                 body="The audio and its transcript are moved to the trash.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("trash", "Move to trash")
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)

        def done(d, result):
            if d.choose_finish(result) != "trash":
                return
            for p in (path, path.with_suffix(".txt")):
                if p.exists():
                    try:
                        Gio.File.new_for_path(str(p)).trash(None)
                    except GLib.Error as exc:
                        self._on_error(f"Could not trash {p.name}: {exc.message}")
            self.refresh_library()

        dialog.choose(self, None, done)

    # -- window lifecycle ---------------------------------------------

    def _on_close_request(self, *_):
        if self.state != "idle":
            dialog = Adw.AlertDialog(heading="Stop recording?",
                                     body="The current recording will be saved before closing.")
            dialog.add_response("cancel", "Keep recording")
            dialog.add_response("stop", "Stop and close")
            dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)

            def done(d, result):
                if d.choose_finish(result) == "stop":
                    self.stop_recording()
                    GLib.timeout_add(300, self._close_when_saved)

            dialog.choose(self, None, done)
            return True
        self.settings.window_width, self.settings.window_height = self.get_default_size()
        self.settings.save()
        return False

    def _close_when_saved(self):
        self.state = "idle"
        self.close()
        return False
