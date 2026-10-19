"""Explicit representative data and named shape correspondence for release checks."""

from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef
from rdflib.term import Node

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Report
from bijux_ontology.shacl.execution.results import ShaclResult
from bijux_ontology.shacl.findings import ShaclFinding


@dataclass(frozen=True)
class ShapeSample:
    """One actual owned physical data selection consumed unchanged by both sides."""

    name: str
    data: PreparedSource

    def __post_init__(self) -> None:
        """Own finite sample metadata without preparing the data a second time."""
        bound_text(self.name, Budget())
        if not self.name or not isinstance(self.data, PreparedSource):
            raise InputError("Shape samples require a name and PreparedSource")
        object.__setattr__(self, "name", str(self.name))


@dataclass(frozen=True)
class ShapeCorrespondence:
    """Caller-established named identities; None explicitly adds/removes a shape.

    Anonymous shape labels are deliberately outside this correspondence API.
    Pure anonymous renaming is qualified separately by joint RDF isomorphism.
    """

    identifier: str
    before: URIRef | None
    after: URIRef | None

    def __post_init__(self) -> None:
        """Require explicit named endpoints and an owned finite logical name."""
        operation = Budget()
        bound_text(self.identifier, operation)
        if not self.identifier or self.before is None and self.after is None:
            raise InputError("Shape correspondence needs a name and an endpoint")
        for node in (self.before, self.after):
            if node is not None:
                if not isinstance(node, URIRef):
                    raise InputError("Shape correspondence endpoints must be URIRef")
                bound_node(node, operation)
                absolute_iri(node)
        object.__setattr__(self, "identifier", str(self.identifier))


@dataclass(frozen=True)
class ShapeFindingChange:
    """An established contract group with every original finding still available.

    A joint anonymous group asserts isomorphic result sets, not a unique pairing
    of internal blank nodes. Values, severities and multiplicity remain raw.
    """

    status: Literal["introduced", "resolved", "persistent", "uncertain"]
    identity: tuple[object, ...] | None
    before: tuple[ShaclFinding, ...]
    after: tuple[ShaclFinding, ...]


@dataclass(frozen=True)
class ShapeTargetChange:
    """Actual completed root evaluation, separate from constraint nonconformance."""

    status: Literal["introduced", "removed", "persistent"]
    shape: tuple[str, str]
    focus: Node


@dataclass(frozen=True)
class ShapeSampleAssessment:
    """Both raw maintained results beside qualified finding and target deltas."""

    name: str
    before: ShaclResult | None
    after: ShaclResult | None
    findings: tuple[ShapeFindingChange, ...]
    targets: tuple[ShapeTargetChange, ...]
    report: Report


@dataclass(frozen=True)
class ShapeUpgradeAssessment:
    """Finite representative impact without a universal refinement assertion."""

    samples: tuple[ShapeSampleAssessment, ...]
    report: Report

    @property
    def verdict(self) -> str:
        """Retain demonstrated regressions even when later required work stops."""
        if any(
            any(change.status == "introduced" for change in sample.findings)
            or any(change.status == "removed" for change in sample.targets)
            for sample in self.samples
        ):
            return "demonstrated-regression"
        return "agreement" if self.report.verified else "unverified"
