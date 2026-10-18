"""Explicit named SKOS policy scope and indispensable SHACL translation evidence."""

from dataclasses import dataclass

from rdflib import URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Report
from bijux_ontology.shacl.authoring.types import ShaclNodeShape, ShaclPropertyShape
from bijux_ontology.shacl.components.types import ComponentShapes


@dataclass(frozen=True)
class SkosProfile:
    """Select named resources and exact native label policy parameters.

    Concepts are an explicit resource selection, not an inferred class target.
    The lexical-labels application policy is selected only when named in rules.
    No XL implications, import closure or external ontology are requested here.
    """

    shape: URIRef
    concepts: tuple[URIRef, ...]
    languages: tuple[str, ...]
    rules: tuple[str, ...] = (
        "lexical-labels",
        "label-integrity",
        "language-coverage",
    )
    language_fallback: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Bound every public inventory before membership, copying or hashing."""
        budget = Budget()
        if not isinstance(self.shape, URIRef):
            raise InputError("SKOS profile shapes require an explicit named IRI")
        bound_node(self.shape, budget)
        absolute_iri(self.shape)
        for values in (
            self.concepts,
            self.languages,
            self.rules,
            self.language_fallback,
        ):
            if type(values) is not tuple:
                raise InputError(
                    "SKOS profile selections require immutable finite tuples"
                )
            budget.consume("states", len(values))
        for node in self.concepts:
            if not isinstance(node, URIRef):
                raise InputError(
                    "SKOS profile targets require explicit named IRI terms"
                )
            bound_node(node, budget)
            absolute_iri(node)
        for name in self.rules:
            if type(name) is not str:
                raise InputError("SKOS rule names require ordinary text")
            bound_text(name, budget)
            if not name:
                raise InputError("SKOS rules require nonempty names")
        for tag in (*self.languages, *self.language_fallback):
            if type(tag) is not str:
                raise InputError("SKOS language policies require ordinary tag text")
            bound_text(tag, budget)
            language_tag(tag)
        if len(set(self.concepts)) != len(self.concepts) or len(set(self.rules)) != len(
            self.rules
        ):
            raise InputError("SKOS profile targets and selected rules must be distinct")


@dataclass(frozen=True)
class SkosProfileShapes:
    """Portable supported rules beside required scope and translation evidence.

    An available RDF artifact or raw SHACL conformance never establishes complete
    translation when ``report`` retains untranslated rules or unavailable query
    qualification. Components retain existing execution requirements and trust.
    """

    profile: SkosProfile | None
    definitions: tuple[ShaclNodeShape | ShaclPropertyShape, ...]
    source: PreparedSource | None
    components: ComponentShapes | None
    translated: tuple[str, ...]
    untranslated: tuple[str, ...]
    assumptions: tuple[str, ...]
    report: Report
