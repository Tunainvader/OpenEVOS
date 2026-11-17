# ADR-0003: Read-only AI evidence boundary and evidence-label policy

- **Status:** Accepted (hardware-independent)
- **Date:** 2026-11-11

## Context

The plan (§4, §7, §13.3) requires AI to interpret evidence without ever actuating hardware or upgrading uncertain findings. This ADR records how the code enforces that, including points where the code is **stricter than the plan text**.

## Decisions

### 1. Read-only by construction (Inspector)
- `openevos.inspector.safe_exec` is the only module allowed to start a subprocess. It runs a fixed registry of named PowerShell queries. Each script is validated at import time against a verb allowlist (`Get`, `Select`, `Where`, `ForEach`, `ConvertTo`, …) and a list of forbidden tools (`pnputil`, `reg add`, `Invoke-Expression`, `.Delete()`, …).
- Serial ports are only *listed* (pyserial `list_ports`), never opened. The registry is opened with `KEY_READ` only. Files are opened `'rb'`.
- `tests/test_readonly_guard.py` scans the AST of every Inspector/AI module for subprocess imports outside `safe_exec`, `serial.Serial`, registry writes, file deletion or copying, and write-mode `open()`.
- The CLI refuses an output directory located inside an `--artifact-root`, which protects mounted disk images.

### 2. Evidence labels and confidence bands
Six labels, each with a fixed confidence band (half-open, so no overlap):

| Label | Confidence |
|---|---|
| Verified | exactly 1.0 |
| Strongly supported | [0.75, 1.0) |
| Probable | [0.40, 0.75) |
| Unknown / Not tested / Not applicable | must be `null` |

Assertive labels (Verified, Strongly supported, Probable) must cite evidence IDs that exist in the register.

### 3. AI cannot mint "Verified" (stricter than plan §7.4)
The plan's example JSON has the AI return a `"verified"` finding. **We do not allow that.** Only deterministic sources (collectors, rules, operator observations) may produce `Verified`. An AI finding can reach at most `Strongly supported`, and it should reference the deterministic finding through `cites_findings`. This makes "never silently upgrade probable to verified" a schema rule rather than something reviewers have to catch.

### 4. Hypotheses always carry alternatives
A hypothesis has a confidence strictly between 0 and 1, at least one alternative and at least one evidence ID. AI `Probable` findings must also list alternatives.

### 5. Recommended checks are limited to two risk classes in v0.1
`read_only` and `operator_observation` (the latter requires `requires_operator: true`). Any action text that describes transmitting bytes, opening a COM port, moving a motor, switching illumination, changing a setpoint, changing a driver or repairing a disk is rejected by `validate_against_register()`, whatever risk class it claims.

### 6. "Working" requires stage ≥ Responsive
Any AI claim that a component "is working/operational/functional" is rejected unless the register shows evidence stage `RESPONSIVE` or higher for that component. The Inspector is capped at `IDENTIFIED`, so during v0.1 such claims are always rejected.

### 7. Privacy
AI providers receive only redacted packages. Redaction uses value-based keyed tokens (HMAC), so the same serial number maps to the same token in every file. `NullProvider` (no AI) is the default, and every feature works offline.

## Consequences
- Some legitimate AI phrasing will be rejected, and prompts must teach the model to use the schema.
- New risk classes (for example, validated control in Phase 4+) need a new ADR, a schema change and new tests.
