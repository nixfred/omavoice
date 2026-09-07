"""Application object, CLI options, notifications."""

import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from omavoice import APP_ID, APP_NAME, __version__  # noqa: E402
from omavoice.config import Settings  # noqa: E402
from omavoice.session import Engine  # noqa: E402


class OmavoiceApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.settings = Settings.load()
        self.engine = Engine()
        self.window = None
        self._toggle_on_start = False
        self.add_main_option("toggle", ord("t"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Start recording, or stop the recording in progress", None)
        self.add_main_option("version", ord("v"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Print the version and exit", None)
        self.connect("handle-local-options", self._on_local_options)
        self.connect("shutdown", lambda *_: self.engine.shutdown())

    def _on_local_options(self, app, options):
        if options.contains("version"):
            print(f"{APP_NAME} {__version__}")
            return 0
        if options.contains("toggle"):
            try:
                self.register(None)
            except GLib.Error as exc:
                print(f"could not register application: {exc.message}", file=sys.stderr)
                return 1
            if self.get_is_remote():
                self.activate_action("toggle-record", None)
                return 0
            self._toggle_on_start = True
        return -1

    def do_startup(self):
        Adw.Application.do_startup(self)
        for name, handler in (("toggle-record", self._toggle_record), ("open-folder", self._open_folder),
                              ("preferences", self._preferences), ("about", self._about),
                              ("quit", self._quit)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
        self.set_accels_for_action("app.toggle-record", ["<Control>r"])
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def do_activate(self):
        if self.window is None:
            from omavoice.window import MainWindow
            self.window = MainWindow(self, self.settings, self.engine)
            self.window.install_actions()
        self.window.present()
        if self._toggle_on_start:
            self._toggle_on_start = False
            self.window.toggle_record()

    # -- actions -------------------------------------------------------

    def _quit(self, *_):
        # Route through the window so an active take is stopped and saved first.
        if self.window is not None:
            self.window.close()
        else:
            self.quit()

    def busy(self, delta: int):
        """Keep the process alive while a take is being encoded and transcribed."""
        def apply():
            if delta > 0:
                self.hold()
            else:
                self.release()
            return False
        GLib.idle_add(apply)

    def _toggle_record(self, *_):
        if self.window is None:
            self.activate()
        self.window.toggle_record()
        self.window.present()

    def _open_folder(self, *_):
        folder = self.settings.recordings_path()
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _preferences(self, *_):
        from omavoice.preferences import PreferencesDialog

        def changed(key):
            if key == "recordings_dir" and self.window is not None:
                self.window.refresh_library()

        PreferencesDialog(self.settings, changed, busy=self.busy).present(self.window)

    ABOUT_LINKS = (
        ("Source code", "https://github.com/nixfred/omavoice"),
        ("nixfred.com", "https://nixfred.com"),
        ("Report an Issue", "https://github.com/nixfred/omavoice/issues"),
    )

    def _about(self, *_):
        """A hand-built About rather than Adw.AboutDialog.

        AboutDialog only takes extra links through add_link(), which files them
        on its Details subpage, and its front-page rows come from fixed
        properties whose labels cannot be changed. The links belong on the face
        of the dialog, so this lays them out directly.
        """
        dialog = Adw.Dialog(title=f"About {APP_NAME}", content_width=400)
        view = Adw.ToolbarView()
        dialog.set_child(view)
        view.add_top_bar(Adw.HeaderBar(show_title=False))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       margin_top=4, margin_bottom=26, margin_start=22, margin_end=22)
        view.set_content(body)

        icon = Gtk.Image.new_from_icon_name(APP_ID)
        icon.set_pixel_size(96)
        icon.set_margin_bottom(10)
        body.append(icon)

        title = Gtk.Label(label=APP_NAME)
        title.add_css_class("title-1")
        body.append(title)

        developer = Gtk.Label(label="Fred Nix")
        developer.add_css_class("dim-label")
        body.append(developer)

        version = Gtk.Label(label=__version__, margin_top=4, margin_bottom=14)
        version.add_css_class("caption")
        version.add_css_class("accent")
        body.append(version)

        comments = Gtk.Label(label="Record from any input and get a transcript beside every file.",
                             wrap=True, justify=Gtk.Justification.CENTER, margin_bottom=14)
        body.append(comments)

        links = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        links.add_css_class("boxed-list")
        body.append(links)
        for label, uri in self.ABOUT_LINKS:
            row = Adw.ActionRow(title=label, activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
            row.connect("activated", self._open_uri, uri)
            links.append(row)

        legal = Gtk.Label(label="MIT License", margin_top=14)
        legal.add_css_class("dim-label")
        legal.add_css_class("caption")
        body.append(legal)

        dialog.present(self.window)

    def _open_uri(self, _row, uri):
        try:
            subprocess.Popen(["xdg-open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            print(f"could not open {uri}: {exc}", file=sys.stderr)

    def notify_saved(self, saved):
        note = Gio.Notification.new("Recording saved")
        note.set_body(f"{saved.audio_path.name}\n{saved.audio_path.parent}")
        note.set_default_action("app.open-folder")
        note.set_icon(Gio.ThemedIcon.new(APP_ID))
        try:
            self.send_notification("omavoice-saved", note)
        except GLib.Error:
            subprocess.Popen(["notify-send", "-a", APP_NAME, "Recording saved", str(saved.audio_path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main(argv=None):
    app = OmavoiceApp()
    return app.run(sys.argv if argv is None else argv)
