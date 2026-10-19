"""Owned CSVW cell correspondence and explicit native RDF postconditions."""

from collections.abc import Callable
from dataclasses import dataclass

from rdflib import Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.snapshots import PreparedSource


@dataclass(frozen=True)
class CsvwCell:
    """Original CSV-decoded cell and statements produced by its declared mapping.

    Row and column ordinals start at one. ``line`` is the original physical start
    line, including the header; quoted multiline cells retain their parsed text.
    Null and suppressed cells have no statements, rather than invented values.
    """

    table: str
    row: int
    line: int
    column: int
    name: str
    text: str
    statements: tuple[tuple[URIRef, URIRef, URIRef | Literal], ...] = ()


@dataclass(frozen=True)
class CsvwCheck:
    """An explicit required ordinary check over actual owned extracted RDF.

    Callbacks are trusted synchronous Python. They receive the shared operation
    budget; neither native parsing nor arbitrary callbacks are hard-preempted.
    """

    name: str
    check: Callable[[PreparedSource, Budget], Report]

    def __post_init__(self) -> None:
        """Require a named callable without guessing any semantic postcondition."""
        if type(self.name) is not str or not self.name or not callable(self.check):
            raise InputError("CSVW checks require a name and a callable")


@dataclass(frozen=True)
class CsvwExtraction:
    """Conversion evidence and required selected checks retain separate truth.

    ``source`` exists only after complete successful conversion. Caller checks
    can reject that actual candidate without destroying its inspected evidence.
    No result asserts a complete CSVW implementation or universal RDF validity.
    """

    source: PreparedSource | None
    cells: tuple[CsvwCell, ...]
    conversion_report: Report
    check_reports: tuple[Report, ...]
    report: Report
