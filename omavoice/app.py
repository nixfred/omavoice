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

    def _about(self, *_):
        about = Adw.AboutDialog(application_name=APP_NAME, application_icon=APP_ID, version=__version__,
                                developer_name="Fred Nix", license_type=Gtk.License.MIT_X11,
                                issue_url="https://github.com/nixfred/omavoice/issues",
                                comments="Record from any input and get a transcript beside every file.")
        # Named rows rather than `website`, which libadwaita renders as an
        # unlabelled "Website" and gives no clue where it goes.
        about.add_link("Source code", "https://github.com/nixfred/omavoice")
        about.add_link("nixfred.com", "https://nixfred.com")
        about.present(self.window)

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
