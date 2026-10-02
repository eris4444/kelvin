"""XDG autostart entry (~/.config/autostart), run by uwsm at login."""

from __future__ import annotations

import shutil

from . import APP_ID, APP_NAME, paths

ENTRY = f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Comment=Applies your AC/battery GPU profiles and shows the Kelvin tray icon
Exec={{exe}} --background
Icon={APP_ID}
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
"""


def executable() -> str:
    return shutil.which("kelvin") or "/usr/bin/kelvin"


def is_enabled() -> bool:
    return paths.autostart_file().is_file()


def set_enabled(enabled: bool) -> None:
    paths.legacy_autostart_file().unlink(missing_ok=True)
    path = paths.autostart_file()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = ENTRY.format(exe=executable())
        if not path.is_file() or path.read_text() != content:
            path.write_text(content)
    else:
        path.unlink(missing_ok=True)
