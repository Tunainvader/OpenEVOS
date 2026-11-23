"""Settings › Interface colour theme picker (ADR-0063): preset swatches, a custom colour, high contrast, theme files.

The picker changes settings through the main window (`set_theme_preset`, `set_custom_theme`, `set_high_contrast`) so
the View menu, the command palette and this page always agree; without a window (tests, a bare page) it applies the
theme itself.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QFileDialog,
    QHBoxLayout,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openevos.ui import theme
from openevos.ui import theme_engine as te
from openevos.ui.common import label

SWATCH = 22


def swatch_icon(fill: str, ring: str, selected: bool) -> QIcon:
    dpr = 2.0
    pm = QPixmap(round(SWATCH * dpr), round(SWATCH * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r = QRectF(3, 3, SWATCH - 6, SWATCH - 6)
    p.setPen(QPen(QColor(ring), 2 if selected else 1))
    p.setBrush(QColor(fill))
    p.drawEllipse(r)
    if selected:  # a mark as well as the ring: colour is never the only signal
        p.setPen(QPen(QColor(theme.on(fill)), 2))
        c = r.center()
        p.drawLine(QPointF(c.x() - 4, c.y()), QPointF(c.x() - 1, c.y() + 3))
        p.drawLine(QPointF(c.x() - 1, c.y() + 3), QPointF(c.x() + 4, c.y() - 3))
    p.end()
    return QIcon(pm)


class ThemePicker(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.swatches: dict[str, QToolButton] = {}
        for pid, spec in [*te.PRESETS.items(), ("custom", None)]:
            b = QToolButton()
            b.setObjectName("swatch")
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setIconSize(QSize(SWATCH, SWATCH))
            b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            b.setText(spec.name if spec else "Custom")
            b.setAccessibleName(f"{b.text()} colour theme")
            b.clicked.connect(lambda _c=False, p=pid: self._choose(p))
            self.group.addButton(b)
            self.swatches[pid] = b
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        acts = QHBoxLayout()
        self.btn_custom = QPushButton("Custom colour…")
        self.btn_custom.setToolTip("Pick a main colour: OpenEVOS adjusts its lightness per mode so text and controls "
                                   "stay readable (WCAG 2.2 AA, or 7:1 with high contrast)")
        self.btn_custom.clicked.connect(self.pick_colour)
        self.btn_import = QPushButton("Import…")
        self.btn_import.setToolTip("Load an OpenEVOS theme file (.json) as the custom theme")
        self.btn_import.clicked.connect(lambda: self.import_file())
        self.btn_export = QPushButton("Export…")
        self.btn_export.setToolTip("Save the current colour theme as a file to share or keep")
        self.btn_export.clicked.connect(lambda: self.export_file())
        for b in (self.btn_custom, self.btn_import, self.btn_export):
            acts.addWidget(b)
        acts.addStretch(1)
        lay.addLayout(acts)

        self.chk_hc = QCheckBox("High contrast — text 7:1, control borders 4.5:1")
        self.chk_hc.toggled.connect(self._hc)
        lay.addWidget(self.chk_hc)
        self.summary = label("", "caption", wrap=True)
        lay.addWidget(self.summary)
        self.refresh()

    # ------------------------------------------------------------------ state
    @property
    def basic(self):
        return self.session.settings.basic

    def refresh(self) -> None:
        """Show what the settings say (also after a change from the View menu)."""
        mode = theme.current().name
        cur = self.basic.theme_preset
        for pid, b in self.swatches.items():
            if pid == "custom":
                spec, _note = te.spec_from_settings("custom", self.basic.theme_custom)
                b.setEnabled(bool(self.basic.theme_custom))
                b.setToolTip("Your custom theme" if self.basic.theme_custom else "No custom theme yet: use Custom "
                             "colour… or Import…")
            else:
                spec = te.PRESETS[pid]
                b.setToolTip(f"{spec.name} colours")
            selected = pid == cur
            b.blockSignals(True)
            b.setChecked(selected)
            b.blockSignals(False)
            b.setIcon(swatch_icon(te.swatch(spec, mode), theme.current().tx if selected else theme.BORDER_STRONG,
                                  selected))
        self.chk_hc.blockSignals(True)
        self.chk_hc.setChecked(self.basic.high_contrast)
        self.chk_hc.blockSignals(False)
        built = theme.LAST_BUILD or theme.resolve(self.basic, QApplication.instance())
        self.summary.setText(built.summary())

    # ------------------------------------------------------------------ actions
    def _win(self, name: str):
        return getattr(self.window(), name, None)

    def _apply(self) -> None:
        theme.apply_settings(self.basic, QApplication.instance())
        self.refresh()

    def _choose(self, preset: str) -> None:
        f = self._win("set_theme_preset")
        if f:
            f(preset)
        elif preset != "custom" or self.basic.theme_custom:
            self.basic.theme_preset = preset
            self._apply()
        self.refresh()

    def _hc(self, on: bool) -> None:
        f = self._win("set_high_contrast")
        if f:
            f(on)
        else:
            self.basic.high_contrast = on
            self._apply()
        self.refresh()

    def use_spec(self, spec: te.ThemeSpec) -> None:
        f = self._win("set_custom_theme")
        if f:
            f(spec)
        else:
            self.basic.theme_custom = spec.model_dump(exclude_none=True)
            self.basic.theme_preset = "custom"
            self._apply()
        self.refresh()

    def pick_colour(self, color: str | None = None) -> None:
        if color is None:
            start = theme.ACCENT
            spec, _ = te.spec_from_settings(self.basic.theme_preset, self.basic.theme_custom)
            start = spec.main or start
            c = QColorDialog.getColor(QColor(start), self, "Main colour")
            if not c.isValid():
                return
            color = c.name().upper()
        self.use_spec(te.custom_from_color(color))

    def import_file(self, path: str | None = None) -> bool:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Import theme", "", "OpenEVOS theme (*.json)")
            if not path:
                return False
        try:
            spec = te.load_spec(Path(path))
        except ValueError as exc:
            self.summary.setText(f"Not imported: {exc}")
            return False
        self.use_spec(spec.model_copy(update={"id": "custom"}))
        return True

    def export_file(self, path: str | None = None) -> Path | None:
        spec, _ = te.spec_from_settings(self.basic.theme_preset, self.basic.theme_custom)
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Export theme", f"{spec.id}.json", "OpenEVOS theme (*.json)")
            if not path:
                return None
        try:
            out = te.save_spec(spec, Path(path))
        except OSError as exc:
            self.summary.setText(f"Not exported: {exc}")
            return None
        self.summary.setText(f"Exported {spec.name} to {out.name}.")
        return out
