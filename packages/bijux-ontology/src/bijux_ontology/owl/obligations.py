"""Independent finite proof questions without claims of general OWL completeness."""

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.literals import DatatypeRelation, Regime, datatype_relation
from bijux_ontology.rdf.snapshots import PreparedSource, _is_owned_reader, copy_graph
from bijux_ontology.rdf.structure import read_rdf_list

from .classes import ClassContracts

ObligationKind = Literal[
    "subsumption",
    "datatype_inclusion",
    "disjointness",
    "required_coinstance",
    "restrictions",
    "consistency",
]
ProofStatus = Literal["established", "contradicted", "unknown"]
WitnessEdge = tuple[Node, URIRef, Node]


@dataclass(frozen=True)
class OntologyObligation:
    """One selected question, retaining its relation, operands and semantic regime.

    Pair relations use one actual IRI and a required IRI. ``required_coinstance``
    asks whether the supplied simultaneous types can describe a required nonempty
    instance; explicit disjointness can contradict that requirement. It is a
    different question from named subsumption, where empty classes are allowed.
    Restrictions ask about unsupported relevant class expressions. Consistency
    is a separately requested whole-ontology question, outside this native scope.
    """

    kind: ObligationKind
    actual: tuple[str, ...] = ()
    required: str | None = None
    location: str = ""
    regime: Regime = "rdf"

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {
            "subsumption",
            "datatype_inclusion",
            "disjointness",
            "required_coinstance",
            "restrictions",
            "consistency",
        }:
            raise InputError("Unknown ontology obligation")
        if isinstance(cast(object, self.actual), (str, bytes)) or not isinstance(
            self.actual, Sequence
        ):
            raise InputError("Obligation types must be an IRI sequence")
        actual = tuple(absolute_iri(iri) for iri in self.actual)
        if self.required is not None:
            absolute_iri(self.required)
        pair = self.kind in {"subsumption", "datatype_inclusion", "disjointness"}
        if pair and (len(actual) != 1 or self.required is None):
            raise InputError("Pair obligations require one actual and one required IRI")
        if not pair and self.required is not None:
            raise InputError("Only pair obligations accept a required IRI")
        if self.kind in {"required_coinstance", "restrictions"} and not actual:
            raise InputError("The selected obligation requires explicit types")
        if self.kind == "consistency" and actual:
            raise InputError("Ontology consistency has no selected class operands")
        if not isinstance(self.regime, str) or self.regime not in {
            "application",
            "rdf",
            "owl-dl",
        }:
            raise InputError("Unknown semantic regime")
        if not isinstance(self.location, str):
            raise InputError("Obligation location must be a string")
        object.__setattr__(self, "actual", actual)


@dataclass(frozen=True)
class ObligationResult:
    """One answer and its exact RDF edges or supported datatype comparison.

    ``complete`` states whether the native attempt finished within its budget.
    An unknown answer never means OWL non-entailment, disjointness, satisfiability
    or global consistency. A successful relation discharges only its own question.
    """

    obligation: OntologyObligation
    status: ProofStatus
    complete: bool = True
    witness: tuple[WitnessEdge, ...] = ()
    datatype_relation: DatatypeRelation | None = None
    basis: str = "not_proven"


class _NativeProofs:
    def __init__(self, graph: Graph, budget: Budget) -> None:
        self.graph = graph
        self.budget = budget
        self.classes = ClassContracts(graph, budget)
        self.paths: dict[tuple[str, str], tuple[WitnessEdge, ...]] = {}

    def _path(self, start: str, target: str) -> tuple[WitnessEdge, ...]:
        if start == target:
            return ()
        cached = self.paths.get((start, target))
        if cached is not None:
            return cached
        pending = deque([start])
        previous: dict[str, tuple[str, WitnessEdge] | None] = {start: None}
        while pending:
            self.budget.consume("work")
            current = pending.popleft()
            node = URIRef(current)
            edges = [
                (neighbour, (node, predicate, neighbour))
                for predicate in (RDFS.subClassOf, OWL.equivalentClass)
                for neighbour in self.graph.objects(node, predicate)
                if isinstance(neighbour, URIRef)
            ]
            edges.extend(
                (neighbour, (neighbour, OWL.equivalentClass, node))
                for neighbour in self.graph.subjects(OWL.equivalentClass, node)
                if isinstance(neighbour, URIRef)
            )
            for neighbour, edge in sorted(edges, key=lambda item: str(item[0])):
                self.budget.consume("work")
                name = str(neighbour)
                if name in previous:
                    continue
                self.budget.consume("states")
                previous[name] = current, edge
                if name == target:
                    path = []
                    cursor = target
                    while (step := previous[cursor]) is not None:
                        cursor, used = step
                        path.append(used)
                    result = tuple(reversed(path))
                    self.paths[start, target] = result
                    return result
                pending.append(name)
        return ()

    def _paths(
        self, roots: tuple[str, ...], targets: tuple[str, ...]
    ) -> tuple[WitnessEdge, ...]:
        edges: list[WitnessEdge] = []
        for target in targets:
            for root in roots:
                if target in self.classes.ancestors((root,))[0]:
                    edges.extend(self._path(root, target))
                    break
        return tuple(dict.fromkeys(edges))

    def _disjoint(
        self, roots: tuple[str, ...]
    ) -> tuple[str, tuple[WitnessEdge, ...]] | None:
        ancestors, _ = self.classes.ancestors(roots)
        if str(OWL.Nothing) in ancestors:
            return "bottom_class", self._paths(roots, (str(OWL.Nothing),))
        for left, right in sorted(
            self.graph.subject_objects(OWL.disjointWith),
            key=lambda pair: tuple(map(str, pair)),
        ):
            self.budget.consume("work")
            if (
                isinstance(left, URIRef)
                and isinstance(right, URIRef)
                and str(left) in ancestors
                and str(right) in ancestors
            ):
                return "disjoint_axiom", (
                    *self._paths(roots, (str(left), str(right))),
                    (left, OWL.disjointWith, right),
                )
        for group in sorted(
            self.graph.subjects(RDF.type, OWL.AllDisjointClasses), key=str
        ):
            self.budget.consume("work")
            heads = tuple(self.graph.objects(group, OWL.members))
            if len(heads) != 1:
                continue
            try:
                members = read_rdf_list(
                    self.graph, heads[0], max_items=self.budget.limits.max_work
                )
            except LimitExceeded:
                raise
            except InputError:
                continue
            self.budget.consume("work", len(members))
            if any(not isinstance(member, URIRef) for member in members):
                continue
            selected = tuple(member for member in members if str(member) in ancestors)
            if len(selected) < 2:
                continue
            evidence: list[WitnessEdge] = [
                (group, RDF.type, OWL.AllDisjointClasses),
                (group, OWL.members, heads[0]),
            ]
            cell = heads[0]
            for member in members:
                self.budget.consume("work")
                tail = next(self.graph.objects(cell, RDF.rest))
                evidence.extend(((cell, RDF.first, member), (cell, RDF.rest, tail)))
                cell = tail
            return "disjoint_group", (
                *self._paths(roots, tuple(map(str, selected[:2]))),
                *evidence,
            )
        return None

    def answer(self, question: OntologyObligation) -> ObligationResult:
        """Answer exactly one supported question, leaving other obligations intact."""
        self.budget.consume("work")
        if question.kind == "consistency":
            return ObligationResult(question, "unknown", basis="requires_reasoner")
        if question.kind == "restrictions":
            _, unsupported = self.classes.ancestors(question.actual)
            return ObligationResult(
                question,
                "unknown" if unsupported else "established",
                basis="unsupported_expression"
                if unsupported
                else "supported_named_scope",
            )
        if question.kind == "datatype_inclusion":
            assert question.required is not None
            relation = datatype_relation(
                question.actual[0], question.required, regime=question.regime
            )
            status: ProofStatus = (
                "established"
                if relation in {"equal", "subset"}
                else "contradicted"
                if relation == "disjoint"
                else "unknown"
            )
            return ObligationResult(
                question, status, datatype_relation=relation, basis="datatype_relation"
            )
        if question.kind == "subsumption":
            assert question.required is not None
            actual = question.actual[0]
            if actual == question.required:
                return ObligationResult(question, "established", basis="reflexivity")
            if question.required == str(OWL.Thing) or actual == str(OWL.Nothing):
                return ObligationResult(question, "established", basis="builtin_class")
            if question.required in self.classes.ancestors((actual,))[0]:
                return ObligationResult(
                    question,
                    "established",
                    witness=self._path(actual, question.required),
                    basis="named_edges",
                )
            return ObligationResult(question, "unknown")
        roots = (
            *question.actual,
            *((question.required,) if question.required is not None else ()),
        )
        disjoint = self._disjoint(roots)
        if disjoint is None:
            return ObligationResult(question, "unknown")
        basis, witness = disjoint
        self.budget.consume("states", len(witness))
        return ObligationResult(
            question,
            "contradicted" if question.kind == "required_coinstance" else "established",
            witness=witness,
            basis=basis,
        )


def prove_obligations(
    source: Graph | PreparedSource,
    obligations: Sequence[OntologyObligation],
    *,
    limits: Limits | None = None,
) -> tuple[ObligationResult, ...]:
    """Attempt finite selected proofs over one explicit owned graph scope.

    Use a loaded vocabulary graph for an already resolved import closure.
    Dataset inputs require a selected prepared snapshot or selected graph export.
    Disjointness and named non-reachability never contradict subsumption: a class
    may be empty. Unsupported datatype overlap also remains unknown.
    """
    selected = limits or Limits()
    if isinstance(cast(object, obligations), (str, bytes)) or not isinstance(
        obligations, Sequence
    ):
        raise InputError("Expected selected ontology obligations")
    if len(obligations) > selected.max_states:
        raise LimitExceeded("Selected obligations exceed max_states")
    questions = tuple(obligations)
    if any(not isinstance(item, OntologyObligation) for item in questions):
        raise InputError("Expected typed ontology obligations")
    results = []
    try:
        graph = (
            source._read()
            if isinstance(source, PreparedSource)
            else source
            if _is_owned_reader(source)
            else copy_graph(
                source, max_triples=selected.max_triples, standardize_apart=False
            )
        )
        budget = Budget(selected)
        budget.consume("triples", len(graph))
        native = _NativeProofs(graph, budget)
        for question in questions:
            results.append(native.answer(question))
    except LimitExceeded:
        results.extend(
            ObligationResult(question, "unknown", complete=False, basis="exhausted")
            for question in questions[len(results) :]
        )
    return tuple(results)
