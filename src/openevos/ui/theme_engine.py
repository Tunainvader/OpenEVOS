"""Theme engine (ADR-0063): presets, custom main colours, high contrast and theme files, Craftimon rules.

A theme is a small spec (`ThemeSpec`): a main colour, optionally the three accents and a tint for the neutral
surfaces. The engine builds the full `Tokens` set for a mode (dark or light) from the hand-tuned base palette in
`tokens.py` and *solves* each colour for its job instead of trusting it:

* **main as a fill** (primary buttons, checked boxes, slider fill): >= 3:1 against the panels (WCAG 1.4.11), and the
  label on it (`on_color`, dark or white) >= 4.5:1;
* **main as text** (links, selected nav, checked tabs): >= 4.5:1 on window, panels, menus and the selected fill;
* **accents** (teal = SIMULATED, amber, violet): >= 4.5:1 as text on window, panels and menus;
* **neutrals**: text roles >= 4.5:1 (secondary and tertiary too), control borders >= 3:1;
* **high contrast**: text >= 7:1 and control borders >= 4.5:1.

Solving keeps hue and moves lightness in OKLCH (perceptual), reducing chroma only to stay inside sRGB. A colour that
already passes is left exactly as given, so the "Lab" preset is the hand-tuned palette, unchanged.

Semantic colours (ok, warning, danger, unknown) are outside the themeable four and keep their values. Only when a
target cannot be met (a tinted surface, high contrast) does their lightness move, and the result says so.

Qt-free, like tokens.py: tests check every preset and random colours.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openevos.ui import tokens as tk

FORMAT: Literal["openevos-theme/1"] = "openevos-theme/1"
HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
TEXT_AA, TEXT_AAA, UI_AA, UI_HC = 4.5, 7.0, 3.0, 4.5


# ---------------------------------------------------------------------------------------------------- colour maths
def _hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(min(1.0, max(0.0, c)) * 255):02X}" for c in rgb)


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _gam(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * (max(c, 0.0) ** (1 / 2.4)) - 0.055


def to_oklch(h: str) -> tuple[float, float, float]:
    """sRGB hex -> OKLCH (L 0..1, C, hue degrees). Björn Ottosson's OKLab."""
    r, g, b = (_lin(c) for c in _hex_to_rgb(h))
    lm = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    mm = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    sm = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (math.copysign(abs(v) ** (1 / 3), v) for v in (lm, mm, sm))
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    bb = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return L, math.hypot(a, bb), math.degrees(math.atan2(bb, a)) % 360


def _oklch_to_linear(L: float, C: float, H: float) -> tuple[float, float, float]:
    a, b = C * math.cos(math.radians(H)), C * math.sin(math.radians(H))
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    lm, mm, sm = l_ ** 3, m_ ** 3, s_ ** 3
    return (4.0767416621 * lm - 3.3077115913 * mm + 0.2309699292 * sm,
            -1.2684380046 * lm + 2.6097574011 * mm - 0.3413193965 * sm,
            -0.0041960863 * lm - 0.7034186147 * mm + 1.7076147010 * sm)


def from_oklch(L: float, C: float, H: float) -> str:
    """OKLCH -> sRGB hex, reducing chroma until the colour fits in sRGB (hue and lightness kept)."""
    L = min(1.0, max(0.0, L))

    def fits(c: float) -> bool:
        return all(-1e-4 <= v <= 1 + 1e-4 for v in _oklch_to_linear(L, c, H))

    if not fits(C):
        lo, hi = 0.0, C
        for _ in range(24):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if fits(mid) else (lo, mid)
        C = lo
    return _rgb_to_hex(tuple(_gam(v) for v in _oklch_to_linear(L, C, H)))  # type: ignore[arg-type]


def mix(a: str, b: str, t: float) -> str:
    """`t` of colour a over colour b, in OKLab."""
    La, Ca, Ha = to_oklch(a)
    Lb, Cb, Hb = to_oklch(b)
    xa, ya = Ca * math.cos(math.radians(Ha)), Ca * math.sin(math.radians(Ha))
    xb, yb = Cb * math.cos(math.radians(Hb)), Cb * math.sin(math.radians(Hb))
    L, x, y = La * t + Lb * (1 - t), xa * t + xb * (1 - t), ya * t + yb * (1 - t)
    return from_oklch(L, math.hypot(x, y), math.degrees(math.atan2(y, x)) % 360)


def min_contrast(color: str, against: list[str]) -> float:
    return min(tk.contrast(color, s) for s in against)


def solve(color: str, against: list[str], target: float, *, lighter: bool) -> str:
    """The colour nearest to `color` (same hue, lightness moved one way) with >= `target` contrast against every
    colour in `against`. Unchanged when it already passes; the extreme of that direction when nothing passes."""
    if min_contrast(color, against) >= target:
        return color
    L, C, H = to_oklch(color)
    end = 1.0 if lighter else 0.0
    best = from_oklch(end, C, H)
    if min_contrast(best, against) < target:
        return best  # cannot be met in this direction; caller reports it
    lo, hi = L, end  # lo fails, hi passes
    for _ in range(30):
        mid = (lo + hi) / 2
        c = from_oklch(mid, C, H)
        if min_contrast(c, against) >= target:
            hi, best = mid, c
        else:
            lo = mid
    return best


def solve_fill(color: str, panels: list[str], *, lighter: bool, ui_target: float) -> str:
    """A fill that stands out from the panels (>= ui_target) and carries a readable label (dark or white, >= 4.5)."""
    def ok(c: str) -> bool:
        return min_contrast(c, panels) >= ui_target and tk.contrast(c, tk.on_color(c)) >= TEXT_AA

    if ok(color):
        return color
    L, C, H = to_oklch(color)
    steps = [L + (1 - L) * i / 60 for i in range(1, 61)] if lighter else [L - L * i / 60 for i in range(1, 61)]
    for x in steps:
        c = from_oklch(x, C, H)
        if ok(c):
            return c
    return from_oklch(1.0 if lighter else 0.0, C, H)


def hue_distance(a: str, b: str) -> float:
    ha, hb = to_oklch(a)[2], to_oklch(b)[2]
    d = abs(ha - hb) % 360
    return min(d, 360 - d)


# ---------------------------------------------------------------------------------------------------- the spec
class ThemeSpec(BaseModel):
    """What a theme file holds. Missing colours fall back to the Lab palette for the mode."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["openevos-theme/1"] = FORMAT
    id: str = Field(default="custom", pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    name: str = Field(default="Custom", min_length=1, max_length=40)
    main: str | None = None  # None: the Lab blue, hand-tuned per mode
    teal: str | None = None
    amber: str | None = None
    violet: str | None = None
    #: tint the neutral surfaces toward this hue (degrees, OKLCH); None keeps the Lab surfaces
    neutral_hue: float | None = Field(default=None, ge=0, lt=360)
    #: scale the surfaces' colourfulness: 0 = pure greys, 1 = as the Lab surfaces, up to 2
    neutral_chroma: float = Field(default=1.0, ge=0, le=2)

    @field_validator("main", "teal", "amber", "violet")
    @classmethod
    def _hex(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not HEX.match(v):
            raise ValueError("colours are #RRGGBB")
        return v.upper()


PRESETS: dict[str, ThemeSpec] = {
    "lab": ThemeSpec(id="lab", name="Lab"),
    "iris": ThemeSpec(id="iris", name="Iris", main="#8B7BF7", violet="#5BB8F5"),
    "cobalt": ThemeSpec(id="cobalt", name="Cobalt", main="#2F7DF6", neutral_hue=255, neutral_chroma=1.4),
    "graphite": ThemeSpec(id="graphite", name="Graphite", main="#8C9BB4", neutral_chroma=0.0),
}


@dataclass(frozen=True)
class Check:
    role: str
    surface: str
    ratio: float
    target: float

    @property
    def ok(self) -> bool:
        return self.ratio >= self.target - 1e-9


@dataclass(frozen=True)
class Built:
    """A built theme: the tokens, what the engine had to change, what it warns about, and every contrast check."""

    tokens: tk.Tokens
    spec: ThemeSpec
    mode: str
    high_contrast: bool
    adjusted: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def passes(self) -> bool:
        return all(c.ok for c in self.checks)

    def summary(self) -> str:
        level = "AAA (7:1 text)" if self.high_contrast else "WCAG 2.2 AA"
        head = (f"All {len(self.checks)} colour pairs meet {level}." if self.passes else
                f"{sum(not c.ok for c in self.checks)} of {len(self.checks)} colour pairs miss {level}.")
        return " ".join([head, *self.adjusted, *self.warnings])


# ---------------------------------------------------------------------------------------------------- building
NEUTRALS = ("bg", "s1", "s2", "s3", "bd", "bdd", "hi", "sh", "tx", "t2", "t3")
TEXT_ROLES = ("tx", "t2", "t3")
SURFACES = ("bg", "s1", "s2")


def _tint(color: str, hue: float | None, scale: float) -> str:
    L, C, H = to_oklch(color)
    return from_oklch(L, C * scale, H if hue is None else hue)


def build(spec: ThemeSpec | None = None, mode: str = "dark", *, high_contrast: bool = False) -> Built:
    spec = spec or PRESETS["lab"]
    if mode not in tk.THEMES:
        raise ValueError(f"mode must be one of {sorted(tk.THEMES)}")
    base = tk.THEMES[mode]
    lighter = mode == "dark"  # on dark surfaces colours move up in lightness, on light ones down
    text_t, ui_t = (TEXT_AAA, UI_HC) if high_contrast else (TEXT_AA, UI_AA)
    t = base
    adjusted: list[str] = []
    warnings: list[str] = []

    def note(role: str, before: str, after: str, why: str) -> None:
        if before.upper() != after.upper():
            adjusted.append(f"{role} {'lightened' if lighter else 'darkened'} to {after} ({why}).")

    # 1. neutral surfaces: tint, then make the text and borders on them pass again
    if spec.neutral_hue is not None or spec.neutral_chroma != 1.0:
        t = replace(t, **{k: _tint(getattr(t, k), spec.neutral_hue, spec.neutral_chroma) for k in NEUTRALS})
    surf = [getattr(t, s) for s in SURFACES]
    for role in TEXT_ROLES:
        t = replace(t, **{role: solve(getattr(t, role), surf, text_t, lighter=lighter)})
    t = replace(t, bdd=solve(t.bdd, [t.bg, t.s1], ui_t, lighter=lighter))
    if high_contrast:
        adjusted.append("High contrast: text raised to 7:1 and control borders to 4.5:1.")

    # 2. main colour: a fill, a selected tint, and a text colour, each solved for its job
    if spec.main is not None:
        acc = solve_fill(spec.main, [t.bg, t.s1, t.s2], lighter=lighter, ui_target=ui_t)
        note("Main colour", spec.main, acc, f"{ui_t:g}:1 against the panels and a readable label on it")
        acc_dim = mix(acc, t.s1, 0.24 if lighter else 0.16)
        acc_tx = solve(spec.main, [t.bg, t.s1, t.s2, acc_dim], text_t, lighter=lighter)
        t = replace(t, acc=acc, acc_dim=acc_dim, acc_tx=acc_tx, focus=acc)
    else:
        acc_tx = solve(t.acc_tx, [t.bg, t.s1, t.s2, t.acc_dim], text_t, lighter=lighter)
        acc = solve_fill(t.acc, [t.bg, t.s1, t.s2], lighter=lighter, ui_target=ui_t)
        t = replace(t, acc=acc, acc_tx=acc_tx, focus=acc)

    # 3. accents: given or Lab, as text on the surfaces
    for role in ("teal", "amber", "violet"):
        given = getattr(spec, role) or getattr(t, role)
        new = solve(given, [t.bg, t.s1, t.s2], text_t, lighter=lighter)
        if getattr(spec, role):
            note(role.capitalize(), given, new, f"{text_t:g}:1 as text")
        t = replace(t, **{role: new})

    # 4. semantics keep their values unless a target cannot be met on these surfaces
    for role in ("ok", "warn", "danger", "unknown"):
        before = getattr(t, role)
        new = solve(before, [t.bg, t.s1, t.s2], text_t, lighter=lighter)
        if new != before and not high_contrast:
            adjusted.append(f"Semantic colour {role} {'lightened' if lighter else 'darkened'} to {new} on the tinted "
                            "surfaces (its hue is kept).")
        t = replace(t, **{role: new})

    # 5. lint: the main colour must not be mistaken for a state
    if spec.main is not None:
        for role, meaning in (("danger", "a fault"), ("warn", "a warning"), ("teal", "SIMULATED"), ("ok", "OK")):
            other = getattr(t, role)
            if hue_distance(t.acc, other) < 22 and to_oklch(t.acc)[1] > 0.06 and to_oklch(other)[1] > 0.06:
                warnings.append(f"The main colour is close in hue to {role} ({meaning}): states may be harder to tell "
                                "apart. Every state also carries text, but consider another hue.")

    # `name` stays the mode: widgets choose their material (brushed silver or gunmetal) by it
    return Built(t, spec, mode, high_contrast, adjusted, warnings, audit(t, high_contrast=high_contrast))


def audit(t: tk.Tokens, *, high_contrast: bool = False) -> list[Check]:
    """Every pairing the UI draws, with its WCAG target."""
    text_t, ui_t = (TEXT_AAA, UI_HC) if high_contrast else (TEXT_AA, UI_AA)
    out: list[Check] = []
    for role in ("tx", "t2", "t3", "acc_tx", "teal", "amber", "violet", "ok", "warn", "danger", "unknown"):
        for s in SURFACES:
            out.append(Check(role, s, tk.contrast(getattr(t, role), getattr(t, s)), text_t))
    out.append(Check("acc_tx", "acc_dim", tk.contrast(t.acc_tx, t.acc_dim), text_t))  # selected nav, checked buttons
    for s in ("bg", "s1"):
        out.append(Check("bdd", s, tk.contrast(t.bdd, getattr(t, s)), ui_t))
        out.append(Check("acc", s, tk.contrast(t.acc, getattr(t, s)), ui_t))
    out.append(Check("acc_on", "acc", tk.contrast(t.acc_on, t.acc), TEXT_AA))  # primary button label
    return out


# ---------------------------------------------------------------------------------------------------- files
def load_spec(path: Path) -> ThemeSpec:
    """Read a theme file. Raises ValueError with a readable reason."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {Path(path).name}: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError(f"{Path(path).name} is not an OpenEVOS theme (format {FORMAT!r})")
    try:
        return ThemeSpec.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise ValueError(f"{Path(path).name}: {exc}") from exc


def save_spec(spec: ThemeSpec, path: Path) -> Path:
    Path(path).write_text(json.dumps(spec.model_dump(exclude_none=True), indent=2) + "\n", encoding="utf-8")
    return Path(path)


def spec_from_settings(preset: str, custom: dict | None) -> tuple[ThemeSpec, str]:
    """The spec the saved settings name, and a note when they could not be used (falls back to Lab)."""
    if preset == "custom":
        try:
            return ThemeSpec.model_validate(custom or {}), ""
        except Exception as exc:  # noqa: BLE001 - a damaged saved theme must never stop the app
            return PRESETS["lab"], f"saved custom theme ignored ({str(exc).splitlines()[0]}); using Lab"
    if preset in PRESETS:
        return PRESETS[preset], ""
    return PRESETS["lab"], f"unknown theme {preset!r}; using Lab"


def custom_from_color(hex_color: str, name: str = "Custom") -> ThemeSpec:
    """A theme from one picked colour: the main colour; accents stay Lab's (already solved per mode)."""
    return ThemeSpec(id="custom", name=name, main=hex_color.upper())


def swatch(spec: ThemeSpec, mode: str) -> str:
    """The colour that represents a theme in a picker (its main fill for the mode)."""
    return build(spec, mode).tokens.acc
