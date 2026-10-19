"""Explicit deployment axes and finite supported consumer-version evidence."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.reports import Report
from bijux_ontology.workflows.upgrades.applications.types import (
    ApplicationCheck,
    _json,
    _mapping,
)


def _name(value: str, budget: Budget) -> str:
    """Own explicit labels without assigning meaning to version syntax."""
    if not isinstance(value, str) or not value.strip():
        raise InputError("Matrix identities require nonempty text")
    bound_text(value, budget)
    return str(value)


@dataclass(frozen=True)
class ConsumerVersion:
    """One caller-required consumer contract version, never a version heuristic."""

    consumer: str
    version: str

    def __post_init__(self) -> None:
        """Capture both labels before use in any registry or diagnostics."""
        budget = Budget()
        object.__setattr__(self, "consumer", _name(self.consumer, budget))
        object.__setattr__(self, "version", _name(self.version, budget))


@dataclass(frozen=True)
class MatrixCell:
    """One explicitly selected application/context/ontology combination."""

    application: str
    ontology: str
    context: str

    def __post_init__(self) -> None:
        """Capture axis labels independently of the caller's mutable registries."""
        budget = Budget()
        for name in ("application", "ontology", "context"):
            object.__setattr__(self, name, _name(getattr(self, name), budget))


@dataclass(frozen=True, init=False)
class MatrixContext:
    """An explicit context and complete closed resource registry for this axis.

    ABSENT means use the actual output's own context. JSON null is a distinct
    explicit context. These resources replace the application's context registry;
    no hidden resources are borrowed from another context version.
    """

    _context_json: str | None
    _resources_json: Mapping[str, str]

    def __init__(
        self,
        context: object | Absent = ABSENT,
        *,
        resources: Mapping[str, object] | None = None,
    ) -> None:
        """Capture bounded context data before returning a reusable axis value."""
        budget = Budget()
        supplied = _mapping(resources if resources is not None else {}, object, budget)
        owned = {}
        for url, value in supplied.items():
            absolute_iri(url)
            owned[url] = _json(value, budget)
        object.__setattr__(
            self, "_context_json", None if context is ABSENT else _json(context, budget)
        )
        object.__setattr__(self, "_resources_json", MappingProxyType(owned))

    @property
    def context(self) -> object | Absent:
        """Export independent data, retaining explicit null versus absence."""
        return ABSENT if self._context_json is None else json.loads(self._context_json)

    @property
    def resources(self) -> Mapping[str, object]:
        """Export this version's independent closed JSON resources only."""
        return {url: json.loads(value) for url, value in self._resources_json.items()}


@dataclass(frozen=True)
class ConsumerCompatibility:
    """The original single-version report for one required consumer obligation."""

    consumer: ConsumerVersion
    report: Report | None = None
    check: ApplicationCheck | None = None
    required: bool = True


@dataclass(frozen=True)
class CellCompatibility:
    """A selected tested combination, or an explicitly untested known cell."""

    cell: MatrixCell
    selected: bool
    consumers: tuple[ConsumerCompatibility, ...] = ()
    report: Report | None = None

    @property
    def compatible(self) -> bool:
        """Require actual complete agreement, rather than a missing report."""
        return self.selected and self.report is not None and self.report.verified


@dataclass(frozen=True)
class CompatibilityMatrix:
    """Finite selected evidence; other version combinations remain untested."""

    cells: tuple[CellCompatibility, ...]
    required_consumers: tuple[ConsumerVersion, ...]
    report: Report
    requested_consumers: int = 0

    @property
    def deployable(self) -> tuple[MatrixCell, ...]:
        """Return only cells whose every required consumer check verified."""
        return tuple(item.cell for item in self.cells if item.compatible)
