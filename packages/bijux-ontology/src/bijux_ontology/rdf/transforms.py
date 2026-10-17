"""Explicit syntactic transformations of owned RDF, never ontology approval."""

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import cast

from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import PreparedSource, bind_namespaces
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.reports import Issue, Report, combine_reports


@dataclass(frozen=True)
class Transformation:
    """A copied candidate and the exact syntactic operation's findings."""

    source: PreparedSource | None
    report: Report


def _budget(limits: Limits | None, budget: Budget | None) -> Budget:
    if budget is not None and limits is not None and limits != budget.limits:
        raise InputError("Explicit limits conflict with the operation budget")
    return budget or Budget(limits)


def _owned(
    dataset: Dataset, original: PreparedSource, budget: Budget
) -> PreparedSource:
    return PreparedSource(
        dataset,
        is_dataset=original.is_dataset,
        document_iri=original.document_iri,
        effective_url=original.effective_url,
        literal_fidelity=original.literal_fidelity,
        graph_fidelity=original.graph_fidelity,
        max_triples=budget.limits.max_triples,
    )


def _report(scope: str, checked: int, issues: IssueCollector) -> Report:
    return Report(
        scope,
        checked,
        tuple(issues),
        requested=max(checked, 1),
        complete=not any(issue.kind == "exhausted" for issue in issues),
        regime="syntactic-transformation",
        families=(scope,),
    )


def _fidelity(source: PreparedSource, issues: IssueCollector) -> None:
    if source.graph_fidelity != "complete":
        issues.append(
            Issue(
                "rdf.graph_scope_unverified",
                "The source does not establish whether empty named graphs existed",
                kind="unverified",
            )
        )


def extract_neighbourhood(
    source: PreparedSource,
    terms: Sequence[str],
    *,
    predicates: Sequence[str],
    include_blank_nodes: bool = True,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Transformation:
    """Copy selected subjects/edges, optionally closing attached blank structures.

    The caller chooses predicates on seed terms. Attached blank-node subjects use
    all their outgoing edges so included lists and anonymous structures remain
    intact. The closure stays within its original named graph. This is syntactic
    extraction, not a logically conservative OWL module. Other assertions and
    empty unrelated graphs are deliberately outside the extraction scope.
    """
    if (
        isinstance(terms, str)
        or isinstance(predicates, str)
        or not terms
        or not predicates
    ):
        raise InputError("Extraction requires nonempty term and predicate sequences")
    if type(include_blank_nodes) is not bool:
        raise InputError("include_blank_nodes must be a boolean")
    operation = _budget(limits, budget)
    seeds: set[URIRef] = set()
    edges: set[URIRef] = set()
    for values, selected in ((terms, seeds), (predicates, edges)):
        for value in values:
            operation.consume("states")
            selected.add(URIRef(absolute_iri(value)))
    issues = IssueCollector(operation)
    result = Dataset()
    checked = 0
    try:
        _fidelity(source, issues)
        dataset = source.dataset()
        bind_namespaces(result, dataset)
        for graph in dataset.graphs():
            operation.consume("work")
            pending: deque[Node] = deque(seeds)
            seen: set[Node] = set()
            target: Graph | None = None
            while pending:
                subject = pending.popleft()
                if subject in seen:
                    continue
                operation.consume("states")
                seen.add(subject)
                for triple in graph.triples((subject, None, None)):
                    operation.consume("work")
                    if subject in seeds and triple[1] not in edges:
                        continue
                    if target is None:
                        target = (
                            result.default_graph
                            if graph.identifier == dataset.default_graph.identifier
                            else result.graph(graph.identifier)
                        )
                    operation.consume("triples")
                    target.add(triple)
                    checked += 1
                    if include_blank_nodes and isinstance(triple[2], BNode):
                        pending.append(triple[2])
        issues.append(
            Issue(
                "rdf.extraction_scope",
                "Only selected seed predicates and requested blank closure are copied",
                severity="info",
                actual=f"{checked} triples; {len(seeds)} seed terms",
            )
        )
        candidate = _owned(result, source, operation)
    except LimitExceeded as error:
        issues.exhausted(str(error))
        candidate = None
    report = _report("rdf.extraction", checked, issues)
    return Transformation(candidate, replace(report, allow_empty=True))


def _datatype(term: Literal) -> URIRef:
    return term.datatype or (
        RDF.langString if term.language is not None else XSD.string
    )


def remap_iris(
    source: PreparedSource,
    mapping: Mapping[str, str],
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Transformation:
    """Apply exact IRI replacements in a copy, rejecting final identity collisions.

    Subjects, predicates, resource objects, named graphs and literal datatypes
    participate. Literal lexical text, language and unmapped IRIs stay unchanged.
    Prefix labels never imply IRI equivalence. Swaps are valid if final identities
    remain distinct. A target colliding with any unmapped source IRI is rejected.
    """
    if not isinstance(mapping, Mapping) or not mapping:
        raise InputError("IRI remapping requires a nonempty explicit mapping")
    operation = _budget(limits, budget)
    replacements: dict[URIRef, URIRef] = {}
    for old, new in mapping.items():
        operation.consume("states")
        replacements[URIRef(absolute_iri(old))] = URIRef(absolute_iri(new))
    issues = IssueCollector(operation)
    result = Dataset()
    changed = 0
    try:
        _fidelity(source, issues)
        dataset = source.dataset()
        iris: set[URIRef] = set()
        for graph in dataset.graphs():
            if graph.identifier != dataset.default_graph.identifier and isinstance(
                graph.identifier, URIRef
            ):
                iris.add(graph.identifier)
            for triple in graph:
                operation.consume("work")
                for term in triple:
                    if isinstance(term, URIRef):
                        iris.add(term)
                    elif isinstance(term, Literal):
                        iris.add(_datatype(term))
        identities: dict[URIRef, URIRef] = {}
        for iri in sorted(iris):
            operation.consume("states")
            target = replacements.get(iri, iri)
            if target in identities and identities[target] != iri:
                issues.append(
                    Issue(
                        "rdf.remap_collision",
                        "Distinct source IRIs would acquire the same final identity",
                        str(target),
                        expected=str(identities[target]),
                        actual=str(iri),
                    )
                )
            identities[target] = iri
        if any(issue.code == "rdf.remap_collision" for issue in issues):
            return Transformation(None, _report("rdf.remapping", len(iris), issues))

        def remap(term: Node) -> Node:
            """Preserve lexical text even when a caller maps the datatype IRI."""
            nonlocal changed
            if isinstance(term, URIRef):
                target = replacements.get(term, term)
                changed += target != term
                return target
            if isinstance(term, Literal) and _datatype(term) in replacements:
                changed += 1
                return literal_term(
                    str(term),
                    datatype=str(replacements[_datatype(term)]),
                    language=term.language,
                )
            return term

        bind_namespaces(result, dataset)
        for graph in dataset.graphs():
            operation.consume("work")
            target_graph = (
                result.default_graph
                if graph.identifier == dataset.default_graph.identifier
                else result.graph(cast(URIRef | BNode, remap(graph.identifier)))
            )
            for triple in graph:
                operation.consume("triples")
                target_graph.add((remap(triple[0]), remap(triple[1]), remap(triple[2])))
        candidate = _owned(result, source, operation)
        issues.append(
            Issue(
                "rdf.remap_scope",
                "Applied only the supplied exact IRI mapping",
                severity="info",
                actual=f"{changed} changed RDF positions",
            )
        )
    except LimitExceeded as error:
        issues.exhausted(str(error))
        candidate = None
    except InputError as error:
        try:
            issues.append(Issue("rdf.remap_literal_loss", str(error)))
        except LimitExceeded as limit:
            issues.exhausted(str(limit))
        candidate = None
    return Transformation(
        candidate, _report("rdf.remapping", len(replacements), issues)
    )


def merge_sources(
    sources: Sequence[PreparedSource],
    *,
    union: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Transformation:
    """Copy selected snapshots, separating independent blank nodes per source.

    Named graphs and empty graph identities remain unless union=True deliberately
    selects a graph union. Import statements remain ordinary RDF assertions; this
    operation never fetches imported documents or infers their closure.
    """
    if not sources or type(union) is not bool:
        raise InputError("Merging requires sources and a boolean union policy")
    operation = _budget(limits, budget)
    issues = IssueCollector(operation)
    result = Dataset()
    checked = 0
    try:
        datasets: list[Dataset] = []
        for source in sources:
            operation.consume("resources")
            _fidelity(source, issues)
            dataset = source.dataset()
            datasets.append(dataset)
            nodes: dict[BNode, BNode] = {}

            def own(term: Node, nodes: dict[BNode, BNode] = nodes) -> Node:
                """Independent documents cannot share a blank-node label by chance."""
                if isinstance(term, BNode):
                    if term not in nodes:
                        operation.consume("states")
                        nodes[term] = BNode()
                    return nodes[term]
                return term

            for graph in dataset.graphs():
                operation.consume("work")
                target = (
                    result.default_graph
                    if union or graph.identifier == dataset.default_graph.identifier
                    else result.graph(cast(URIRef | BNode, own(graph.identifier)))
                )
                for triple in graph:
                    operation.consume("triples")
                    target.add((own(triple[0]), own(triple[1]), own(triple[2])))
            checked += 1
        bind_namespaces(result, *datasets)
        candidate = PreparedSource(
            result,
            is_dataset=not union and any(source.is_dataset for source in sources),
            document_iri=None,
            effective_url=None,
            literal_fidelity="source_terms",
            graph_fidelity="complete"
            if all(source.graph_fidelity == "complete" for source in sources)
            else "nonempty-only",
            max_triples=operation.limits.max_triples,
        )
        issues.append(
            Issue(
                "rdf.merge_scope",
                "Merged explicit sources; import statements remain without fetching",
                severity="info",
                actual="graph union" if union else "dataset membership retained",
            )
        )
    except LimitExceeded as error:
        issues.exhausted(str(error))
        candidate = None
    return Transformation(candidate, _report("rdf.merge", checked, issues))


def recheck_transformation(
    original: PreparedSource,
    candidate: PreparedSource,
    checks: Mapping[str, Callable[[PreparedSource], Report]],
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Retain selected before/after check outcomes without adopting the candidate.

    Callbacks are trusted Python code. Their deliberate exceptions propagate.
    Each receives an independent owned snapshot so a callback cannot mutate the
    original or influence another check. Findings are scoped to their supplied
    checks and never presented as scientific or full OWL approval.
    """
    if not checks or any(
        not name or not callable(check) for name, check in checks.items()
    ):
        raise InputError("Rechecking requires named callable checks")
    operation = _budget(limits, budget)
    reports: list[Report] = []
    for name, check in checks.items():
        for role, source in (("before", original), ("after", candidate)):
            operation.consume("work")
            owned = _owned(source.dataset(), source, operation)
            report = check(owned)
            if not isinstance(report, Report):
                raise InputError("Transformation checks must return Report")
            reports.append(replace(report, scope=f"{role}.{name}"))
    return replace(
        combine_reports("rdf.candidate_checks", *reports),
        regime="selected-candidate-checks",
        families=("rdf.candidate_checks",),
    )
