"""Theme engine (ADR-0063): presets, custom colours, high contrast and theme files keep the Craftimon rules.

Qt-free part first (every preset, random colours, files, settings); then the app offscreen (View menu, Settings ›
Interface picker, repaint).
"""

from __future__ import annotations

import json
import os
import random
import time

import pytest

from openevos.instrument.settings import AppSettings
from openevos.ui import theme_engine as te
from openevos.ui import tokens as tk

MODES = ("dark", "light")


# ------------------------------------------------------------------------------------------------ engine
@pytest.mark.parametrize("mode", MODES)
def test_lab_is_the_hand_tuned_palette_unchanged(mode):
    built = te.build(te.PRESETS["lab"], mode)
    assert built.tokens == tk.THEMES[mode]
    assert built.passes and not built.adjusted and not built.warnings


@pytest.mark.parametrize("hc", [False, True], ids=["standard", "high-contrast"])
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("pid", list(te.PRESETS))
def test_every_preset_meets_its_targets(pid, mode, hc):
    built = te.build(te.PRESETS[pid], mode, high_contrast=hc)
    failing = [(c.role, c.surface, round(c.ratio, 2), c.target) for c in built.checks if not c.ok]
    assert not failing
    assert built.tokens.name == mode  # widgets pick their material (silver or gunmetal) by mode
    targets = {c.target for c in built.checks if c.role == "t3"}
    assert targets == {7.0 if hc else 4.5}


@pytest.mark.parametrize("pid", list(te.PRESETS))
def test_semantic_colours_do_not_follow_presets(pid):
    for mode in MODES:
        t, base = te.build(te.PRESETS[pid], mode).tokens, tk.THEMES[mode]
        assert (t.ok, t.warn, t.danger, t.unknown, t.stage) == (base.ok, base.warn, base.danger, base.unknown,
                                                                 base.stage)


def test_any_picked_colour_gives_a_readable_theme():
    rng = random.Random(7)
    colours = ["#FFFF00", "#FFFFFF", "#000000", "#808080", "#FF0000", "#00FF00", "#0000FF", "#00FFFF", "#FF00FF"]
    colours += [f"#{rng.randrange(1 << 24):06X}" for _ in range(120)]
    started = time.perf_counter()
    for c in colours:
        for mode in MODES:
            for hc in (False, True):
                built = te.build(te.custom_from_color(c), mode, high_contrast=hc)
                assert built.passes, (c, mode, hc, [x for x in built.checks if not x.ok])
                assert te.hue_distance(built.tokens.acc, c) < 3 or te.to_oklch(c)[1] < 0.03  # hue kept
    assert (time.perf_counter() - started) / (len(colours) * 4) < 0.05  # fast enough to rebuild on every change


def test_tinted_surfaces_stay_readable():
    rng = random.Random(11)
    for _ in range(60):
        spec = te.ThemeSpec(main=f"#{rng.randrange(1 << 24):06X}", neutral_hue=rng.uniform(0, 359.9),
                            neutral_chroma=rng.uniform(0, 2))
        for mode in MODES:
            for hc in (False, True):
                assert te.build(spec, mode, high_contrast=hc).passes


def test_a_colour_that_passes_is_kept_exactly_and_one_that_fails_is_reported():
    ok = te.build(te.custom_from_color("#2F7DF6"), "dark")
    assert ok.tokens.acc == "#2F7DF6" and not ok.adjusted
    dark_blue = te.build(te.custom_from_color("#1A237E"), "dark")
    assert dark_blue.tokens.acc != "#1A237E" and any("Main colour lightened" in a for a in dark_blue.adjusted)
    assert tk.contrast(dark_blue.tokens.acc_on, dark_blue.tokens.acc) >= 4.5


def test_lint_warns_when_the_main_colour_looks_like_a_state():
    built = te.build(te.custom_from_color("#FF5A5A"), "dark")
    assert any("danger" in w for w in built.warnings)
    assert not te.build(te.PRESETS["iris"], "dark").warnings


def test_oklch_round_trip():
    for h in ("#6293FF", "#14171C", "#EFAB57", "#FFFFFF", "#000000", "#00FF00"):
        assert te.from_oklch(*te.to_oklch(h)) == h


# ------------------------------------------------------------------------------------------------ files and settings
def test_theme_file_round_trip(tmp_path):
    spec = te.ThemeSpec(id="night-lab", name="Night lab", main="#8b7bf7", neutral_hue=270, neutral_chroma=0.5)
    p = te.save_spec(spec, tmp_path / "night.json")
    data = json.loads(p.read_text())
    assert data["format"] == te.FORMAT and data["main"] == "#8B7BF7" and "teal" not in data
    assert te.load_spec(p) == spec


@pytest.mark.parametrize("content, reason", [
    ("not json", "cannot read"),
    ('{"main": "#112233"}', "not an OpenEVOS theme"),
    ('{"format": "openevos-theme/1", "main": "blue"}', "#RRGGBB"),
    ('{"format": "openevos-theme/1", "script": "x"}', "Extra inputs"),
    ('{"format": "openevos-theme/1", "neutral_chroma": 9}', "less than or equal"),
])
def test_bad_theme_files_are_refused_with_a_reason(tmp_path, content, reason):
    p = tmp_path / "t.json"
    p.write_text(content)
    with pytest.raises(ValueError, match=reason):
        te.load_spec(p)


def test_settings_keep_the_theme_and_old_files_still_load(tmp_path):
    s = AppSettings()
    s.basic.theme_preset = "custom"
    s.basic.theme_custom = te.custom_from_color("#8B7BF7").model_dump(exclude_none=True)
    s.basic.high_contrast = True
    p = tmp_path / "ui_settings.json"
    s.save(p)
    again = AppSettings.load(p)
    assert again.basic.theme_custom["main"] == "#8B7BF7" and again.basic.high_contrast
    old = json.loads(p.read_text())
    for k in ("theme_preset", "theme_custom", "high_contrast"):
        old["basic"].pop(k)
    p.write_text(json.dumps(old))
    assert AppSettings.load(p).basic.theme_preset == "lab"  # settings saved before ADR-0063


def test_a_damaged_saved_theme_falls_back_to_lab():
    spec, note = te.spec_from_settings("custom", {"main": "not a colour"})
    assert spec.id == "lab" and "using Lab" in note
    spec, note = te.spec_from_settings("ocean-deep", None)
    assert spec.id == "lab" and "unknown theme" in note


# ------------------------------------------------------------------------------------------------ the app
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from openevos.ui import theme

    a = QApplication.instance() or QApplication([])
    a.setStyleSheet(theme.QSS)
    yield a
    theme.apply_theme("dark", a)


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from openevos.acquisition.session import build_sim_session
    from openevos.core.clock import ThreadClock
    from openevos.ui import theme
    from openevos.ui.session_model import SessionModel
    from openevos.ui.shell import MainWindow

    s = build_sim_session(ThreadClock(), fast=True, fps=30, settings=AppSettings())
    w = MainWindow(SessionModel(s))
    w.resize(1400, 900)
    w.show()
    app.processEvents()
    yield w
    w.close()
    s.shutdown()
    w.deleteLater()
    app.processEvents()
    theme.apply_theme("dark", app)
    theme.LAST_BUILD = None


def test_view_menu_switches_colour_theme_and_high_contrast(app, window):
    from openevos.ui import theme

    for pid in te.PRESETS:
        assert f"view.theme.preset.{pid}" in window.commands
    window.run_command("view.theme.preset.iris")
    assert window.session.settings.basic.theme_preset == "iris"
    assert theme.ACCENT == te.build(te.PRESETS["iris"], "dark").tokens.acc
    assert "QPushButton#primary" in app.styleSheet() and theme.ACCENT in app.styleSheet()
    window.run_command("view.theme.high_contrast")
    assert window.session.settings.basic.high_contrast and theme.LAST_BUILD.high_contrast
    assert tk.contrast(theme.MUTED, theme.PANEL) >= 7.0
    window.run_command("view.theme.light")
    assert theme.current().name == "light" and theme.LAST_BUILD.passes
    # no custom theme yet: the command explains instead of switching
    window.run_command("view.theme.preset.custom")
    assert window.session.settings.basic.theme_preset == "iris"


def test_settings_picker_custom_colour_import_and_export(app, window, tmp_path):
    from openevos.ui import theme

    window.navigate("settings")
    picker = window.pages["settings"].theme_picker
    assert set(picker.swatches) == {*te.PRESETS, "custom"} and not picker.swatches["custom"].isEnabled()

    picker.pick_colour("#1A237E")  # a dark navy: too dark for dark mode, the engine lightens it
    b = window.session.settings.basic
    assert b.theme_preset == "custom" and b.theme_custom["main"] == "#1A237E"
    assert theme.ACCENT != "#1A237E" and "lightened" in picker.summary.text()
    assert picker.swatches["custom"].isChecked() and picker.swatches["custom"].isEnabled()
    assert window.commands["view.theme.preset.custom"].action.isChecked()

    out = picker.export_file(str(tmp_path / "navy.json"))
    assert out and te.load_spec(out).main == "#1A237E"

    picker.swatches["cobalt"].click()
    assert b.theme_preset == "cobalt" and picker.swatches["cobalt"].isChecked()

    bad = tmp_path / "bad.json"
    bad.write_text('{"format": "openevos-theme/1", "main": "red"}')
    assert not picker.import_file(str(bad)) and "Not imported" in picker.summary.text()
    assert b.theme_preset == "cobalt"  # unchanged
    assert picker.import_file(str(out)) and b.theme_preset == "custom"

    picker.chk_hc.setChecked(True)
    assert b.high_contrast and "AAA" in picker.summary.text()


def test_every_preset_paints_the_window(app, window):
    from openevos.ui import theme

    seen = set()
    for pid in te.PRESETS:
        for mode in MODES:
            window.session.settings.basic.theme = mode
            window.set_theme_preset(pid)
            app.processEvents()
            img = window.grab().toImage()
            assert img.width() > 0
            seen.add(theme.ACCENT)
    assert len(seen) >= len(te.PRESETS)  # each preset reaches the screen
