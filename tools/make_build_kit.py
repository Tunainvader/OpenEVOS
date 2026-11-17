"""Assemble the OpenEVOS UI build kit for another AI/developer (no network needed to use it).

    QT_QPA_PLATFORM=offscreen python tools/make_ui_screenshots.py docs/ui/screenshots
    python tools/make_build_kit.py docs/ui/build-kit

Outputs: design-tokens.json, theme-dark.qss, theme-light.qss, icons/, fonts/, screenshots/, annotated/
(numbered callouts + legend), specs/commands.json, specs/callouts.json. BUILD_INSTRUCTIONS.md and
ACCEPTANCE.md are maintained by hand in the kit folder.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from openevos.ui import tokens as tk
from openevos.ui.theme import build_qss


def _font(size: int, bold: bool = False):
    for name in (("Inter-SemiBold.otf" if bold else "Inter-Regular.otf"), "DejaVuSans.ttf"):
        p = ROOT / "src/openevos/ui/assets/fonts" / name
        try:
            return ImageFont.truetype(str(p) if p.exists() else name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def annotate(src: Path, marks: list, dst: Path, title: str) -> None:
    im = Image.open(src).convert("RGB")
    w, h = im.size
    legend_w = 560
    out = Image.new("RGB", (w + legend_w, max(h, 120 + 34 * len(marks))), tk.DARK.bg)
    out.paste(im, (0, 0))
    d = ImageDraw.Draw(out)
    amber = tk.DARK.amber
    on = tk.on_color(amber)
    f_num, f_txt, f_title = _font(15, True), _font(15), _font(20, True)
    for i, (label, (x, y, ww, hh)) in enumerate(marks, 1):
        d.rectangle([x, y, x + ww, y + hh], outline=amber, width=2)
        cx, cy = min(max(x + 4, 14), w - 14), min(max(y + 4, 14), h - 14)
        d.ellipse([cx - 13, cy - 13, cx + 13, cy + 13], fill=amber, outline=tk.DARK.bg, width=2)
        d.text((cx, cy), str(i), fill=on, font=f_num, anchor="mm")
    lx = w + 24
    d.text((lx, 24), title, fill=tk.DARK.tx, font=f_title)
    y = 70
    for i, (label, _g) in enumerate(marks, 1):
        d.ellipse([lx, y, lx + 24, y + 24], fill=amber)
        d.text((lx + 12, y + 12), str(i), fill=on, font=f_num, anchor="mm")
        words, line, lines = label.split(), "", []
        for wd in words:
            if d.textlength(line + " " + wd, font=f_txt) > legend_w - 70:
                lines.append(line)
                line = wd
            else:
                line = (line + " " + wd).strip()
        lines.append(line)
        for k, ln in enumerate(lines):
            d.text((lx + 36, y + 3 + 19 * k), ln, fill=tk.DARK.t2, font=f_txt)
        y += max(34, 19 * len(lines) + 12)
    out.save(dst)


def main(kit: Path) -> int:
    shots = ROOT / "docs/ui/screenshots"
    kit.mkdir(parents=True, exist_ok=True)
    tokens = {
        "source": "Craftimon Studio 'Lab' palette, verified WCAG 2.2 AA (see tests/test_design_system.py)",
        "themes": {n: tk.as_dict(t) for n, t in tk.THEMES.items()},
        "typography": {"ui": tk.FONT_UI, "mono": tk.FONT_MONO, "px": tk.TYPE,
                       "section_header": "11 px, semibold, uppercase, +6 % letter spacing",
                       "numerals": "tabular (font feature tnum)"},
        "spacing_px": tk.SPACE, "radius_px": tk.RADIUS, "inspector_row": {"height": tk.ROW_H, "label_width": tk.LABEL_W},
        "states": {k: {"token": a, "text": t} for k, (a, t) in tk.STATES.items()},
        "contrast": {n: {f"{r}/{s}": round(tk.contrast(getattr(t, r), getattr(t, s)), 2)
                         for r in ("tx", "t2", "t3", "acc_tx", "teal", "ok", "warn", "danger") for s in ("bg", "s1", "s2")}
                     for n, t in tk.THEMES.items()},
    }
    (kit / "design-tokens.json").write_text(json.dumps(tokens, indent=2, ensure_ascii=False), "utf-8")
    for n, t in tk.THEMES.items():
        (kit / f"theme-{n}.qss").write_text(build_qss(t), "utf-8")
    for sub, src in (("icons", ROOT / "src/openevos/ui/assets/icons"), ("fonts", ROOT / "src/openevos/ui/assets/fonts")):
        dst = kit / sub
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    sdst = kit / "screenshots"
    sdst.mkdir(exist_ok=True)
    for p in shots.glob("*.png"):
        shutil.copy2(p, sdst / p.name)
    specs = kit / "specs"
    specs.mkdir(exist_ok=True)
    for name in ("commands.json", "callouts.json"):
        if (shots / name).exists():
            shutil.copy2(shots / name, specs / name)
    callouts = json.loads((shots / "callouts.json").read_text()) if (shots / "callouts.json").exists() else {}
    ann = kit / "annotated"
    ann.mkdir(exist_ok=True)
    titles = {"01_capture_live": "Capture workspace", "08_analysis_measure": "Analysis › Measure",
              "04_scan": "Scan workspace", "15_calibration": "Calibration center",
              "22_capture_narrow": "Narrow window (1200 px)"}
    for name, marks in callouts.items():
        annotate(shots / f"{name}.png", marks, ann / f"{name}_annotated.png", titles.get(name, name))
    # icon sheet
    from subprocess import run

    run([sys.executable, str(ROOT / "tools/render_icon_sheet.py"), str(kit / "icons"), str(kit / "icon-sheet.png")],
        check=False)
    print(f"kit written to {kit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "docs/ui/build-kit")))
