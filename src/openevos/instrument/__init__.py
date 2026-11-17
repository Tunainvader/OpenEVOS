"""Instrument configuration records: cameras, light channels, objective slots, vessels, settings.

These are *configuration* records with provenance. They say what the instrument is configured
to have, not what has been physically verified. Each record carries a `Provenance` so the UI can
distinguish simulator values, documentation, vendor-recovered data and verified evidence
(ADR-0003 / ADR-0006).
"""
