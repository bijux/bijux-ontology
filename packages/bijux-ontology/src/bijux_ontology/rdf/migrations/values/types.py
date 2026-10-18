"""Explicit immutable object-position selection and lexical/exact value actions."""

from dataclasses import dataclass

from rdflib import BNode, Literal, URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate
from bijux_ontology.rdf.roles.classification import bound_node, bound_text
from bijux_ontology.rdf.terms import term_key

ValueTerm = Literal | URIRef


def _tuple(values: object, budget: Budget) -> tuple[object, ...]:
    """Require finite owned tuples before validating or hashing their contents."""
    if type(values) is not tuple:
        raise InputError("Value migration inventories require immutable tuples")
    budget.consume("states", len(values))
    budget.consume("bytes", 64 + 16 * len(values))
    return values


def _iri(value: object, budget: Budget) -> str:
    """Bound original text before creating its owned absolute IRI spelling."""
    if not isinstance(value, str) or isinstance(value, BNode):
        raise InputError("Value migration identifiers require absolute IRI text")
    bound_text(value, budget)
    absolute_iri(value)
    return str(value)


@dataclass(frozen=True)
class ValueSelection:
    """Select object positions inside the supplied snapshot's authorized scope.

    None subject/graph filters mean all authorized subjects/graphs. A graph None
    inside a tuple selects only the default graph. Blank graph terms must be
    actual owned snapshot terms, never reconstructed equal labels.
    """

    predicates: tuple[str, ...]
    subjects: tuple[str, ...] | None = None
    graphs: tuple[URIRef | BNode | None, ...] | None = None

    def __post_init__(self) -> None:
        """Own explicit IRI filters without changing graph term identity."""
        budget = Budget()
        predicates = tuple(_iri(x, budget) for x in _tuple(self.predicates, budget))
        if not predicates or len(set(predicates)) != len(predicates):
            raise InputError("Value selection needs distinct nonempty predicates")
        object.__setattr__(self, "predicates", predicates)
        if self.subjects is not None:
            subjects = tuple(_iri(x, budget) for x in _tuple(self.subjects, budget))
            if not subjects or len(set(subjects)) != len(subjects):
                raise InputError("Value selection subjects must be nonempty/distinct")
            object.__setattr__(self, "subjects", subjects)
        if self.graphs is not None:
            graphs = _tuple(self.graphs, budget)
            for graph in graphs:
                if graph is not None:
                    if not isinstance(graph, URIRef | BNode):
                        raise InputError("Graph selection needs exact RDF graph terms")
                    bound_node(graph, budget)
                    if isinstance(graph, URIRef):
                        absolute_iri(graph)
            if not graphs or len(set(graphs)) != len(graphs):
                raise InputError("Value selection graphs must be nonempty/distinct")


@dataclass(frozen=True)
class LiteralConversion:
    """Declare lexical interpretation/retagging while preserving every character."""

    source_datatype: str
    target_datatype: str

    def __post_init__(self) -> None:
        """Own datatype identifiers without claiming supported conversion."""
        budget = Budget()
        for name in ("source_datatype", "target_datatype"):
            object.__setattr__(self, name, _iri(getattr(self, name), budget))


@dataclass(frozen=True)
class ValueMap:
    """Rewrite only exact object terms; one pair is an explicit literal rewrite.

    Unlisted terms stay unchanged. Language changes require the dedicated flag;
    collisions additionally require migrate_values(allow_merge=True).
    """

    entries: tuple[tuple[ValueTerm, ValueTerm], ...]
    allow_language_change: bool = False

    def __post_init__(self) -> None:
        """Validate finite exact map keys before retaining an immutable action."""
        if type(self.allow_language_change) is not bool:
            raise InputError("Language-change permission must be boolean")
        budget = Budget()
        seen = set()
        entries = _tuple(self.entries, budget)
        if not entries:
            raise InputError("A value map needs at least one exact entry")
        for entry in entries:
            if type(entry) is not tuple or len(entry) != 2:
                raise InputError("Value maps require source/target term pairs")
            for value in entry:
                if not isinstance(value, Literal | URIRef):
                    raise InputError("Value maps accept only exact IRI/literal terms")
                bound_node(value, budget)
                if isinstance(value, URIRef):
                    absolute_iri(value)
            key = term_key(entry[0])
            if key in seen:
                raise InputError("A value map cannot contain duplicate exact keys")
            seen.add(key)


@dataclass(frozen=True)
class ValueMigrationCandidate(MigrationCandidate):
    """A common checked migration with separate exact object inventory counts."""

    selected: int
    changed: int
    unchanged: int
    rejected: int
