# ADR-0063: Theme engine (presets, custom colours, high contrast, theme files)

- **Status:** Accepted (2026-11-24). Implemented and tested offscreen on the simulator.
- **Asked for by:** the operator, 2026-11-24: "make a theme patch, similar to the theme engine we have for craftimon".
- **Builds on:**
  - `ui/tokens.py`, the "Lab" palette and rules carried over from the Craftimon Studio design system (Menu Strip &
    Theme System): one main colour and three accents, each solved for WCAG 2.2 AA on its surface; the label on an
    accent computed, never assumed; semantic colours outside the themeable four; colour never the only signal.
- **Code:**
  - `ui/theme_engine.py` (new, Qt-free);
  - `ui/theme.py` (`resolve`, `apply_settings`, swatch styles);
  - `ui/widgets/theme_picker.py` (new);
  - `ui/pages/instrument.py` (Settings › System — Basic › Colours);
  - `ui/shell.py` (View › Theme);
  - `ui/app.py` (start-up);
  - `instrument/settings.py` (`theme_preset`, `theme_custom`, `high_contrast`);
  - `ui/widgets/focus_pad.py` (the safe-height icon is keyed by the whole token set).
- **Tests:** `tests/test_theme_engine.py`: 38 tests, including 2,000+ generated themes.

## Context
OpenEVOS had two hand-tuned palettes (dark and light) and a `with_accent` helper that nothing used. The Craftimon
rules were written down in `tokens.py` but checked only against those two palettes. A theme engine has to keep the
rules for **any** colour a user picks, without a designer re-tuning each one.

## Decision
1. **A theme is a small spec** (`ThemeSpec`, JSON file format `openevos-theme/1`):
   - a main colour;
   - optionally the three accents (teal, which is also the SIMULATED state, amber, violet);
   - optionally a tint for the neutral surfaces (hue, and chroma scaled from 0 = pure greys to 2).

   Anything left out comes from the Lab palette for the mode. Unknown keys are refused.
2. **The engine solves, it doesn't trust.** `build(spec, mode, high_contrast)` starts from the Lab tokens for the
   mode. It moves each colour's lightness in OKLCH, keeping its hue, until the colour meets its job:

   | Role | Target (standard / high contrast) |
   |---|---|
   | main as a fill (primary button, checks, slider) | ≥ 3:1 / 4.5:1 against window, panels, menus, **and** its computed label ≥ 4.5:1 |
   | main as text (links, selected nav, checked tabs) | ≥ 4.5:1 / 7:1 on window, panels, menus and the selected fill |
   | accents as text | ≥ 4.5:1 / 7:1 on window, panels, menus |
   | text, secondary text, captions | ≥ 4.5:1 / 7:1 |
   | control borders (WCAG 1.4.11) | ≥ 3:1 / 4.5:1 |

   - A colour that already passes is kept exactly. So the Lab preset **is** the hand-tuned palette, byte for byte
     (tested).
   - Chroma is reduced only to stay inside sRGB.
   - Every change is reported in plain words ("Main colour lightened to #498FFF (4.5:1 against the panels and a
     readable label on it)"), and `audit()` lists all 39 pairings with their ratios.
3. **Semantics stay fixed.** ok, warning, danger, unknown and the image surround keep their values across presets
   (tested). Only high contrast, or a tint that would make them fail, moves their lightness; hue is kept, and the
   change is reported.
4. **Lint.** A main colour within 22° of hue of danger, warning, SIMULATED teal or OK gets a warning: states would be
   harder to tell apart. It still works, because every state also carries text.
5. **Presets:**
   - Lab (default, unchanged);
   - Iris (violet main, sky accent in place of violet);
   - Cobalt (saturated blue, bluer surfaces);
   - Graphite (slate main, pure grey surfaces).

   Warm or green mains are left to custom themes because of the lint above.
6. **Where the operator meets it:**
   - Settings › System — Basic › Colours: swatches (a check mark as well as a ring marks the chosen one); Custom
     colour… (colour dialog, one main colour); Import… / Export… (theme files); High contrast; a one-line contrast
     summary.
   - View › Theme: the same presets, Custom colours (explains when there is none), High contrast; all in the
     command palette.
   - Settings save `theme_preset`, `theme_custom` (the spec) and `high_contrast`. Settings files from before this
     ADR load unchanged (defaults: Lab, off).
   - A damaged saved custom theme never stops the app: it falls back to Lab and says why on stderr and in the log.
7. **Materials stay materials.** The jog and focus pads' brushed silver (light) and gunmetal (dark) are chosen by
   mode, not by theme, and `Tokens.name` stays the mode for that reason. Widgets that cache by theme key on the whole
   token set.

## Example theme file
```json
{
  "format": "openevos-theme/1",
  "id": "night-lab",
  "name": "Night lab",
  "main": "#8B7BF7",
  "neutral_hue": 270,
  "neutral_chroma": 0.5
}
```

## Consequences
- Any colour picked gives a readable interface in both modes and at both contrast levels: 2,000+ generated themes
  (random colours and tints, 2.7 ms per build) all pass in the tests.
- A theme can't make the image look different: the viewport surround is fixed, and display mapping is untouched.
- Not covered: per-theme radius, density or typography (the Craftimon spec, as written in `tokens.py`, themes colour
  only); per-accent pickers in the UI (theme files can set them).
