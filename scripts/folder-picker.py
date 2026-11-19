#!/usr/bin/python3
"""Print the selected local directory; dismissing the chooser prints nothing."""

import os
import sys
from pathlib import Path


def choose(initial, title=None):
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, GLib, Gtk

    if not Gtk.init_check():
        raise RuntimeError("Could not connect to the desktop display")
    chooser = Gtk.FileDialog()
    if title:
        chooser.set_title(title)
    folder = Path(initial).expanduser()
    if folder.is_dir():
        chooser.set_initial_folder(Gio.File.new_for_path(os.fspath(folder)))

    loop = GLib.MainLoop()
    result = None

    def finished(dialog, async_result):
        nonlocal result
        result = async_result
        loop.quit()

    chooser.select_folder(None, None, finished)
    loop.run()
    try:
        selected = chooser.select_folder_finish(result)
    except GLib.Error as error:
        if any(error.matches(Gtk.DialogError.quark(), code) for code in
               (Gtk.DialogError.DISMISSED, Gtk.DialogError.CANCELLED)):
            return None
        raise RuntimeError(error.message) from error

    path = selected.get_path()
    if not path or not os.path.isabs(path):
        raise ValueError("Select a local folder to save recordings")
    if "\n" in path or "\r" in path:
        raise ValueError("The folder name contains a line break")
    return path


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit("Usage: folder-picker.py DIRECTORY [TITLE]")
    try:
        path = choose(*sys.argv[1:])
    except (ImportError, ValueError, RuntimeError) as error:
        sys.exit(str(error))
    if path is not None:
        print(path)


if __name__ == "__main__":
    main()
