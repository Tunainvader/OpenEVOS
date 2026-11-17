"""Write the Inspector's JSON exports. Writes only inside the chosen output directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from openevos.inspector.reports.redaction import Redactor

EXPORT_FILES = {
    "system": "system_inventory.json",
    "devices": "device_inventory.json",
    "drivers": "driver_inventory.json",
    "software": "software_inventory.json",
    "register": "evidence_register.json",
    "manifest": "scan_manifest.json",
}


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def write_exports(out_dir: Path, models: dict[str, BaseModel], redactor: Redactor | None = None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for key, model in models.items():
        data = _dump(model)
        if redactor is not None:
            data = redactor.register_dict(data) if key == "register" else redactor.obj(data)
        path = out_dir / EXPORT_FILES[key]
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
        written[key] = path
    return written
