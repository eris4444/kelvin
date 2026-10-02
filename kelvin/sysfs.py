"""Small, exception-free readers for sysfs/procfs attributes."""

from pathlib import Path


def read(path: Path | str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def read_int(path: Path | str, base: int = 10) -> int | None:
    value = read(path)
    if value is None:
        return None
    try:
        return int(value, base)
    except ValueError:
        return None


def link_name(path: Path | str) -> str | None:
    """Basename of a symlink target (e.g. a device's bound driver)."""
    try:
        return Path(path).resolve(strict=True).name
    except OSError:
        return None
