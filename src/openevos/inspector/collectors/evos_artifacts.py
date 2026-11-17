"""EVOS software remnants: metadata, hashes, PE bitness and version resources — never copies.

Search roots:
* live Windows (default): Program Files, Program Files (x86), ProgramData, the current user's
  AppData, and top-level folders on the system drive whose name matches an EVOS keyword;
* every --artifact-root, e.g. a *read-only mount of a copy* of the recovered OptiPlex disk image.
  (Mount the image, never the original disk. Linux: `mount -o ro,loop,noexec ...`.)

A directory whose name matches a keyword is "hot": every file beneath it is described, because
DLLs and calibration files inside an EVOS folder rarely carry the brand in their own names.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from openevos.inspector.collectors import Collector, CollectorContext, CollectorSkipped, ScanResults
from openevos.inspector.collectors.keywords import keyword_hits
from openevos.inspector.pe import PE_EXTENSIONS, parse_pe, version_info
from openevos.inspector.safe_exec import QueryError
from openevos.inspector.schemas import FileArtifact

MAX_SIGNATURE_CHECKS = 500
DEFAULT_ROOT_VARS = ("ProgramFiles", "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA", "APPDATA")
# OpenEVOS's own package/project folders and default output folder: its source files
# (evos_artifacts.py, evos_fl_auto_profile.json) and earlier scans are not EVOS remnants.
LIVE_SKIP_DIRS = frozenset({"openevos", "openevos-scans"})

IMAGE_SKIP_DIRS = frozenset(
    {"windows", "$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws",
     "recovery", "msocache", "$sysreset"}
)


def sha256_file(path: Path, limit: int) -> tuple[str | None, str | None]:
    try:
        size = path.stat().st_size
        if size > limit:
            return None, f"larger than hash limit ({size} > {limit} bytes)"
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest(), None
    except OSError as exc:
        return None, f"unreadable: {exc.__class__.__name__}"


def describe_file(ctx: CollectorContext, path: Path, root: Path, hits: list[str]) -> FileArtifact | None:
    try:
        st = path.stat()
    except OSError:
        return None
    ctx.op(f"read-file:{path}")
    sha, skip = sha256_file(path, ctx.hash_size_limit)
    pe = parse_pe(path) if path.suffix.lower() in PE_EXTENSIONS else None
    return FileArtifact(
        path=str(path),
        root=str(root),
        size=st.st_size,
        modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        sha256=sha,
        skipped_hash_reason=skip,
        keyword_hits=hits,
        pe=pe,
        version_info=version_info(path) if pe else {},
    )


def search_roots(ctx: CollectorContext) -> list[tuple[Path, frozenset[str]]]:
    roots: list[tuple[Path, frozenset[str]]] = []
    if ctx.is_windows and ctx.include_default_roots:
        found, _missing = ctx.env_dirs(DEFAULT_ROOT_VARS)
        roots.extend((p, LIVE_SKIP_DIRS) for p in found)
        drive = Path(ctx.environ.get("SystemDrive", "C:") + os.sep)
        ctx.op(f"list-directory:{drive}")
        try:
            for p in drive.iterdir():
                if p.is_dir() and keyword_hits(p.name):
                    roots.append((p, LIVE_SKIP_DIRS))
        except OSError:
            pass
    for r in ctx.artifact_roots:
        roots.append((r, IMAGE_SKIP_DIRS))
    seen, uniq = set(), []
    for r, skip in roots:
        k = str(r).lower()
        if k not in seen:
            seen.add(k)
            uniq.append((r, skip))
    return uniq


class EvosArtifactsCollector(Collector):
    name = "evos_artifacts"
    description = "EVOS-related files: metadata, SHA-256, PE bitness, version info (no copying)"
    requires_windows = False

    def collect(self, ctx: CollectorContext, results: ScanResults) -> str | None:
        defaults_wanted = ctx.is_windows and ctx.include_default_roots
        roots = search_roots(ctx)
        if not roots:
            why = "no default search folder could be resolved" if defaults_wanted else "not Windows"
            raise CollectorSkipped(f"no search roots ({why}, and no --artifact-root given)")
        count, truncated = 0, False
        for root, skip in roots:
            for path in ctx.walk_files(root, skip):
                try:
                    rel_parts = path.relative_to(root).parts
                except ValueError:
                    rel_parts = path.parts
                dir_hits: list[str] = []
                for part in rel_parts[:-1]:
                    dir_hits = keyword_hits(part) or dir_hits
                    if dir_hits:
                        break
                hits = sorted(set(dir_hits) | set(keyword_hits(path.name)))
                if not hits:
                    continue
                if count >= ctx.max_artifact_files:
                    truncated = True
                    break
                art = describe_file(ctx, path, root, hits)
                if art is None:
                    continue
                eid = ctx.add_evidence(
                    self, "file", str(path), f"file:{path}",
                    art.model_dump(mode="json", exclude={"evidence_ids"}),
                )
                art.evidence_ids.append(eid)
                results.artifacts.append(art)
                count += 1
        warnings: list[str] = []
        if defaults_wanted:
            _found, missing = ctx.env_dirs(DEFAULT_ROOT_VARS)
            if missing:
                # Never let a narrower search read as a complete "nothing found".
                warnings.append(f"default search folder(s) not resolved: {', '.join(missing)}")
        if truncated:
            warnings.append(f"stopped after {ctx.max_artifact_files} files; raise --max-artifact-files")
        if ctx.is_windows:
            sig_warning = self._signatures(ctx, results)
            if sig_warning:
                warnings.append(sig_warning)
        return "; ".join(warnings) or None

    def _signatures(self, ctx: CollectorContext, results: ScanResults) -> str | None:
        """Authenticode status for PE artifacts (Get-AuthenticodeSignature; read-only)."""
        pe_arts = [a for a in results.artifacts if a.pe is not None][:MAX_SIGNATURE_CHECKS]
        if not pe_arts:
            return None
        try:
            rows = ctx.query("authenticode", stdin_obj=[a.path for a in pe_arts])
        except QueryError as exc:
            return f"signature check unavailable: {exc}"
        by_path = {str(r.get("Path", "")).lower(): r for r in rows}
        for a in pe_arts:
            r = by_path.get(a.path.lower())
            if r is None:
                continue
            a.signature = {"status": r.get("Status"), "signer": r.get("Signer"), "thumbprint": r.get("Thumbprint")}
            eid = ctx.add_evidence(self, "sig", a.path, "powershell:authenticode", r)
            a.evidence_ids.append(eid)
        return None
