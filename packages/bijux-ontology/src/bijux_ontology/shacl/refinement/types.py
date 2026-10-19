"""Explicit aligned shape contracts and finite refinement evidence."""

from dataclasses import dataclass
from typing import Literal

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.paths import RdfPath
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.shacl.checking import ShaclResult


@dataclass(frozen=True)
class RefinementContract:
    """One original selected shape and an explicit default or named data graph.

    Targets belong to the authored shape. The data graph describes the common
    validation scope, not an instruction to widen a pinned shapes selection.
    """

    source: PreparedSource
    root: URIRef | BNode
    data_graph: URIRef | None = None

    def __post_init__(self) -> None:
        """Require actual owned inputs without coercing RDF or graph identities."""
        if not isinstance(self.source, PreparedSource) or not isinstance(
            self.root, URIRef | BNode
        ):
            raise InputError("Refinement contracts require prepared shapes and a root")
        if self.data_graph is not None and not isinstance(self.data_graph, URIRef):
            raise InputError("Refinement data scope requires a named IRI or default")


@dataclass(frozen=True)
class RefinementProof:
    """One child conjunction entails one parent conjunction in the finite subset."""

    child_branch: int
    parent_branch: int
    paths: tuple[RdfPath, ...]


@dataclass(frozen=True)
class ProfileRefinement:
    """Finite implication and actual compatibility are separate required evidence.

    Refines requires a universal supported proof and an actual compatible data
    witness. Weakens requires actual child conformance and parent violation.
    Unknown includes unsupported logic and a failure to construct a witness;
    failure to prove implication alone never establishes weakening.
    """

    parent: RefinementContract
    child: RefinementContract
    relation: Literal["refines", "weakens", "unknown"]
    proofs: tuple[RefinementProof, ...]
    witness: PreparedSource | None
    parent_result: ShaclResult | None
    child_result: ShaclResult | None
    report: Report
