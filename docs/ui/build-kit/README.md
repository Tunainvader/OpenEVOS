# OpenEVOS UI build kit

A self-contained handoff for an AI or developer who will build or extend the OpenEVOS desktop UI. It
needs no network access.

## Files

| Path | What it is |
|---|---|
| `BUILD_INSTRUCTIONS.md` | The specification: rules, architecture, design system, shell, every workspace, behaviour, data formats, build order, known pitfalls |
| `ACCEPTANCE.md` | Testable checklist, mapped to the reference test suite |
| `design-tokens.json` | Colours (dark/light), type, spacing, radius, states, measured contrast ratios |
| `theme-dark.qss`, `theme-light.qss` | Qt stylesheets generated from the tokens |
| `icons/` + `icon-sheet.png` | 44 stroke icons (24 px grid, 1.75 stroke, `currentColor`) |
| `fonts/` | Inter + JetBrains Mono (SIL OFL 1.1) |
| `screenshots/` | Every workspace (dark), plus light-theme and narrow-window variants |
| `annotated/` | Capture, Analysis › Measure, Scan, Calibration and narrow-window screenshots with numbered callouts and a legend |
| `specs/commands.json` | Every menu command: key, text, menu path, shortcut, toggle |
| `specs/callouts.json` | Widget geometry behind the annotated images |

## How to use it

1. Hand `BUILD_INSTRUCTIONS.md` and `ACCEPTANCE.md` to the implementing AI first. They reference the
   other files by relative path.
2. The AI follows the build order in §8 of `BUILD_INSTRUCTIONS.md` and compares its offscreen
   screenshots with `screenshots/` and `annotated/`.
3. Acceptance = every box in `ACCEPTANCE.md`.

## Regenerating

From the OpenEVOS repository:

```
QT_QPA_PLATFORM=offscreen python tools/make_ui_screenshots.py docs/ui/screenshots
python tools/make_build_kit.py docs/ui/build-kit
```

All images come from the simulator and are labelled SIMULATED.
