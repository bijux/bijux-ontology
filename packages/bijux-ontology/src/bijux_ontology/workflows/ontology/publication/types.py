"""Explicit finite SKOS publication selections and mapping boundaries."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from rdflib import BNode
from rdflib import Literal as RdfLiteral
from rdflib.namespace import RDF, SKOS

from bijux_ontology.errors import InputError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.skos.structure import _RELATIONS
from bijux_ontology.skos.xl import ROLES, XL

_STRUCTURAL = frozenset(
    map(
        str,
        (
            *_RELATIONS,
            RDF.type,
            RDF.first,
            RDF.rest,
            SKOS.inScheme,
            SKOS.topConceptOf,
            SKOS.hasTopConcept,
            SKOS.member,
            SKOS.memberList,
            *ROLES,
            XL.literalForm,
            XL.labelRelation,
        ),
    )
)


def _iris(values: Sequence[str], budget: Budget) -> tuple[str, ...]:
    """Own accepted textual identifiers without changing their exact spelling."""
    captured = capture_items(values, str, budget)
    result = []
    for value in captured:
        if isinstance(value, BNode | RdfLiteral):
            raise InputError("Publication identifiers require named IRIs")
        bound_text(value, budget)
        absolute_iri(value)
        budget.consume("states")
        result.append(str(value))
    if len(set(result)) != len(result):
        raise InputError("Publication inventories require unique IRIs")
    return tuple(result)


@dataclass(frozen=True)
class SkosPublication:
    """A selected scheme, exact concepts and complete collection dependencies.

    None selects all supplied scheme Concepts; explicit concepts never gain
    omitted collection members implicitly. All selected labels are retained;
    language requirements belong in the required ordinary consumer checks.
    Mapping inclusion follows supplied endpoints to a bounded fixed point.
    Reference and omission policies explicitly limit the completeness claim.
    """

    scheme: str
    mapping_boundary: Literal["retain-reference", "include-target", "omit"]
    concepts: tuple[str, ...] | None = None
    collections: tuple[str, ...] = ()
    annotation_predicates: tuple[str, ...] = ()
    checks: tuple[MigrationCheck, ...] = ()

    def __post_init__(self) -> None:
        """Capture finite declarations before a later caller can mutate them."""
        operation = Budget()
        scheme = _iris((self.scheme,), operation)[0]
        if self.mapping_boundary not in ("retain-reference", "include-target", "omit"):
            raise InputError("Select an explicit publication mapping boundary")
        concepts = (
            _iris(self.concepts, operation) if self.concepts is not None else None
        )
        if concepts == ():
            raise InputError("Explicit publication concepts must be nonempty")
        checks = capture_items(self.checks, MigrationCheck, operation)
        for check in checks:
            bound_text(check.name, operation)
        object.__setattr__(self, "scheme", scheme)
        object.__setattr__(self, "concepts", concepts)
        object.__setattr__(self, "collections", _iris(self.collections, operation))
        annotations = _iris(self.annotation_predicates, operation)
        if _STRUCTURAL.intersection(annotations):
            raise InputError(
                "Structural predicates cannot override publication boundaries"
            )
        object.__setattr__(self, "annotation_predicates", annotations)
        object.__setattr__(self, "checks", checks)
