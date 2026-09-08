"""Create Desktop and Start Menu shortcuts so the overlay launches like an app.

Run once: `python install.py`. Re-running is safe; it overwrites the shortcuts.
Pass --uninstall to remove them (and the start-with-Windows entry).

Shortcuts point at `pythonw.exe`, not `python.exe`, so double-clicking opens the
overlay with no console window behind it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
ENTRY = PROJECT / "launch.py"
ICON = PROJECT / "app.ico"
SHORTCUT_NAME = "Lyric Overlay.lnk"


def _pythonw() -> Path:
    """The windowless interpreter, falling back to the current one."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def _write_icon() -> bool:
    """Render the app icon to app.ico. Needs a QApplication for QPainter."""
    try:
        from PyQt6.QtWidgets import QApplication

        from app.icon import write_ico
    except ImportError as exc:
        print(f"  ! cannot render icon ({exc})")
        return False

    # QPixmap requires a QApplication to exist, even when nothing is shown.
    owned = QApplication.instance() is None
    app = QApplication([]) if owned else QApplication.instance()
    try:
        ok = write_ico(ICON)
    finally:
        if owned:
            app.quit()

    print(f"  {'created' if ok else 'skipped'} {ICON.name}"
          + ("" if ok else " (install Pillow for a custom icon)"))
    return ok


def _shell_folder(name: str, fallback: Path) -> Path:
    """Resolve a Windows shell folder from the registry.

    Necessary because `~/Desktop` is often wrong: with OneDrive's Known Folder
    Move enabled the real Desktop lives under `~/OneDrive/Desktop`, and only the
    shell folder entry says so.
    """
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, name)
        resolved = Path(value)
        if resolved.is_dir():
            return resolved
    except (ImportError, OSError):
        pass
    return fallback


def _shortcut_targets() -> list[Path]:
    desktop = _shell_folder("Desktop", Path.home() / "Desktop")
    start_menu = _shell_folder(
        "Programs",
        Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows"
        / "Start Menu" / "Programs",
    )
    return [d / SHORTCUT_NAME for d in (desktop, start_menu) if d.is_dir()]


def _create_shortcut(path: Path, icon_ok: bool) -> bool:
    """Create a .lnk via WScript.Shell, driven from PowerShell.

    PowerShell is used rather than a COM binding so the script has no
    dependency beyond the standard library.
    """
    icon_line = (
        f'$s.IconLocation = "{ICON}"' if icon_ok and ICON.exists() else "# no custom icon"
    )
    script = f"""
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut("{path}")
$s.TargetPath = "{_pythonw()}"
$s.Arguments = '"{ENTRY}"'
$s.WorkingDirectory = "{PROJECT}"
$s.Description = "Transparent Spotify lyrics overlay"
{icon_line}
$s.Save()
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ! failed to create {path.parent.name} shortcut: {result.stderr.strip()}")
        return False
    print(f"  created {path}")
    return True


def install() -> int:
    print("Installing Lyric Overlay shortcuts...")
    icon_ok = _write_icon()

    targets = _shortcut_targets()
    if not targets:
        print("  ! found neither Desktop nor Start Menu")
        return 1

    created = sum(_create_shortcut(path, icon_ok) for path in targets)
    if not created:
        return 1

    print(
        "\nDone. Launch it from the Desktop or Start Menu.\n"
        "It runs in the notification area — click the tray icon to show or hide\n"
        "the overlay, right-click it for Settings and Quit."
    )
    return 0


def uninstall() -> int:
    print("Removing Lyric Overlay shortcuts...")
    for path in _shortcut_targets():
        if path.exists():
            try:
                path.unlink()
                print(f"  removed {path}")
            except OSError as exc:
                print(f"  ! could not remove {path}: {exc}")

    try:
        from app import startup

        startup.set_enabled(False)
        print("  removed start-with-Windows entry")
    except ImportError:
        pass

    print("\nDone. The app itself is untouched; delete this folder to remove it.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uninstall", action="store_true", help="remove the shortcuts")
    args = parser.parse_args()

    sys.path.insert(0, str(PROJECT))
    raise SystemExit(uninstall() if args.uninstall else install())
