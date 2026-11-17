"""Provider-independent AI interface (plan §7.3).

Providers receive a *redacted* evidence package (see openevos.inspector.reports.redaction) and
must return an `AITriageResponse`. They get no hardware handles, no subprocess access and no
filesystem paths outside the package. The core works with `NullProvider` (offline, no AI).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from openevos.ai.schemas import AITriageResponse


class AIProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def analyze_inventory(self, package: dict[str, Any]) -> AITriageResponse: ...

    @abstractmethod
    def explain_diagnostic(self, package: dict[str, Any]) -> AITriageResponse: ...


class NullProvider(AIProvider):
    """Default. Keeps the application fully functional with cloud AI disabled."""

    name = "none"

    def _empty(self) -> AITriageResponse:
        return AITriageResponse(
            summary="AI analysis not configured; deterministic findings only.",
            unknowns=["No AI interpretation was performed."],
        )

    def analyze_inventory(self, package: dict[str, Any]) -> AITriageResponse:
        return self._empty()

    def explain_diagnostic(self, package: dict[str, Any]) -> AITriageResponse:
        return self._empty()
