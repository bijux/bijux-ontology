"""Bounded syntactic Skolemization without canonical labels or entailment."""

from collections.abc import Sequence
from typing import cast
from uuid import uuid4

from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.patches.types import BlankNodePair
from bijux_ontology.rdf.skolem.types import (
    SkolemBinding,
    SkolemMap,
    SkolemResult,
)
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata as _SnapshotMetadata
from bijux_ontology.rdf.sources.snapshots import PreparedSource, _read_graph
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.rdf.transforms import (
    Transformation,
    _authorized_contexts,
    _bind,
    _budget,
)


def _contexts(
    source: PreparedSource, budget: Budget
) -> tuple[tuple[URIRef | BNode | None, Graph], ...]:
    """Retain physical graph roles, including an explicitly authorized union."""
    for text in (
        source.document_iri,
        source.effective_url,
        source.graph_name,
        source.literal_fidelity,
        source.graph_fidelity,
    ):
        if text is not None:
            bound_text(text, budget)
    if source.graph_fidelity != "complete":
        raise InputError("Complete named-graph inventory is required for reversal")
    result: list[tuple[URIRef | BNode | None, Graph]] = []
    default = source._dataset.default_graph.identifier
    selected = (
        (
            (
                None
                if graph.identifier == default
                else cast(URIRef | BNode, graph.identifier),
                _read_graph(graph),
            )
            for graph in source._owned_contexts(budget)
        )
        if source.union
        else _authorized_contexts(source, budget)
    )
    for name, graph in selected:
        budget.consume("states")
        budget.consume("bytes", 32)
        if name is not None:
            bound_node(name, budget)
        result.append((name, graph))
    return tuple(result)


def _inventory(
    contexts: tuple[tuple[URIRef | BNode | None, Graph], ...],
    budget: Budget,
) -> tuple[dict[int, BNode], dict[BNode, BNode], set[URIRef]]:
    """Qualify actual objects before hashing, retaining cross-role RDF identity."""
    owned: dict[int, BNode] = {}
    blanks: dict[BNode, BNode] = {}
    iris: set[URIRef] = set()

    def add(node: Node) -> None:
        bound_node(node, budget)
        budget.consume("states")
        budget.consume("bytes", 32)
        if isinstance(node, BNode):
            owned[id(node)] = node
            blanks.setdefault(node, node)
        elif isinstance(node, URIRef):
            iris.add(node)
        elif isinstance(node, Literal) and node.datatype is not None:
            iris.add(node.datatype)

    for name, graph in contexts:
        if name is not None:
            add(name)
        for triple in graph:
            budget.consume("triples")
            budget.consume("work")
            for node in triple:
                add(node)
    return owned, blanks, iris


def _adopt(dataset: Dataset, source: PreparedSource, budget: Budget) -> PreparedSource:
    """Adopt only the newly allocated dataset without changing owned blank terms."""
    return PreparedSource._from_owned(
        dataset,
        is_dataset=source.is_dataset,
        document_iri=source.document_iri,
        effective_url=source.effective_url,
        literal_fidelity=source.literal_fidelity,
        graph_fidelity=source.graph_fidelity,
        graph_name=source.graph_name,
        union=source.union,
        max_triples=budget.limits.max_triples,
    )


def _copy(
    source: PreparedSource,
    contexts: tuple[tuple[URIRef | BNode | None, Graph], ...],
    replacements: dict[Node, Node],
    budget: Budget,
) -> PreparedSource:
    """Apply one qualified term correspondence to every selected physical role."""
    dataset = Dataset()
    for name, graph in contexts:
        budget.consume("work")
        changed_name = replacements.get(name, name) if name is not None else None
        if changed_name is not None and not isinstance(changed_name, URIRef | BNode):
            raise InputError("A transformed graph name must remain a resource")
        target = (
            dataset.default_graph
            if changed_name is None
            else dataset.graph(changed_name)
        )
        _bind(dataset, (graph,), budget)
        for triple in graph:
            budget.consume("work")
            budget.consume("triples")
            budget.consume("states")
            budget.consume("bytes", 24)
            subject, predicate, value = triple
            target.add(
                (
                    replacements.get(subject, subject),
                    replacements.get(predicate, predicate),
                    replacements.get(value, value),
                )
            )
    return _adopt(dataset, source, budget)


def _report(issues: IssueCollector, completed: bool) -> Report:
    return Report(
        "rdf-skolemization",
        int(completed),
        tuple(issues),
        requested=1,
        complete=completed,
        regime="scoped-rdf-identity-replacement",
        families=("rdf-skolemization",),
    )


def _qualified_map(mapping: SkolemMap, budget: Budget) -> None:
    """Reject changed snapshot scope or fidelity before reusing owned identities."""
    original = _SnapshotMetadata.capture(mapping.original, budget)
    output = _SnapshotMetadata.capture(mapping.output, budget)
    if not mapping._original_metadata.same_scope(
        original
    ) or not mapping._output_metadata.same_scope(output):
        raise InputError("Skolem map snapshot metadata changed after map creation")


def _shared(
    shared: SkolemMap | None,
    pairs: Sequence[BlankNodePair],
    base: str,
    owned: dict[int, BNode],
    iris: set[URIRef],
    budget: Budget,
) -> dict[Node, Node]:
    """Require explicit actual-owned pair witnesses for every reused identity."""
    captured = capture_items(pairs, BlankNodePair, budget)
    count = len(captured)
    budget.consume("states", count)
    budget.consume("bytes", 32 * count)
    if shared is None:
        if count:
            raise InputError("Shared correspondence requires a prior Skolem map")
        return {}
    if not isinstance(shared, SkolemMap):
        raise InputError("Shared correspondence requires a factory-owned Skolem map")
    _qualified_map(shared, budget)
    bound_text(shared.base, budget)
    if shared.base != base:
        raise InputError("Shared maps require the same explicit Skolem base")
    prior_owned, _, _ = _inventory(_contexts(shared.original, budget), budget)
    _, _, prior_output = _inventory(_contexts(shared.output, budget), budget)
    prior: dict[int, SkolemBinding] = {}
    for binding in shared.bindings:
        budget.consume("work")
        budget.consume("states")
        budget.consume("bytes", 32)
        bound_node(binding.original, budget)
        bound_node(binding.iri, budget)
        if (
            prior_owned.get(id(binding.original)) is not binding.original
            or binding.iri not in prior_output
        ):
            raise InputError(
                "The shared map no longer establishes its owned correspondence"
            )
        prior[id(binding.original)] = binding
    result: dict[Node, Node] = {}
    used: set[URIRef] = set()
    for index in range(count):
        budget.consume("work")
        pair = captured[index]
        if not isinstance(pair, BlankNodePair):
            raise InputError("Correspondence requires typed BlankNodePairs")
        bound_node(pair.before, budget)
        bound_node(pair.after, budget)
        selected_binding = prior.get(id(pair.before))
        if (
            selected_binding is None
            or selected_binding.original is not pair.before
            or owned.get(id(pair.after)) is not pair.after
        ):
            raise InputError(
                "Blank correspondence requires exact original owned objects"
            )
        if pair.after in result or selected_binding.iri in used:
            raise InputError("Shared blank correspondence must be one-to-one")
        if selected_binding.iri in iris:
            raise InputError(
                "A shared Skolem IRI collides with an existing named identity"
            )
        result[pair.after] = selected_binding.iri
        used.add(selected_binding.iri)
    return result


def skolemize(
    source: PreparedSource,
    base: str,
    *,
    shared: SkolemMap | None = None,
    blank_pairs: Sequence[BlankNodePair] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> SkolemResult:
    """Replace selected blanks using fresh UUID names under a caller-owned base.

    The base must be an absolute IRI ending in '/'; ownership is the caller's
    assertion. Every invocation generates a fresh operation UUID. Explicit pairs
    alone can reuse identities from a previous factory map. Original literals,
    empty graphs and physical sharing remain intact. This changes RDF identity,
    and does not establish exact graph isomorphism until reversal.
    """
    operation = _budget(limits, budget)
    issues = IssueCollector(operation)
    output = None
    mapping = None
    completed = False
    try:
        if not isinstance(source, PreparedSource):
            raise InputError("Skolemization requires an owned prepared snapshot")
        if not isinstance(base, str) or isinstance(base, BNode | Literal):
            raise InputError("Skolem bases require named IRI text")
        bound_text(base, operation)
        base = str(base)
        absolute_iri(base)
        if not base.endswith("/") or "#" in base or "?" in base:
            raise InputError("A Skolem base must end in '/' without query or fragment")
        contexts = _contexts(source, operation)
        owned, blanks, iris = _inventory(contexts, operation)
        replacements = _shared(shared, blank_pairs, base, owned, iris, operation)
        operation.consume("states")
        operation.consume("bytes", 32)
        token = uuid4().hex
        bindings: list[SkolemBinding] = []
        for index, node in enumerate(blanks):
            operation.consume("work")
            operation.consume("states")
            operation.consume("bytes", 64)
            iri = replacements.get(node)
            if iri is None:
                operation.consume("work", len(base) + len(token) + 32)
                operation.consume("bytes", 4 * (len(base) + len(token) + 32))
                text = base + token + "/" + str(index)
                bound_text(text, operation)
                iri = URIRef(text)
            if not isinstance(iri, URIRef) or iri in iris:
                raise InputError(
                    "A generated Skolem IRI collides with an existing identity"
                )
            iris.add(iri)
            replacements[node] = iri
            bindings.append(SkolemBinding(node, iri))
        candidate = _copy(source, contexts, replacements, operation)
        mapping = SkolemMap._from_owned(
            source, candidate, base, token, tuple(bindings), operation
        )
        output = candidate
        completed = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(Issue("rdf.skolem_unverified", str(error), kind="unverified"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return SkolemResult(output, mapping, _report(issues, completed))


def deskolemize(
    source: PreparedSource,
    mapping: SkolemMap,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Transformation:
    """Reverse only the exact output snapshot explicitly bound by a factory map.

    Equal-content clones and maps belonging to other invocations are rejected;
    similarly spelled unrelated IRIs are never treated as generated blank nodes.
    """
    operation = _budget(limits, budget)
    issues = IssueCollector(operation)
    output = None
    completed = False
    try:
        if not isinstance(mapping, SkolemMap) or source is not mapping.output:
            raise InputError("Reversal requires the map's exact output snapshot object")
        _qualified_map(mapping, operation)
        contexts = _contexts(source, operation)
        _, _, iris = _inventory(contexts, operation)
        original, _, _ = _inventory(_contexts(mapping.original, operation), operation)
        replacements: dict[Node, Node] = {}
        for binding in mapping.bindings:
            operation.consume("work")
            operation.consume("states")
            operation.consume("bytes", 32)
            bound_node(binding.iri, operation)
            bound_node(binding.original, operation)
            if (
                original.get(id(binding.original)) is not binding.original
                or binding.iri not in iris
            ):
                raise InputError("Reversal lacks its exact owned correspondence")
            replacements[binding.iri] = binding.original
        output = _copy(source, contexts, replacements, operation)
        completed = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(Issue("rdf.skolem_unverified", str(error), kind="unverified"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return Transformation(output, _report(issues, completed))
