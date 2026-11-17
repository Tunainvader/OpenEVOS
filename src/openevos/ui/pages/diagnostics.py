"""Diagnostics workspace: open an Inspector scan folder and show its findings, devices, serial ports,
collectors and report, read-only. Ported from the legacy UI (UI merge, phase 4)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from openevos.core.diagnostics import ScanBundle, ScanLoadError, find_scans, load_scan
from openevos.ui import theme
from openevos.ui.common import label, panel

FINDING_HEADERS = ["Label", "Confidence", "Component", "Claim", "Origin", "Rule", "Stage", "Evidence"]
DEVICE_HEADERS = ["Name", "Class", "Status", "Present", "VID:PID", "Driver", "Problem", "Evidence stage", "Instance ID"]
PORT_HEADERS = ["Port", "Description", "VID:PID", "Manufacturer", "HWID"]
COLLECTOR_HEADERS = ["Collector", "Status", "Duration", "Evidence", "Reason", "Read-only operations"]
STAGE_NAMES = {1: "Detected", 2: "Identified", 3: "Connected", 4: "Responsive", 5: "Functionally validated"}
LABELS = ("verified", "strongly_supported", "probable", "unknown", "not_tested", "not_applicable")


def _label_color(lab: str) -> str:
    return {"verified": theme.OK, "strongly_supported": theme.OK, "probable": theme.ACCENT_TX,
            "unknown": theme.WARN, "not_tested": theme.MUTED, "not_applicable": theme.MUTED}.get(lab, theme.MUTED)


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    t.horizontalHeader().setStretchLastSection(True)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.setMinimumHeight(260)
    return t


def _item(text, color: str | None = None, tooltip: str | None = None) -> QTableWidgetItem:
    it = QTableWidgetItem("" if text is None else str(text))
    if color:
        it.setForeground(theme.qcolor(color) if hasattr(theme, "qcolor") else _qc(color))
    if tooltip:
        it.setToolTip(str(tooltip))
    return it


def _qc(hex_color: str):
    from PySide6.QtGui import QColor

    return QColor(hex_color)


class DiagnosticsPage(QWidget):
    def __init__(self, model, scans_dir: Path | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.model = model
        self.scans_dir = scans_dir or getattr(model, "scans_dir", None) or Path.cwd() / "openevos-scans"
        self.bundle: ScanBundle | None = None
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        g, gl = panel("Inspector scan")
        bar = QHBoxLayout()
        open_btn = QPushButton("Open scan folder…")
        open_btn.setObjectName("primary")
        open_btn.clicked.connect(self._browse)
        bar.addWidget(open_btn)
        self.recent = QComboBox()
        self.recent.setMinimumWidth(320)
        self.recent.activated.connect(self._open_recent)
        bar.addWidget(self.recent)
        self.path_label = label("No scan loaded", "muted")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bar.addWidget(self.path_label, 1)
        gl.addLayout(bar)
        gl.addWidget(label("Read-only: this view never changes the scan, the PC or any device.", "muted"))
        self.summary = QWidget()
        self.summary_grid = QGridLayout(self.summary)
        self.summary_grid.setContentsMargins(0, 4, 0, 0)
        self.summary_grid.setHorizontalSpacing(24)
        gl.addWidget(self.summary)
        root.addWidget(g)

        self.tabs = QTabWidget()
        filt_w = QWidget()
        fl = QVBoxLayout(filt_w)
        fl.setContentsMargins(0, 6, 0, 0)
        frow = QHBoxLayout()
        self.label_filter = QComboBox()
        self.label_filter.addItem("All labels", None)
        for lab in LABELS:
            self.label_filter.addItem(lab.replace("_", " ").capitalize(), lab)
        self.label_filter.currentIndexChanged.connect(self._fill_findings)
        frow.addWidget(QLabel("Label"))
        frow.addWidget(self.label_filter)
        frow.addStretch(1)
        fl.addLayout(frow)
        self.findings_table = _table(FINDING_HEADERS)
        fl.addWidget(self.findings_table)
        self.tabs.addTab(filt_w, "Findings")
        self.devices_table = _table(DEVICE_HEADERS)
        self.tabs.addTab(self.devices_table, "Devices")
        self.ports_table = _table(PORT_HEADERS)
        self.tabs.addTab(self.ports_table, "Serial ports")
        self.collectors_table = _table(COLLECTOR_HEADERS)
        self.tabs.addTab(self.collectors_table, "Collectors")
        self.report = QTextBrowser()
        self.report.setOpenExternalLinks(False)
        self.tabs.addTab(self.report, "Report")
        root.addWidget(self.tabs, 1)
        self._fill_summary()
        self.refresh_recent()

    # ------------------------------------------------------------------ loading
    def refresh_recent(self) -> None:
        self.recent.clear()
        scans = find_scans(self.scans_dir)
        self.recent.setToolTip(str(self.scans_dir))
        if not scans:
            self.recent.addItem(f"No scans found in {self.scans_dir.name}/", None)
            self.recent.setEnabled(False)
            return
        self.recent.setEnabled(True)
        self.recent.addItem(f"Recent scans in {self.scans_dir.name}/ ({len(scans)})", None)
        for s in scans:
            self.recent.addItem(s.name, str(s))

    def _open_recent(self, index: int) -> None:
        path = self.recent.itemData(index)
        if path:
            self.open_scan(Path(path))

    def _browse(self) -> None:
        start = str(self.scans_dir if self.scans_dir.is_dir() else Path.cwd())
        d = QFileDialog.getExistingDirectory(self, "Open Inspector scan folder", start)
        if d:
            self.open_scan(Path(d))

    def open_scan(self, folder: Path) -> bool:
        try:
            self.bundle = load_scan(folder)
        except ScanLoadError as exc:
            self.model.add_log("error", f"diagnostics: {exc}")
            return False
        self.path_label.setText(str(folder))
        self._fill_summary()
        self._fill_findings()
        self._fill_devices()
        self._fill_ports()
        self._fill_collectors()
        self.report.setMarkdown(self.bundle.report_markdown or "_No openevos_diagnostic_report.md in this scan._")
        self.model.add_log("info", f"diagnostics: loaded scan {self.bundle.scan_id} (read-only)")
        return True

    # ------------------------------------------------------------------ fill
    def _fill_summary(self) -> None:
        g = self.summary_grid
        while g.count():
            w = g.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        b = self.bundle
        if b is None:
            g.addWidget(label("Open a folder written by openevos-inspector scan (for example "
                              "openevos-scans/<scan_id>/) to see its findings and devices here.", "muted", wrap=True),
                        0, 0)
            return
        m = b.meta
        pairs = [("Scan", b.scan_id), ("Label", b.label or "—"),
                 ("Started", str(m.get("started_at", "—"))[:19].replace("T", " ")),
                 ("Host", m.get("host_platform", "—")), ("Tool", m.get("tool_version", "—")),
                 ("Redacted", "yes" if m.get("redacted") else "no"), ("Evidence items", str(b.evidence_count)),
                 ("Register integrity", "hashes verified" if b.register_validated else "NOT validated")]
        for i, (k, v) in enumerate(pairs):
            g.addWidget(label(k, "muted"), (i // 4) * 2, i % 4)
            vl = QLabel(str(v))
            if k == "Register integrity":
                vl.setStyleSheet(f"color: {theme.OK if b.register_validated else theme.FAULT}; font-weight: 600;")
            g.addWidget(vl, (i // 4) * 2 + 1, i % 4)
        counts = b.label_counts()
        parts = [f"{lab.replace('_', ' ')} {counts[lab]}" for lab in LABELS if counts.get(lab)]
        g.addWidget(QLabel(f"{len(b.findings)} findings: " + " · ".join(parts)
                           + f"  ·  {len(b.devices)} devices · {len(b.serial_ports)} serial ports"), 4, 0, 1, 4)
        if b.problems:
            p = QLabel("Problems: " + "; ".join(b.problems))
            p.setStyleSheet(f"color: {theme.WARN};")
            p.setWordWrap(True)
            g.addWidget(p, 5, 0, 1, 4)

    def _fill_findings(self) -> None:
        t = self.findings_table
        t.setRowCount(0)
        if self.bundle is None:
            return
        want = self.label_filter.currentData()
        rows = [f for f in self.bundle.findings if want is None or f.get("label") == want]
        t.setRowCount(len(rows))
        for r, f in enumerate(rows):
            lab = str(f.get("label", ""))
            conf = f.get("confidence")
            stage = STAGE_NAMES.get(f.get("stage"), "") if f.get("stage") else ""
            ev = ", ".join(f.get("evidence_ids") or [])
            alts = f.get("alternatives") or []
            tip = str(f.get("claim", "")) + (("\nAlternatives: " + "; ".join(alts)) if alts else "")
            if f.get("note"):
                tip += f"\nNote: {f['note']}"
            cells = [_item(lab.replace("_", " "), _label_color(lab)), _item("—" if conf is None else f"{conf:.2f}"),
                     _item(f.get("component", ""), tooltip=f.get("component", "")),
                     _item(f.get("claim", ""), tooltip=tip), _item(f.get("origin", "")), _item(f.get("rule_id") or ""),
                     _item(stage), _item(ev, tooltip=ev)]
            for c, it in enumerate(cells):
                t.setItem(r, c, it)
        t.resizeColumnsToContents()
        t.setColumnWidth(3, min(t.columnWidth(3), 560))
        t.setColumnWidth(2, min(t.columnWidth(2), 300))

    def _fill_devices(self) -> None:
        t = self.devices_table
        devs = self.bundle.devices if self.bundle else []
        t.setRowCount(len(devs))
        for r, d in enumerate(devs):
            usb = d.get("usb") or {}
            vidpid = f"{usb.get('vid')}:{usb.get('pid')}" if usb.get("vid") and usb.get("pid") else ""
            if usb.get("mi"):
                vidpid += f" MI_{usb['mi']}"
            drv = " ".join(x for x in (d.get("driver_provider"), d.get("driver_version")) if x)
            status = d.get("status") or ""
            color = theme.OK if status == "OK" else (theme.FAULT if status == "Error" else None)
            cells = [_item(d.get("friendly_name") or d.get("bus_reported_description") or ""),
                     _item(d.get("device_class") or ""), _item(status, color),
                     _item({True: "yes", False: "no (remembered)"}.get(d.get("present"), "?")),
                     _item(vidpid), _item(drv), _item(d.get("problem") or ""),
                     _item(STAGE_NAMES.get(d.get("stage"), str(d.get("stage") or ""))),
                     _item(d.get("instance_id", ""), tooltip=d.get("instance_id", ""))]
            for c, it in enumerate(cells):
                t.setItem(r, c, it)
        t.resizeColumnsToContents()

    def _fill_ports(self) -> None:
        t = self.ports_table
        ports = self.bundle.serial_ports if self.bundle else []
        t.setRowCount(len(ports))
        for r, p in enumerate(ports):
            vp = f"{p.get('vid')}:{p.get('pid')}" if p.get("vid") and p.get("pid") else ""
            for c, v in enumerate([p.get("device"), p.get("description"), vp, p.get("manufacturer"), p.get("hwid")]):
                t.setItem(r, c, _item(v or ""))
        t.resizeColumnsToContents()

    def _fill_collectors(self) -> None:
        t = self.collectors_table
        cols = self.bundle.collectors if self.bundle else []
        t.setRowCount(len(cols))
        for r, c in enumerate(cols):
            st = c.get("status", "")
            color = {"ok": theme.OK, "partial": theme.WARN, "skipped": theme.MUTED}.get(st, theme.FAULT)
            ops = ", ".join(c.get("operations") or [])
            cells = [_item(c.get("name")), _item(st, color), _item(f"{float(c.get('duration_s', 0)):.2f} s"),
                     _item(c.get("evidence_count")), _item(c.get("reason") or ""), _item(ops, tooltip=ops)]
            for i, it in enumerate(cells):
                t.setItem(r, i, it)
        t.resizeColumnsToContents()
