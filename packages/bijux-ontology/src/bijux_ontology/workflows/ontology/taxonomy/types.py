"""Explicit class/concept identity and literal-candidate publication choices."""

from dataclasses import KW_ONLY, dataclass
from typing import Literal

from rdflib import Literal as RdfLiteral
from rdflib import URIRef
from rdflib.namespace import RDFS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.roles.classification import classify_statement
from bijux_ontology.rdf.roles.types import TermRoleOccurrence
from bijux_ontology.rdf.terms import bound_node, literal_term
from bijux_ontology.vocabulary.browsing.results import Edge
from bijux_ontology.workflows.ontology.publication.types import SkosPublication, _iris
from bijux_ontology.workflows.upgrades.plans.types import MigrationPlanResult


@dataclass(frozen=True)
class ClassConcept:
    """An exact named class/concept binding, without an equivalence assertion."""

    class_iri: str
    concept_iri: str

    def __post_init__(self) -> None:
        """Own supported textual IRI subclasses with their exact spelling."""
        budget = Budget()
        object.__setattr__(self, "class_iri", _iris((self.class_iri,), budget)[0])
        object.__setattr__(self, "concept_iri", _iris((self.concept_iri,), budget)[0])


@dataclass(frozen=True)
class PreferredClassLabel:
    """Choose one actual source candidate for its intrinsic language group."""

    class_iri: str
    term: RdfLiteral

    def __post_init__(self) -> None:
        """Capture exact literal metadata independently of caller-owned objects."""
        budget = Budget()
        object.__setattr__(self, "class_iri", _iris((self.class_iri,), budget)[0])
        if not isinstance(self.term, RdfLiteral):
            raise InputError("Preferred class labels require RDF literals")
        bound_node(self.term, budget)
        object.__setattr__(
            self,
            "term",
            literal_term(
                str(self.term),
                language=self.term.language,
                datatype=str(self.term.datatype) if self.term.datatype else None,
            ),
        )


@dataclass(frozen=True)
class TaxonomyProjection:
    """A finite named taxonomy and explicit publication modeling decisions.

    Labels/definitions come only from selected source predicates. Exact choices
    resolve a language's competing preferred labels. Omission policies explicitly
    scope the projection; they never assert OWL equivalence or global roots.
    """

    scheme: str
    classes: tuple[str, ...]
    _: KW_ONLY
    identity: Literal["same-iri-punning", "separate-iri"]
    class_mappings: tuple[ClassConcept, ...] = ()
    source_class_predicate: str | None = None
    label_predicates: tuple[str, ...] = (str(RDFS.label),)
    preferred_labels: tuple[PreferredClassLabel, ...] = ()
    definition_predicates: tuple[str, ...] = ()
    expressions: Literal["reject", "omit"] = "reject"
    outside_parents: Literal["reject", "omit"] = "reject"
    checks: tuple[MigrationCheck, ...] = ()

    def __post_init__(self) -> None:
        """Capture finite immutable declarations through existing bounded owners."""
        budget = Budget()
        for name in ("classes", "label_predicates", "definition_predicates"):
            object.__setattr__(self, name, _iris(getattr(self, name), budget))
        object.__setattr__(self, "scheme", _iris((self.scheme,), budget)[0])
        if not self.classes:
            raise InputError("Taxonomy publication requires selected named classes")
        budget.consume("states", len(self.classes))
        selected = frozenset(self.classes)
        if self.identity not in ("same-iri-punning", "separate-iri"):
            raise InputError("Select an explicit class/concept identity policy")
        if self.expressions not in ("reject", "omit") or self.outside_parents not in (
            "reject",
            "omit",
        ):
            raise InputError("Select explicit taxonomy omission policies")
        mappings = capture_items(self.class_mappings, ClassConcept, budget)
        choices = capture_items(self.preferred_labels, PreferredClassLabel, budget)
        checks = capture_items(self.checks, MigrationCheck, budget)
        object.__setattr__(self, "class_mappings", mappings)
        object.__setattr__(self, "preferred_labels", choices)
        object.__setattr__(self, "checks", checks)
        if self.source_class_predicate is not None:
            predicate = _iris((self.source_class_predicate,), budget)[0]
            SkosPublication(self.scheme, "omit", annotation_predicates=(predicate,))
            roles = classify_statement(
                (URIRef(self.scheme), URIRef(predicate), URIRef(self.scheme)),
                budget=budget,
            )
            if any(
                item.position == "predicate" and item.role == "structural"
                for item in roles
            ):
                raise InputError(
                    "Source-class links require an ordinary annotation predicate"
                )
            object.__setattr__(self, "source_class_predicate", predicate)
        if self.identity == "same-iri-punning":
            if mappings:
                raise InputError("Same-IRI punning does not accept alternate bindings")
        elif (
            self.source_class_predicate is None
            or {item.class_iri for item in mappings} != selected
            or len(mappings) != len(self.classes)
            or len({item.concept_iri for item in mappings}) != len(mappings)
            or any(item.concept_iri in selected for item in mappings)
        ):
            raise InputError("Separate concept IRIs require complete distinct bindings")
        keys = [
            (choice.class_iri, (choice.term.language or "").lower())
            for choice in choices
        ]
        if len(set(keys)) != len(keys) or any(
            choice.class_iri not in selected for choice in choices
        ):
            raise InputError(
                "Preferred label choices require unique selected language groups"
            )


@dataclass(frozen=True)
class TaxonomyExclusion:
    """An original assertion omitted by a declared finite publication policy."""

    reason: Literal["expression", "outside-parent", "label-choice"]
    statement: Edge


@dataclass(frozen=True)
class TaxonomyPublication:
    """Checked publication, exact identity table and retained source-role evidence.

    A missing publication means required choices or checks did not verify.
    Supplied-inferred hierarchy is a caller declaration, never inference proof.
    """

    bindings: tuple[ClassConcept, ...]
    publication: MigrationPlanResult | None
    report: Report
    roles: tuple[TermRoleOccurrence, ...] = ()
    exclusions: tuple[TaxonomyExclusion, ...] = ()
