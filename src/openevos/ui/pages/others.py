"""Registry of the non-capture workspaces."""

from __future__ import annotations


def build_other_pages(model, capture_page) -> dict:
    from openevos.ui.pages.analysis import AnalysisPage
    from openevos.ui.pages.instrument import (
        CalibrationPage,
        EnvironmentPage,
        EventsPage,
        HardwarePage,
        ServicePage,
        SettingsPage,
    )
    from openevos.ui.pages.routines import ScanPage, TimelapsePage
    from openevos.ui.pages.workspaces import MoviesPage, ReviewPage, ZStackPage

    tl = TimelapsePage(model)
    return {
        "scan": ScanPage(model),
        "timelapse": tl,
        "zstack": ZStackPage(model, capture_page),
        "movies": MoviesPage(model, tl),
        "review": ReviewPage(model),
        "analysis": AnalysisPage(model),
        "hardware": HardwarePage(model),
        "environment": EnvironmentPage(model),
        "calibration": CalibrationPage(model, capture_page),
        "settings": SettingsPage(model),
        "service": ServicePage(model),
        "events": EventsPage(model),
    }
