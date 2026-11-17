"""Design tokens — the "Lab" palette carried over from the Craftimon Studio design system.

Rules inherited from Craftimon (Menu Strip & Theme System, Context Bar audit):
* One main colour + three accents (teal, amber, violet), each solved for WCAG 2.2 AA on its surface.
* The label colour on an accent is *computed* (`on_color`), never assumed: the main blue takes a dark
  label, amber a white one in light mode, etc.
* Semantic colours (ok / warning / danger) sit outside the themeable four and never change with theme.
* Colour is never the only signal: every state also has text, an icon or a pattern.

Qt-free: plain data + contrast maths, so tests can check every pairing.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def on_color(bg: str, dark: str = "#111111", light: str = "#FFFFFF") -> str:
    """Label colour for text placed on `bg`: whichever of dark/white contrasts more."""
    return dark if contrast(bg, dark) >= contrast(bg, light) else light


@dataclass(frozen=True)
class Tokens:
    name: str
    bg: str  # window background
    s1: str  # chrome, panels
    s2: str  # raised controls, menus
    s3: str  # inputs
    tx: str  # primary text
    t2: str  # secondary text
    t3: str  # tertiary text / captions
    bd: str  # hairlines
    bdd: str  # control borders
    acc: str  # main
    acc_tx: str  # main as text/link on surface
    acc_dim: str  # selected / pressed fill
    teal: str  # accent 1 (also: SIMULATED state)
    amber: str  # accent 2
    violet: str  # accent 3
    ok: str  # semantic — not themeable
    warn: str
    danger: str
    unknown: str
    stage: str  # image viewport surround (always dark: fluorescence is judged on black)
    focus: str  # focus ring
    hi: str = "#FFFFFF"  # soft-UI light edge (top-left of raised surfaces, bottom-right of inset ones)
    sh: str = "#000000"  # soft-UI shade edge (bottom-right of raised surfaces, top-left of inset ones)

    @property
    def acc_on(self) -> str:
        return on_color(self.acc)

    def on(self, color: str) -> str:
        return on_color(color)


# Depth-aware minimalism (2026-11-18, the operator's direction: ~70 % flat, 15 % material elevation, 10 % skeuomorphic
# cues, 5 % neumorphic detail). Solid surfaces; panels sit one tone above the window (tonal elevation, s1 over bg);
# controls are flat fills with a 3:1 border (bdd); fields are a touch sunken (s3); hi / sh only for fine detail.
DARK = Tokens(
    name="dark", bg="#14171C", s1="#1C2027", s2="#242A33", s3="#15181D",
    tx="#F3F5F7", t2="#B4BBC6", t3="#8F98A8", bd="#2C333E", bdd="#65738E",
    acc="#6293FF", acc_tx="#98BAFF", acc_dim="#25314A",
    teal="#52D1D0", amber="#EFAB57", violet="#E698F7",
    ok="#4CC38A", warn="#EFAB57", danger="#FF6B6B", unknown="#8A93A3",
    stage="#07090C", focus="#6293FF", hi="#2C333E", sh="#0F1216",
)

LIGHT = Tokens(
    name="light", bg="#ECEFF3", s1="#FFFFFF", s2="#F6F8FA", s3="#F2F4F7",
    tx="#14161A", t2="#434B53", t3="#566070", bd="#D0D7E0", bdd="#717B88",
    acc="#3360C8", acc_tx="#294DA3", acc_dim="#DCE6FB",
    teal="#005F5F", amber="#825100", violet="#772F87",
    ok="#1A6E44", warn="#8A4B00", danger="#B42318", unknown="#5F6877",
    stage="#07090C", focus="#3360C8", hi="#FFFFFF", sh="#B7C0CC",
)

THEMES = {"dark": DARK, "light": LIGHT}

# Typography (px) — Inter with tabular numerals; JetBrains Mono for values, logs and coordinates.
FONT_UI = "Inter"
FONT_MONO = "JetBrains Mono"
# caption and section floor 12 px (Impeccable audit 2026-11-18: 11 px was small at lab-monitor distance)
TYPE = {"body": 13, "label": 12, "caption": 12, "section": 12, "title": 15, "brand": 18, "value": 13}
SPACE = {"xs": 4, "s": 6, "m": 8, "l": 12, "xl": 16, "xxl": 24}
RADIUS = {"control": 8, "button": 8, "panel": 12, "menu": 10, "pill": 99}
ROW_H = 30  # inspector row height (label | control | reset)
LABEL_W = 84  # inspector label column

# State vocabulary: (token attribute, text). Text is mandatory — colour never stands alone.
STATES = {
    "ok": ("ok", "OK"), "simulated": ("teal", "SIMULATED"), "busy": ("acc", "BUSY"),
    "warn": ("warn", "WARNING"), "fault": ("danger", "FAULT"), "unknown": ("unknown", "UNKNOWN"),
    "unavailable": ("unknown", "UNAVAILABLE"), "locked": ("warn", "LOCKED"),
}


def state_color(t: Tokens, state: str) -> str:
    return getattr(t, STATES.get(state, ("unknown", ""))[0])


def as_dict(t: Tokens) -> dict:
    d = {f.name: getattr(t, f.name) for f in fields(t)}
    d["acc_on"] = t.acc_on
    return d


def with_accent(t: Tokens, acc: str, acc_tx: str | None = None) -> Tokens:
    """Theme presets swap the main colour only; semantics stay fixed."""
    return replace(t, acc=acc, acc_tx=acc_tx or acc, focus=acc)
