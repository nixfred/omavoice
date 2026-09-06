"""Pause and resume MPRIS media players over D-Bus while recording."""

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"


def _bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def pause_playing() -> list:
    """Pause every player that is currently playing. Returns their bus names."""
    paused = []
    try:
        bus = _bus()
        reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              "ListNames", None, GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 1000, None)
        names = [n for n in reply.unpack()[0] if n.startswith("org.mpris.MediaPlayer2.")]
        for name in names:
            try:
                status = bus.call_sync(name, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "Get",
                                       GLib.Variant("(ss)", (PLAYER_IFACE, "PlaybackStatus")),
                                       GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 1000, None)
                if status.unpack()[0] != "Playing":
                    continue
                bus.call_sync(name, "/org/mpris/MediaPlayer2", PLAYER_IFACE, "Pause", None, None,
                              Gio.DBusCallFlags.NONE, 1000, None)
                paused.append(name)
            except GLib.Error:
                continue
    except GLib.Error:
        pass
    return paused


def resume(names: list) -> None:
    if not names:
        return
    try:
        bus = _bus()
    except GLib.Error:
        return
    for name in names:
        try:
            bus.call_sync(name, "/org/mpris/MediaPlayer2", PLAYER_IFACE, "Play", None, None,
                          Gio.DBusCallFlags.NONE, 1000, None)
        except GLib.Error:
            continue
