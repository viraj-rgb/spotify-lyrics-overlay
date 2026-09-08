"""Start-with-Windows registration.

Uses the per-user Run key rather than a Startup-folder shortcut: it needs no
admin rights, is a single value to add or remove, and is what the Settings app
shows under Startup so the user can turn it off outside this program.
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "LyricOverlay"


def _launch_command() -> str:
    """The command Windows should run at sign-in.

    Points at `launch.py` rather than using `-m app.main`, because the Run key
    sets no working directory and `-m` would fail to resolve the package.
    Prefers `pythonw.exe` so no console window flashes up at sign-in. Both paths
    are quoted, since the project can live under a path containing spaces.
    """
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    if windowless.exists():
        executable = windowless

    entry = Path(__file__).resolve().parent.parent / "launch.py"
    return f'"{executable}" "{entry}"'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    """Add or remove the Run entry. Returns True if the state now matches."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False
