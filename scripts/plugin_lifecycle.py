"""Watch the installed Omarchy bar widget from a detached audio worker."""

import json
import os
from pathlib import Path
import time

PLUGIN_ID = "re_pick.voice-recorder"
CONFIG_GRACE_SECONDS = 5


def file_identity(info):
    return [info.st_dev, info.st_ino]


def installation_identity(plugin, entrypoint):
    return [file_identity(plugin.lstat()), file_identity(plugin.stat()),
            file_identity(entrypoint.stat())]


def installed_plugin(script, source):
    invoked = Path(script).absolute()
    home_config = Path.home() / ".config"
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", home_config))
    # Omarchy reads ~/.config/omarchy. Retain the XDG location for existing
    # installations that invoke the plugin there.
    for config in dict.fromkeys((home_config, xdg_config)):
        plugin = config / "omarchy" / "plugins" / PLUGIN_ID
        entrypoint = plugin / "scripts" / source.name
        try:
            identity = installation_identity(plugin, entrypoint)
            if identity[-1] == file_identity(source.stat()):
                return [str(plugin), identity]
        except OSError:
            pass
        if invoked == entrypoint:
            # A command already launched by the shell can race with removal.
            return [str(plugin), None]
    return None


def widget_enabled(path):
    try:
        with path.open(encoding="utf-8") as source:
            config = json.load(source)
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict) or config.get("version") != 1:
        return None
    disabled = config.get("disabledPlugins", [])
    if not isinstance(disabled, list):
        return None
    if PLUGIN_ID in disabled:
        return False
    bar = config.get("bar")
    layout = bar.get("layout") if isinstance(bar, dict) else None
    if not isinstance(layout, dict) or not all(isinstance(items, list) for items in layout.values()):
        return None
    return any(item == PLUGIN_ID or isinstance(item, dict) and item.get("id") == PLUGIN_ID
               for items in layout.values() for item in items)


class WidgetLifecycle:
    def __init__(self, script, installation):
        self.installation = installation
        self.script = Path(script).name
        self.unreadable_since = None

    def available(self):
        if self.installation is None:
            return True
        location, identity = self.installation
        plugin = Path(location)
        try:
            if installation_identity(plugin, plugin / "scripts" / self.script) != identity:
                return False
        except OSError:
            return False
        enabled = widget_enabled(plugin.parent.parent / "shell.json")
        if enabled is not None:
            self.unreadable_since = None
            return enabled
        # An editor or the shell can briefly leave an incomplete configuration.
        now = time.monotonic()
        if self.unreadable_since is None:
            self.unreadable_since = now
        return now - self.unreadable_since < CONFIG_GRACE_SECONDS
