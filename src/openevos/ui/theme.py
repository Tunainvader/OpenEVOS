"""Qt theme built from `tokens.py` (Craftimon "Lab" palette): QSS, fonts, live theme switching.

Widgets read colours from this module at paint time (`theme.ACCENT`, `theme.state_color(...)`), so a
theme switch only has to call `apply_theme()` and repaint. Nothing outside tokens.py hard-codes a colour
except image data (pseudo colours come from the instrument profile).

Soft UI (neumorphic, 2026-11-17): the window and its panels are one moulded surface; depth comes from a light edge
on the lit side and a shade edge on the other (raised), or the reverse (inset). QSS cannot draw blurred double
shadows, and a QGraphicsEffect on the panels would re-render the live image off screen every frame, so the edges
carry the effect.
"""

from __future__ import annotations

from pathlib import Path

from openevos.ui import tokens as tk

_T: tk.Tokens = tk.DARK


def os_text_scale() -> float:
    """Windows Settings > Accessibility > Text size (100-225 %), so pixel-sized type grows with it; 1.0 elsewhere."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Accessibility") as k:
            return max(1.0, min(2.25, winreg.QueryValueEx(k, "TextScaleFactor")[0] / 100.0))
    except Exception:  # noqa: BLE001 - not Windows, or never set
        return 1.0


TEXT_SCALE = os_text_scale()


def px(role: str) -> int:
    """Type size for a role, scaled by the OS text-size setting."""
    return round(tk.TYPE[role] * TEXT_SCALE)

# Names kept for widgets (refreshed by apply_theme)
BG = CHROME = PANEL = PANEL_2 = INPUT = BORDER = BORDER_STRONG = TEXT = TEXT_2 = MUTED = ""
ACCENT = ACCENT_TX = ACCENT_DIM = ACCENT_ON = OK = SIM = WARN = FAULT = UNKNOWN = ORANGE = STAGE = ""
TEAL = AMBER = VIOLET = HI = SH = ""
STATE_COLORS: dict[str, str] = {}
QSS = ""


def current() -> tk.Tokens:
    return _T


def state_color(state: str) -> str:
    return tk.state_color(_T, state)


def on(color: str) -> str:
    return tk.on_color(color)


def _refresh_globals(t: tk.Tokens) -> None:
    g = globals()
    g.update(BG=t.bg, CHROME=t.s1, PANEL=t.s1, PANEL_2=t.s2, INPUT=t.s3, BORDER=t.bd, BORDER_STRONG=t.bdd,
             TEXT=t.tx, TEXT_2=t.t2, MUTED=t.t3, ACCENT=t.acc, ACCENT_TX=t.acc_tx, ACCENT_DIM=t.acc_dim,
             ACCENT_ON=t.acc_on, OK=t.ok, SIM=t.teal, WARN=t.warn, FAULT=t.danger, UNKNOWN=t.unknown,
             ORANGE=t.amber, STAGE=t.stage, TEAL=t.teal, AMBER=t.amber, VIOLET=t.violet, HI=t.hi, SH=t.sh)
    g["STATE_COLORS"] = {k: tk.state_color(t, k) for k in tk.STATES}
    g["QSS"] = build_qss(t)


def _edges(t: tk.Tokens, inset: bool = False, width: int = 1, control: bool = False) -> str:
    """Depth-aware minimalism (2026-11-18): borders and contrast identify interactive elements. A `control` (button,
    field, check box) gets one solid border in `bdd` (>= 3:1, WCAG 1.4.11); a field (`inset`) keeps a faint sunken
    top edge as its 5 % neumorphic detail; anything else (panels) is a flat hairline in `bd` — panels are lifted by
    tone (s1 over bg), not by edges."""
    if not control:
        return f"border: {width}px solid {t.bd};"
    if inset:
        return f"border: 1px solid {t.bdd}; border-top: 2px solid {t.bdd};"
    return f"border: 1px solid {t.bdd};"


def build_qss(t: tk.Tokens) -> str:
    f = tk.FONT_UI
    raised2 = _edges(t)  # panels: a hairline; tonal elevation does the lifting
    raised, inset = _edges(t, control=True), _edges(t, inset=True, control=True)
    r = tk.RADIUS
    return f"""
* {{ font-family: "{f}", "Segoe UI", "DejaVu Sans", sans-serif; font-size: {px('body')}px; color: {t.tx}; }}
QMainWindow, QWidget#root {{ background: {t.bg}; }}
QWidget {{ background: transparent; }}
QDialog {{ background: {t.s1}; }}
QFrame#panel {{ background: {t.s1}; {raised2} border-radius: {r['panel']}px; }}
QFrame#subpanel {{ background: {t.s3}; {_edges(t, inset=True)} border-radius: {r['control']}px; }}
QFrame#header {{ background: {t.s1}; border: none; border-bottom: 1px solid {t.bd}; }}
QFrame#rail {{ background: {t.s1}; border: none; border-right: 1px solid {t.bd}; }}
QFrame#footer {{ background: {t.s1}; border: none; border-top: 1px solid {t.bd}; }}
QFrame#viewport {{ background: {t.stage}; border: 1px solid {t.bd}; border-radius: {r['control']}px; }}
QLabel#title {{ font-size: {px('title')}px; font-weight: 600; }}
QLabel#brand {{ font-size: {px('brand')}px; font-weight: 700; }}
QLabel#section, QLabel#navgroup {{ color: {t.t3}; font-size: {px('section')}px; font-weight: 600; }}
QLabel#navgroup {{ padding: 12px 12px 3px 12px; }}
QLabel#value {{ font-size: {px('value')}px; font-weight: 500; }}
QLabel#muted, QLabel#caption {{ color: {t.t2}; font-size: {px('label')}px; }}
QLabel#rowlabel {{ color: {t.t2}; font-size: {px('label')}px; }}
QLabel#mono {{ font-family: "{tk.FONT_MONO}", "DejaVu Sans Mono", monospace; font-size: 12px; color: {t.t2}; }}
QLabel#warn {{ color: {t.warn}; font-size: {px('label')}px; }}
QLabel#fault {{ color: {t.danger}; font-size: {px('label')}px; }}
QLabel#ok {{ color: {t.ok}; font-size: {px('label')}px; }}
QPushButton, QToolButton {{ background: {t.s2}; {raised} border-radius: {r['button']}px;
    padding: 6px 14px; font-size: 12px; font-weight: 500; min-height: 18px; }}
QPushButton:hover, QToolButton:hover {{ border-color: {t.acc}; color: {t.acc_tx}; }}
QPushButton:focus, QToolButton:focus {{ border: 2px solid {t.focus}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {t.s3}; }}
QPushButton:checked, QToolButton:checked {{ background: {t.acc_dim}; border: 1px solid {t.acc}; color: {t.acc_tx};
    font-weight: 600; }}
QPushButton:disabled, QToolButton:disabled {{ color: {t.t3}; background: {t.s1}; border: 1px solid {t.bd}; }}
QPushButton#primary {{ background: {t.acc}; border: 1px solid {t.acc}; color: {t.acc_on}; font-weight: 600; }}
QPushButton#primary:hover {{ background: {t.acc}; color: {t.acc_on}; }}
QPushButton#primary:pressed {{ background: {t.acc_dim}; color: {t.acc_tx}; }}
QPushButton#primary:disabled {{ background: {t.s2}; border: 1px solid {t.bd}; color: {t.t3}; }}
QPushButton#danger {{ background: {t.s2}; {raised} color: {t.danger}; font-weight: 600; }}
QPushButton#danger:hover {{ color: {t.danger}; }}
QPushButton#danger:disabled {{ color: {t.t3}; }}
QPushButton#link {{ background: transparent; border: none; color: {t.acc_tx}; padding: 0 2px; }}
QPushButton#reset {{ background: transparent; border: none; color: {t.acc_tx}; padding: 0; min-width: 18px; font-size: 14px; }}
QPushButton#nav {{ background: transparent; border: 1px solid transparent; border-radius: {r['button']}px;
    text-align: left; padding: 7px 12px; color: {t.t2}; font-weight: 500; font-size: 13px; }}
QPushButton#nav:hover {{ color: {t.tx}; background: {t.s2}; }}
QPushButton#nav:checked {{ color: {t.acc_tx}; background: {t.acc_dim}; font-weight: 600; }}
QPushButton#objective {{ border-radius: 21px; {raised} min-width: 42px; max-width: 42px; min-height: 42px;
    max-height: 42px; padding: 0; font-weight: 600; }}
QPushButton#objective:checked {{ background: {t.acc_dim}; border: 2px solid {t.acc}; color: {t.acc_tx}; }}
QPushButton#menu {{ background: transparent; border: none; padding: 4px 9px; border-radius: 6px; color: {t.t2};
    font-weight: 400; }}
QPushButton#menu:hover {{ background: {t.s2}; color: {t.tx}; }}
QMenuBar {{ background: {t.s1}; border: none; padding: 2px 6px; }}
QMenuBar::item {{ padding: 5px 10px; border-radius: 6px; color: {t.t2}; background: transparent; }}
QMenuBar::item:selected {{ background: {t.acc_dim}; color: {t.acc_tx}; }}
QMenu {{ background: {t.s2}; {raised} border-radius: {r['menu']}px; padding: 6px; }}
QMenu::item {{ padding: 6px 28px 6px 12px; border-radius: 6px; color: {t.tx}; }}
QMenu::item:selected {{ background: {t.acc_dim}; color: {t.acc_tx}; }}
QMenu::item:disabled {{ color: {t.t3}; }}
QMenu::separator {{ height: 1px; background: {t.bd}; margin: 5px 8px; }}
QSlider::groove:horizontal {{ height: 6px; background: {t.s3}; border: 1px solid {t.bdd}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {t.acc}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {t.s2}; {raised} width: 14px; height: 14px; margin: -6px 0;
    border-radius: 8px; }}
QSlider::groove:vertical {{ width: 6px; background: {t.s3}; border: 1px solid {t.bdd}; border-radius: 3px; }}
QSlider::add-page:vertical {{ background: {t.acc}; border-radius: 3px; }}
QSlider::handle:vertical {{ background: {t.s2}; {raised} height: 14px; width: 14px; margin: 0 -6px;
    border-radius: 8px; }}
QSlider::handle:disabled {{ background: {t.s1}; border: 1px solid {t.bd}; }}
QSlider::sub-page:disabled, QSlider::add-page:vertical:disabled {{ background: {t.bd}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {t.s3}; {inset} border-radius: {r['control']}px; padding: 4px 9px;
    selection-background-color: {t.acc}; selection-color: {t.acc_on}; min-height: 20px; }}
QSpinBox, QDoubleSpinBox {{ font-family: "{tk.FONT_MONO}", monospace; font-size: 12px; padding: 3px 8px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 0; border: none; background: transparent; }}
QAbstractSpinBox::up-arrow, QAbstractSpinBox::down-arrow {{ image: none; width: 0; height: 0; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QFrame#segmented {{ background: {t.s3}; {inset} border-radius: {r['button']}px; }}
QFrame#segmented:disabled {{ background: {t.s1}; }}
QPushButton#segment {{ background: transparent; border: 1px solid transparent;
    border-radius: {max(2, r['button'] - 3)}px; padding: 4px 10px; min-height: 18px; color: {t.t2}; font-weight: 500; }}
QPushButton#segment:hover {{ background: transparent; color: {t.tx}; border: 1px solid transparent; }}
QPushButton#segment:checked {{ background: {t.s1}; {raised} color: {t.acc_tx}; font-weight: 600; }}
QPushButton#segment:focus {{ border: 2px solid {t.focus}; }}
QPushButton#segment:disabled {{ background: transparent; border: 1px solid transparent; color: {t.t3}; }}
QPushButton#segment:checked:disabled {{ background: {t.s1}; border: 1px solid {t.bd}; color: {t.t3}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{
    border: 2px solid {t.focus}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{ color: {t.t3}; }}
QComboBox QAbstractItemView {{ background: {t.s2}; {raised} selection-background-color: {t.s3};
    selection-color: {t.acc_tx}; }}
QCheckBox, QRadioButton {{ spacing: 8px; color: {t.tx}; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; {inset} background: {t.s3};
    border-radius: 5px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {t.acc}; border: 1px solid {t.acc}; }}
QCheckBox::indicator:focus, QRadioButton::indicator:focus {{ border: 2px solid {t.focus}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {t.t3}; }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ background: {t.s1}; border: 1px solid {t.bd}; }}
QTabWidget::pane {{ {raised} border-radius: {r['control']}px; top: -1px; background: {t.s1}; }}
QTabBar::tab {{ background: transparent; border: none; border-bottom: 2px solid transparent; padding: 7px 12px;
    color: {t.t2}; font-size: 13px; }}
QTabBar::tab:selected {{ color: {t.acc_tx}; font-weight: 600; border-bottom: 2px solid {t.acc}; }}
QTabBar::tab:hover {{ color: {t.tx}; }}
QTableWidget, QTreeWidget, QListWidget {{ background: {t.s3}; {inset} border-radius: {r['control']}px;
    gridline-color: {t.s3}; alternate-background-color: {t.s1}; selection-background-color: {t.acc_dim};
    selection-color: {t.tx}; }}
QHeaderView::section {{ background: {t.s1}; border: none; border-bottom: 1px solid {t.sh}; padding: 6px 8px;
    color: {t.t2}; font-weight: 600; font-size: 12px; }}
QProgressBar {{ background: {t.s3}; {inset} border-radius: 7px; text-align: center; height: 14px;
    color: {t.t2}; font-size: 11px; }}
QProgressBar::chunk {{ background: {t.acc}; border-radius: 6px; }}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {t.s2}; {raised} border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {t.s2}; {raised} border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QToolTip {{ background: {t.s2}; color: {t.tx}; border: 1px solid {t.bdd}; padding: 5px 7px; border-radius: 6px; }}
QGroupBox {{ {inset} border-radius: {r['control']}px; margin-top: 14px; padding-top: 8px; font-weight: 600;
    color: {t.t2}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
"""  # noqa: E501


_refresh_globals(_T)


def apply_theme(name_or_tokens: str | tk.Tokens, app=None) -> tk.Tokens:
    """Switch theme: updates module colours + application stylesheet and repaints top-level widgets."""
    global _T
    _T = tk.THEMES[name_or_tokens] if isinstance(name_or_tokens, str) else name_or_tokens
    _refresh_globals(_T)
    if app is not None:
        app.setStyleSheet(QSS)
        for w in app.topLevelWidgets():
            w.update()
            for child in w.findChildren(object):
                upd = getattr(child, "update", None)
                if callable(upd) and hasattr(child, "isWidgetType") and child.isWidgetType():
                    upd()
    return _T


def paint_surface(p, rect, radius: float, *, inset: bool = False, control: bool = True, fill: str | None = None) -> None:  # noqa: E501
    """The stylesheet's surfaces for QPainter widgets (status pills, channel buttons, vessel map), depth-aware
    minimalism: a `control` is a flat fill with a 3:1 border (`bdd`); a selected control (`inset`) is the tonal accent
    fill with an accent border, like checked buttons; a non-control `inset` is a quiet sunken well (s3, hairline)."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QPen

    t = _T
    r = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
    if control and inset:
        bg, edge = t.acc_dim, t.acc
    elif control:
        bg, edge = t.s2, t.bdd
    else:
        bg, edge = (t.s3, t.bd) if inset else (t.s1, t.bd)
    p.setPen(QPen(QColor(edge), 1))
    p.setBrush(QColor(fill or bg))
    p.drawRoundedRect(r, radius, radius)


def system_prefers_dark(app) -> bool:
    try:
        from PySide6.QtCore import Qt

        return app.styleHints().colorScheme() != Qt.ColorScheme.Light
    except Exception:  # noqa: BLE001 — older Qt: default to dark
        return True


def load_fonts(app) -> None:
    """Register bundled Inter + JetBrains Mono and set the app font with tabular numerals."""
    from PySide6.QtGui import QFont, QFontDatabase

    folder = Path(__file__).with_name("assets") / "fonts"
    for p in sorted(folder.glob("*.[ot]tf")):
        QFontDatabase.addApplicationFont(str(p))
    font = QFont(tk.FONT_UI)
    font.setPixelSize(px("body"))
    try:
        font.setFeature(QFont.Tag("tnum"), 1)  # tabular numerals (Qt >= 6.7)
    except Exception:  # noqa: BLE001, S110 — feature API missing on old Qt; numerals stay proportional
        pass
    app.setFont(font)


def section_font(base):
    """Section headers: sentence case, 12 px semibold (soft UI, 2026-11-17: the all-caps headers read as shouting)."""
    from PySide6.QtGui import QFont

    f = QFont(base)
    f.setPixelSize(px("section"))
    f.setWeight(QFont.DemiBold)
    f.setLetterSpacing(QFont.PercentageSpacing, 100)
    f.setCapitalization(QFont.MixedCase)
    return f
