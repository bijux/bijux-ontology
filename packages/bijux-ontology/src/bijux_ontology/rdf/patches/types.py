"""Portable RDF patch records; blank references are document-local tokens."""

from dataclasses import dataclass
from types import UnionType
from typing import Literal, TypeAlias

from rdflib import BNode, URIRef
from rdflib.namespace import RDF

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.reports import Report


class PatchUnavailable(InputError):
    """The requested patch lacks established scope or anonymous correspondence."""


def _text(value: str, budget: Budget) -> str:
    """Own accepted text subclasses only after bounded qualification."""
    bound_text(value, budget)
    return str(value)


def _iri(value: URIRef, budget: Budget) -> None:
    """Qualify the exact spelling and role of a named RDF term."""
    if not isinstance(value, URIRef):
        raise InputError("Patch named terms require URIRef values")
    bound_text(value, budget)
    absolute_iri(value)


@dataclass(frozen=True)
class PatchBlank:
    """Refer to one blank resource within this patch, never to a parser label."""

    token: str

    def __post_init__(self) -> None:
        """Retain bounded nonempty token text without interpreting its spelling."""
        text = _text(self.token, Budget())
        if not text:
            raise InputError("Patch blank references need nonempty tokens")
        object.__setattr__(self, "token", text)


@dataclass(frozen=True)
class PatchLiteral:
    """Retain exact lexical text and intrinsic RDF 1.1 datatype/language identity."""

    lexical: str
    datatype: str
    language: str | None = None

    def __post_init__(self) -> None:
        """Own literal fields without numeric conversion or lexical normalization."""
        budget = Budget()
        object.__setattr__(self, "lexical", _text(self.lexical, budget))
        datatype = _text(self.datatype, budget)
        absolute_iri(datatype)
        object.__setattr__(self, "datatype", datatype)
        if self.language is not None:
            language = _text(self.language, budget)
            language_tag(language)
            if datatype != str(RDF.langString):
                raise InputError("Language patch literals require rdf:langString")
            object.__setattr__(self, "language", language.lower())
        elif datatype == str(RDF.langString):
            raise InputError("rdf:langString patch literals require a language tag")


PatchResource: TypeAlias = URIRef | PatchBlank
PatchValue: TypeAlias = PatchResource | PatchLiteral


def bound_term(value: PatchValue, budget: Budget) -> None:
    """Account for existing exact term fields before hashing or copying them."""
    if isinstance(value, URIRef):
        _iri(value, budget)
    elif isinstance(value, PatchBlank):
        bound_text(value.token, budget)
    elif isinstance(value, PatchLiteral):
        for text in (value.lexical, value.datatype, value.language or ""):
            bound_text(text, budget)
    else:
        raise InputError("Unsupported portable RDF patch term")


@dataclass(frozen=True)
class PatchQuad:
    """An exact RDF 1.1 quad; None identifies only the default graph."""

    subject: PatchResource
    predicate: URIRef
    value: PatchValue
    graph: PatchResource | None = None

    def __post_init__(self) -> None:
        """Check RDF term positions without permitting a literal subject/graph."""
        budget = Budget()
        if (
            not isinstance(self.subject, URIRef | PatchBlank)
            or self.graph is not None
            and not isinstance(self.graph, URIRef | PatchBlank)
        ):
            raise InputError("Patch subjects and graph names require resource terms")
        _iri(self.predicate, budget)
        for term in (self.subject, self.value, self.graph):
            if term is not None:
                bound_term(term, budget)


def _inventory(
    values: object, expected: type[object] | UnionType, budget: Budget
) -> None:
    """Require immutable bounded typed inventories before set construction."""
    if type(values) is not tuple:
        raise InputError("Patch records require immutable finite tuples")
    budget.consume("states", len(values))
    budget.consume("bytes", 64 + 16 * len(values))
    for value in values:
        budget.consume("work")
        if not isinstance(value, expected):
            raise InputError("A patch inventory contains an invalid record")


SnapshotKind: TypeAlias = Literal["graph", "dataset", "default", "named"]


@dataclass(frozen=True)
class SnapshotWitness:
    """Exact authorized quad and named-graph inventory, including empty graphs."""

    kind: SnapshotKind
    graph_selection: PatchResource | None
    quads: tuple[PatchQuad, ...]
    graphs: tuple[PatchResource, ...] = ()

    def __post_init__(self) -> None:
        """Reject conflicting scopes and implicit undeclared named graph creation."""
        validate_witness(self, Budget())


def validate_witness(witness: SnapshotWitness, budget: Budget) -> None:
    """Qualify one already owned witness with this operation's cumulative limits."""
    if not isinstance(witness, SnapshotWitness) or witness.kind not in (
        "graph",
        "dataset",
        "default",
        "named",
    ):
        raise InputError("Expected a typed patch snapshot scope")
    if (witness.kind == "named") != (witness.graph_selection is not None):
        raise InputError("Only named patch scopes have a graph selection")
    if witness.graph_selection is not None:
        if not isinstance(witness.graph_selection, URIRef | PatchBlank):
            raise InputError("Patch graph selection requires a resource")
        bound_term(witness.graph_selection, budget)
    _inventory(witness.quads, PatchQuad, budget)
    _inventory(witness.graphs, URIRef | PatchBlank, budget)
    for name in witness.graphs:
        bound_term(name, budget)
    for quad in witness.quads:
        budget.consume("triples")
        for term in (quad.subject, quad.predicate, quad.value, quad.graph):
            if term is not None:
                bound_term(term, budget)
    if len(set(witness.quads)) != len(witness.quads) or len(set(witness.graphs)) != len(
        witness.graphs
    ):
        raise InputError("Patch snapshot inventories cannot contain duplicate records")
    graphs = set(witness.graphs)
    if any(
        quad.graph is not None and quad.graph not in graphs for quad in witness.quads
    ):
        raise InputError("Every quad graph must occur in the graph inventory")
    if witness.kind in ("graph", "default") and (
        graphs or any(q.graph is not None for q in witness.quads)
    ):
        raise InputError("Graph/default scopes cannot contain named graphs")
    if witness.kind == "named" and (
        graphs != {witness.graph_selection}
        or any(q.graph != witness.graph_selection for q in witness.quads)
    ):
        raise InputError("Named patch scope cannot widen its selected graph")


@dataclass(frozen=True)
class DatasetPatch:
    """Reviewable edits with mandatory complete structural pre/postconditions."""

    base: SnapshotWitness
    target: SnapshotWitness
    removals: tuple[PatchQuad, ...] = ()
    additions: tuple[PatchQuad, ...] = ()
    removed_graphs: tuple[PatchResource, ...] = ()
    added_graphs: tuple[PatchResource, ...] = ()

    def __post_init__(self) -> None:
        """Require immutable typed edits; application still verifies all conditions."""
        budget = Budget()
        validate_witness(self.base, budget)
        validate_witness(self.target, budget)
        if self.base.kind != self.target.kind:
            raise InputError("A patch cannot widen or change its snapshot scope")
        for values, expected in (
            (self.removals, PatchQuad),
            (self.additions, PatchQuad),
            (self.removed_graphs, URIRef | PatchBlank),
            (self.added_graphs, URIRef | PatchBlank),
        ):
            _inventory(values, expected, budget)
            if len(set(values)) != len(values):
                raise InputError("Patch edit inventories cannot contain duplicates")
        if set(self.removals) & set(self.additions) or set(self.removed_graphs) & set(
            self.added_graphs
        ):
            raise InputError("A patch cannot both add and remove the same record")


@dataclass(frozen=True)
class BlankNodePair:
    """Declare correspondence between exact owned nodes in two input snapshots."""

    before: BNode
    after: BNode

    def __post_init__(self) -> None:
        """Reject parser-label strings masquerading as ownership evidence."""
        if not isinstance(self.before, BNode) or not isinstance(self.after, BNode):
            raise InputError("Blank correspondence requires actual BNode objects")


@dataclass(frozen=True)
class PatchBlankBinding:
    """Bind one base token to an exact owned node for this application."""

    token: str
    node: BNode

    def __post_init__(self) -> None:
        """Own bounded token text while preserving the actual node witness."""
        object.__setattr__(self, "token", PatchBlank(self.token).token)
        if not isinstance(self.node, BNode):
            raise InputError("Patch blank bindings require actual BNode objects")


@dataclass(frozen=True)
class PatchPlan:
    """A complete portable patch or required evidence explaining its absence."""

    patch: DatasetPatch | None
    report: Report
