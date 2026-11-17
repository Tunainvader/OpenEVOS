# ADR-0006: STOP halts motion, light and acquisition, but keeps the incubator running

- **Status:** Accepted (project owner, 2026-11-12)
- **Related:** plan §13 (safety and command governance), ADR-0005 (incubator path)

## Context

The local STOP control (button and Esc) must act at once and never depend on anything remote (plan §13.3). The question was whether it should also switch off the AMC1000's heating and gas.

Cutting temperature or CO₂ control in the middle of an experiment can harm or kill live samples. Stopping motion and light protects the instrument and the sample from what STOP is meant to address: an unexpected move, a collision, or photobleaching.

## Decision

- **STOP:** halts all motion, switches every LED off, cancels any running acquisition, and latches. No further commands run until the operator chooses *Resume control*.
- **The incubator is outside STOP.** Its setpoints and enabled controls (temperature, gas) are left exactly as they were.
- **Switching incubator controls off** stays a separate, explicit action that needs operator confirmation, like every incubator change.

`tests/test_hal_policy.py::test_incubator_keeps_running_through_stop` enforces this.

## Consequences

- An incubator fault (over-temperature, gas problem) needs its own response path. STOP is not that path. Define the incubator alarm and shutdown behaviour when the CN board's real fault codes are known, and keep the board's own protections authoritative (ADR-0005).
- The UI must make clear that STOP does not affect the incubator.
