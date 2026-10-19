"""Explicit model targets and required evidence accompanying generated shapes."""

from dataclasses import KW_ONLY, dataclass

from rdflib import URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report
from bijux_ontology.shacl.authoring.types import ShaclNodeShape, ShaclPropertyShape


@dataclass(frozen=True)
class ModelShapeBinding:
    """Name one model shape and its explicit IRI targets; children may be untargeted."""

    model: str
    shape: URIRef
    _: KW_ONLY
    focus_nodes: tuple[URIRef, ...] = ()
    location: str = ""

    def __post_init__(self) -> None:
        """Own bounded target metadata without inferring targets from ontology
        classes.
        """
        budget = Budget()
        if not isinstance(self.model, str):
            raise InputError("A model shape binding requires a model identifier")
        bound_text(self.model, budget)
        if not self.model.strip():
            raise InputError("A model shape binding requires a model identifier")
        bound_text(self.location, budget)
        if not isinstance(self.shape, URIRef):
            raise InputError("A model shape identifier must be an IRI")
        bound_text(self.shape, budget)
        absolute_iri(self.shape)
        if (
            type(self.focus_nodes) is not tuple
            or len(self.focus_nodes) > Limits().max_states
        ):
            raise InputError("Model shape targets require a bounded immutable tuple")
        for node in self.focus_nodes:
            if not isinstance(node, URIRef):
                raise InputError("Model shape targets must be explicit IRI terms")
            bound_text(node, budget)
            absolute_iri(node)
        object.__setattr__(self, "model", str(self.model))


@dataclass(frozen=True)
class ModelShapeTranslation:
    """Owned Core projection together with its indispensable translation report.

    A source can retain supported rules while the report identifies untranslatable
    obligations. Its presence alone never establishes complete contract coverage.
    """

    definitions: tuple[ShaclNodeShape | ShaclPropertyShape, ...]
    source: PreparedSource | None
    report: Report
