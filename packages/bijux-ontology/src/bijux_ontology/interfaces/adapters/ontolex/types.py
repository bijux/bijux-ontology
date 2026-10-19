"""Immutable asserted OntoLex structure and explicit finite consumer contracts."""

from dataclasses import dataclass

from rdflib import BNode, Literal, Namespace, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.vocabulary.browsing.results import TermInfo

ONTOLEX = Namespace("http://www.w3.org/ns/lemon/ontolex#")
Resource = URIRef | BNode
Statement = tuple[Node, Node, Node]


@dataclass(frozen=True)
class LexicalPolicy:
    """Optional arity rules concern assertions in the selected physical graph.

    Different identifiers do not prove OWL distinctness. No functional-property
    contradiction or global linguistic completeness follows from these rules.
    Required extensions request semantic interpretation, not mere byte retention.
    """

    canonical_minimum: int | None = None
    canonical_maximum: int | None = None
    reference_maximum: int | None = None
    sense_owners_maximum: int | None = None
    require_references: bool = True
    require_written: bool = True
    required_extensions: tuple[URIRef, ...] = ()

    def __post_init__(self) -> None:
        """Reject contradictory counts and mutable extension inventories."""
        for value in (
            self.canonical_minimum,
            self.canonical_maximum,
            self.reference_maximum,
            self.sense_owners_maximum,
        ):
            if value is not None and (
                type(value) is not int or value < 0 or value.bit_length() > 63
            ):
                raise InputError("Lexical counts require bounded nonnegative integers")
        if (
            self.canonical_minimum is not None
            and self.canonical_maximum is not None
            and self.canonical_minimum > self.canonical_maximum
        ):
            raise InputError("Canonical-form minimum exceeds maximum")
        if (
            type(self.require_references) is not bool
            or type(self.require_written) is not bool
        ):
            raise InputError("Lexical requirements need booleans")
        if type(self.required_extensions) is not tuple or any(
            type(p) is not URIRef for p in self.required_extensions
        ):
            raise InputError("Required extensions need an immutable IRI tuple")


@dataclass(frozen=True)
class LexicalReference:
    """Exact ontology reference and its selected declaration-role evidence."""

    target: Resource
    declaration: TermInfo | None


@dataclass(frozen=True)
class LexicalForm:
    """Form resource identity is independent of its original written literals."""

    identifier: Resource
    types: tuple[Node, ...]
    written: tuple[Literal, ...]
    extensions: tuple[Statement, ...]


@dataclass(frozen=True)
class FormLink:
    """Retain authored canonical/other/lexical roles for a shared form resource."""

    predicate: URIRef
    form: LexicalForm


@dataclass(frozen=True)
class LexicalSense:
    """Separate sense identity and every asserted ontology reference remain visible."""

    identifier: Resource
    types: tuple[Node, ...]
    references: tuple[LexicalReference, ...]
    extensions: tuple[Statement, ...]


@dataclass(frozen=True)
class LexicalEntry:
    """Distinct entries and senses never merge because their lemmas agree."""

    identifier: Resource
    types: tuple[Node, ...]
    forms: tuple[FormLink, ...]
    senses: tuple[LexicalSense, ...]
    extensions: tuple[Statement, ...]


@dataclass(frozen=True, init=False, eq=False)
class Lexicon:
    """Owned full source plus one finite selected asserted Core interpretation."""

    source: PreparedSource | None
    graph: Resource | None
    entries: tuple[LexicalEntry, ...]
    policy: LexicalPolicy
    report: Report

    def __init__(self) -> None:
        raise InputError("Use read_lexicon for qualified lexical structure")

    @classmethod
    def _owned(
        cls,
        source: PreparedSource | None,
        graph: Resource | None,
        entries: tuple[LexicalEntry, ...],
        policy: LexicalPolicy,
        report: Report,
    ) -> "Lexicon":
        result = object.__new__(cls)
        for name, value in (
            ("source", source),
            ("graph", graph),
            ("entries", entries),
            ("policy", policy),
            ("report", report),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class LexiconExport:
    """Actual reconstructed RDF; incomplete interpretation never gains export truth."""

    source: PreparedSource | None
    report: Report


@dataclass(frozen=True)
class WrittenMatch:
    """Language selection retains entry, form role, exact literal and all senses."""

    entry: LexicalEntry
    link: FormLink
    written: Literal


@dataclass(frozen=True)
class WrittenSelection:
    """Ties and required unknowns accompany every selected written representation."""

    matches: tuple[WrittenMatch, ...]
    report: Report


@dataclass(frozen=True)
class LexicalOmission:
    """An actual original quad absent from the requested simple-label output."""

    graph: Resource | None
    statement: Statement


@dataclass(frozen=True)
class LabelProjection:
    """Explicitly lossy entry labels keep their full source lexicon available."""

    lexicon: Lexicon
    source: PreparedSource | None
    omitted: tuple[LexicalOmission, ...]
    report: Report
