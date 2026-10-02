"""Omarchy theme integration: palette from the current theme's colors.toml,
turned into CSS custom properties for Kelvin and libadwaita."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import paths

RGB = tuple[float, float, float]


def parse_hex(value: str) -> RGB | None:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        return None
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb: RGB) -> float:
    r, g, b = (_lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: RGB, b: RGB) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(a: RGB, b: RGB, t: float) -> RGB:
    return tuple(x + (y - x) * t for x, y in zip(a, b))  # type: ignore[return-value]


def ensure_contrast(color: RGB, background: RGB, minimum: float) -> RGB:
    """Nudge `color` toward white/black until it reads on `background`."""
    target = (1.0, 1.0, 1.0) if luminance(background) < 0.4 else (0.0, 0.0, 0.0)
    step, out = 0.0, color
    while contrast(out, background) < minimum and step < 1.0:
        step += 0.05
        out = mix(color, target, step)
    return out


def css(rgb: RGB, alpha: float = 1.0) -> str:
    r, g, b = (round(c * 255) for c in rgb)
    return f"rgba({r},{g},{b},{alpha:.3f})" if alpha < 1 else f"#{r:02x}{g:02x}{b:02x}"


@dataclass
class Palette:
    name: str
    background: RGB
    foreground: RGB
    accent: RGB
    light: bool

    @property
    def text(self) -> RGB:
        return ensure_contrast(self.foreground, self.background, 7.0)

    @property
    def accent_text(self) -> RGB:
        return ensure_contrast(self.accent, self.background, 4.5)

    @property
    def accent_fg(self) -> RGB:
        white, black = (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)
        return white if contrast(white, self.accent) >= contrast(black, self.accent) else black


def load_palette(theme_dir: Path | None = None) -> Palette | None:
    theme_dir = theme_dir or paths.omarchy_theme_dir()
    try:
        text = (theme_dir / "colors.toml").read_text()
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r'\s*([A-Za-z0-9_]+)\s*=\s*"([^"]+)"', line)
        if match:
            values[match.group(1)] = match.group(2)
    bg = parse_hex(values.get("background", ""))
    fg = parse_hex(values.get("foreground", ""))
    accent = parse_hex(values.get("accent", "")) or parse_hex(values.get("color4", ""))
    if not (bg and fg and accent):
        return None
    try:
        name = (theme_dir.parent / "theme.name").read_text().strip()
    except OSError:
        name = theme_dir.name
    light = (theme_dir / "light.mode").exists() or luminance(bg) > 0.5
    return Palette(name=name, background=bg, foreground=fg, accent=accent, light=light)


# Status colours tuned for legibility; theme terminal colours are not semantic.
STATUS_DARK = {"good": (0.30, 0.86, 0.56), "warn": (0.98, 0.74, 0.30), "bad": (1.0, 0.43, 0.43)}
STATUS_LIGHT = {"good": (0.10, 0.55, 0.30), "warn": (0.66, 0.42, 0.0), "bad": (0.78, 0.16, 0.16)}


def palette_css(p: Palette | None, translucent: bool, light: bool) -> str:
    """CSS custom properties. Without a palette, map onto libadwaita's own."""
    status = STATUS_LIGHT if light else STATUS_DARK
    lines = [":root {"]
    for key, rgb in status.items():
        lines.append(f"  --gs-{key}: {css(rgb)};")
        lines.append(f"  --gs-{key}-soft: {css(rgb, 0.14)};")
        lines.append(f"  --gs-{key}-line: {css(rgb, 0.32)};")

    if p is None:
        alpha = "92%" if translucent else "100%"
        lines += [
            f"  --gs-window-bg: color-mix(in srgb, var(--window-bg-color) {alpha}, transparent);",
            "  --gs-fg: var(--window-fg-color);",
            "  --gs-dim: color-mix(in srgb, var(--window-fg-color) 62%, transparent);",
            "  --gs-faint: color-mix(in srgb, var(--window-fg-color) 38%, transparent);",
            "  --gs-card: color-mix(in srgb, var(--window-fg-color) 5%, transparent);",
            "  --gs-tile: color-mix(in srgb, var(--window-fg-color) 6%, transparent);",
            "  --gs-border: color-mix(in srgb, var(--window-fg-color) 10%, transparent);",
            "  --gs-accent: var(--accent-bg-color);",
            "  --gs-accent-text: var(--accent-color);",
            "  --gs-accent-fg: var(--accent-fg-color);",
            "  --gs-accent-soft: color-mix(in srgb, var(--accent-bg-color) 16%, transparent);",
            "}",
        ]
        return "\n".join(lines)

    bg, text, accent = p.background, p.text, p.accent
    raised = mix(bg, text, 0.06)
    lines += [
        f"  --gs-window-bg: {css(bg, 0.93 if translucent else 1.0)};",
        f"  --gs-fg: {css(text)};",
        f"  --gs-dim: {css(text, 0.66)};",
        f"  --gs-faint: {css(text, 0.40)};",
        f"  --gs-card: {css(text, 0.045)};",
        f"  --gs-tile: {css(text, 0.055)};",
        f"  --gs-border: {css(text, 0.10)};",
        f"  --gs-accent: {css(accent)};",
        f"  --gs-accent-text: {css(p.accent_text)};",
        f"  --gs-accent-fg: {css(p.accent_fg)};",
        f"  --gs-accent-soft: {css(accent, 0.18)};",
        # libadwaita
        f"  --accent-bg-color: {css(accent)};",
        f"  --accent-fg-color: {css(p.accent_fg)};",
        f"  --accent-color: {css(p.accent_text)};",
        f"  --window-bg-color: {css(bg, 0.93 if translucent else 1.0)};",
        f"  --window-fg-color: {css(text)};",
        f"  --view-bg-color: {css(bg)};",
        f"  --view-fg-color: {css(text)};",
        "  --headerbar-bg-color: transparent;",
        f"  --headerbar-fg-color: {css(text)};",
        f"  --headerbar-backdrop-color: transparent;",
        "  --headerbar-shade-color: transparent;",
        f"  --card-bg-color: {css(text, 0.05)};",
        f"  --card-fg-color: {css(text)};",
        f"  --popover-bg-color: {css(raised)};",
        f"  --popover-fg-color: {css(text)};",
        f"  --dialog-bg-color: {css(raised)};",
        f"  --dialog-fg-color: {css(text)};",
        f"  --sidebar-bg-color: {css(raised)};",
        f"  --sidebar-fg-color: {css(text)};",
        "}",
    ]
    return "\n".join(lines)
