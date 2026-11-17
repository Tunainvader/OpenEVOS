"""Camera-SDK / USB-bridge library detection by filename, strengthened by version resources.

Also lists Micro-Manager installations and their device adapters (mmgr_dal_*.dll), which
answers plan §11.4 "is there an existing adapter for the discovered camera?" once the camera
vendor is known.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

from openevos.inspector.collectors import (
    Collector,
    CollectorContext,
    CollectorSkipped,
    ScanResults,
    child_ci,
    windows_volume_roots,
)
from openevos.inspector.collectors.evos_artifacts import IMAGE_SKIP_DIRS, describe_file
from openevos.inspector.evidence import EvidenceLabel
from openevos.inspector.schemas import MicroManagerInstall, SdkMatch

PROGRAM_DIR_VARS = ("ProgramFiles", "ProgramFiles(x86)")
_MM_DIR = re.compile(r"^micro-?manager", re.IGNORECASE)
_MM_ADAPTER = re.compile(r"^mmgr_dal_(.+)\.dll$", re.IGNORECASE)


def load_signatures() -> list[dict]:
    text = resources.files("openevos.inspector.data").joinpath("sdk_signatures.json").read_text("utf-8")
    sigs = json.loads(text)["signatures"]
    for s in sigs:
        s["_rx"] = [re.compile(p, re.IGNORECASE) for p in s["patterns"]]
    return sigs


def match_signature(filename: str, sigs: list[dict]) -> dict | None:
    for s in sigs:
        if any(rx.match(filename) for rx in s["_rx"]):
            return s
    return None


class CameraSdksCollector(Collector):
    name = "camera_sdks"
    description = "Known camera SDK / USB bridge libraries and Micro-Manager adapters"
    requires_windows = False

    def _locations(self, ctx: CollectorContext) -> list[tuple[Path, frozenset[str], bool]]:
        """(root, skip_dirs, recursive)."""
        locs: list[tuple[Path, frozenset[str], bool]] = []
        for vol in windows_volume_roots(ctx):
            win = child_ci(vol, "Windows")
            for sysdir in ("System32", "SysWOW64"):
                d = child_ci(win, sysdir) if win else None
                if d is not None:
                    locs.append((d, frozenset(), False))
        if ctx.is_windows and ctx.include_default_roots:
            found, _missing = ctx.env_dirs(PROGRAM_DIR_VARS)
            locs.extend((p, frozenset(), True) for p in found)
        for r in ctx.artifact_roots:
            locs.append((r, IMAGE_SKIP_DIRS, True))
        return locs

    def collect(self, ctx: CollectorContext, results: ScanResults) -> str | None:
        locs = self._locations(ctx)
        if not locs:
            raise CollectorSkipped("no search locations")
        sigs = load_signatures()
        seen: set[str] = set()
        mm_dirs: dict[str, Path] = {}

        for root, skip, recursive in locs:
            if recursive:
                files = ctx.walk_files(root, skip)
            else:
                ctx.op(f"list-directory:{root}")
                try:
                    files = [p for p in root.iterdir() if p.is_file()]
                except OSError:
                    files = []
            for path in files:
                for idx, part in enumerate(path.parts[:-1]):
                    if _MM_DIR.match(part):
                        mm_root = Path(*path.parts[: idx + 1])
                        mm_dirs.setdefault(str(mm_root).lower(), mm_root)
                        break
                key = str(path).lower()
                if key in seen:
                    continue
                sig = match_signature(path.name, sigs)
                if sig is None:
                    continue
                seen.add(key)
                art = describe_file(ctx, path, root, [])
                if art is None:
                    continue
                company = " ".join(
                    art.version_info.get(k, "") for k in ("CompanyName", "ProductName", "LegalCopyright")
                ).lower()
                basis = [f"filename matches /{p}/" for p in sig["patterns"] if re.match(p, path.name, re.IGNORECASE)]
                label = EvidenceLabel.PROBABLE
                if any(kw in company for kw in sig["company_keywords"]):
                    label = EvidenceLabel.STRONGLY_SUPPORTED
                    basis.append("version resource names the vendor")
                data = art.model_dump(mode="json", exclude={"evidence_ids"}) | {"signature_id": sig["id"]}
                eid = ctx.add_evidence(self, "sdk", str(path), f"file:{path}", data)
                results.sdk_matches.append(
                    SdkMatch(
                        signature_id=sig["id"], category=sig["category"], vendor=sig["vendor"],
                        sdk=sig["sdk"], path=str(path), label=label, basis=basis, pe=art.pe,
                        evidence_ids=[eid],
                    )
                )

        for mm in mm_dirs.values():
            ctx.op(f"list-directory:{mm}")
            try:
                adapters = sorted(
                    m.group(1) for p in mm.iterdir() if (m := _MM_ADAPTER.match(p.name))
                )
            except OSError:
                adapters = []
            data = {"path": str(mm), "adapters": adapters}
            eid = ctx.add_evidence(self, "mm", str(mm), f"dir:{mm}", data)
            results.micromanager.append(
                MicroManagerInstall(
                    path=str(mm), version_hint=mm.name, adapters=adapters, evidence_ids=[eid]
                )
            )
        if ctx.is_windows and ctx.include_default_roots:
            _found, missing = ctx.env_dirs(PROGRAM_DIR_VARS)
            if missing:
                # System32 alone being searched must not read as "no camera SDK installed".
                return f"default search folder(s) not resolved: {', '.join(missing)}"
        return None
