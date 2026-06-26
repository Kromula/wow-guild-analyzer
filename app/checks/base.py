"""The dynamic check framework.

A *check* is a self-contained analysis that takes the normalized AnalysisDataset
and returns ranked findings. To add a check, drop a module in `app/checks/builtin/`
that defines a subclass of `Check` and decorate it with `@register`. To remove a
check, delete its file. Nothing else needs to change — the registry auto-discovers
everything at import time and the dashboard renders whatever exists.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum

from app.ingest.normalize import AnalysisDataset


class Category(str, Enum):
    PERFORMANCE = "Performance"
    SURVIVAL = "Survival"
    UTILITY = "Utility"
    OTHER = "Other"


class Severity(str, Enum):
    GOOD = "good"        # praise / positive signal
    INFO = "info"        # neutral information
    WARN = "warn"        # worth a look
    CRITICAL = "critical"  # needs attention


@dataclass
class CheckRow:
    """One ranked entry in a check's result (usually one player)."""
    player: str
    value: float                 # the sortable metric
    display: str                 # human-friendly value, e.g. "1.2M DPS"
    detail: str = ""             # extra context
    player_class: str | None = None


@dataclass
class CheckResult:
    id: str
    name: str
    description: str
    category: str
    severity: str
    headline: str                # one-line summary of the finding
    columns: list[str] = field(default_factory=lambda: ["Player", "Value", "Detail"])
    rows: list[CheckRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rows"] = [asdict(r) for r in self.rows]
        return d


class Check(ABC):
    """Base class for all checks. Subclass + @register to add one."""

    #: stable unique id (kebab-case), used in the API and UI
    id: str = ""
    #: short human name
    name: str = ""
    #: what the check measures / why it matters
    description: str = ""
    category: Category = Category.OTHER
    #: lower runs first; purely cosmetic ordering on the dashboard
    order: int = 100

    @abstractmethod
    def run(self, ds: AnalysisDataset) -> CheckResult | None:
        """Produce the check's finding, or return None to opt out of this view.

        Returning None lets boss-/data-specific checks (e.g. the Midnight Falls
        Glaive tracker) stay silent when their data isn't present, instead of
        rendering an empty card. The registry drops None results."""
        ...

    # Convenience for subclasses to build a result with their own metadata.
    def result(self, *, severity: Severity, headline: str,
               columns: list[str], rows: list[CheckRow]) -> CheckResult:
        return CheckResult(
            id=self.id,
            name=self.name,
            description=self.description,
            category=self.category.value,
            severity=severity.value,
            headline=headline,
            columns=columns,
            rows=rows,
        )
