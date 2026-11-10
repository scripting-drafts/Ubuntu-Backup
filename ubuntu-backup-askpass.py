#!/usr/bin/python3 -I
"""GTK askpass helper used by sudo for privileged Ubuntu Backup steps."""
import sys


def main():
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
    except Exception as exc:
        print("GTK password prompt unavailable: " + str(exc), file=sys.stderr)
        return 1
    dialog = Gtk.Dialog(title="Ubuntu Backup — password required")
    dialog.set_modal(True)
    dialog.set_keep_above(True)
    dialog.set_position(Gtk.WindowPosition.CENTER)
    dialog.set_resizable(False)
    dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button("Continue", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    box = dialog.get_content_area()
    box.set_border_width(12)
    box.set_spacing(8)
    box.add(Gtk.Label(
        label="Enter your password to allow this privileged backup step:", xalign=0, wrap=True))
    entry = Gtk.Entry()
    entry.set_visibility(False)
    entry.set_activates_default(True)
    box.add(entry)
    box.show_all()
    response = dialog.run()
    password = entry.get_text()
    dialog.destroy()
    if response != Gtk.ResponseType.OK or not password:
        return 1
    sys.stdout.write(password + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
