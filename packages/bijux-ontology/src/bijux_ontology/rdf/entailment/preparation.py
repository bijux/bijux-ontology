"""Explicit owned entailment preparation, separate from downstream validation."""

import json
import time
from dataclasses import asdict
from threading import Event
from typing import cast

from rdflib import BNode, Dataset, Graph, Literal, URIRef

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.patches.correspondence import _KINDS
from bijux_ontology.rdf.queries.protocol import QueryTerm
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, bound_text, term_key
from bijux_ontology.reports import Issue, Report

from .protocol import decode_rows, decode_term, encode_rows, encode_term
from .types import EntailmentRegime, EntailmentResult, GeneralizedTriple


def _snapshot(
    triples: tuple[GeneralizedTriple, ...],
    original: PreparedSource,
    selection: QueryTerm | None,
    union: bool,
    budget: Budget,
) -> PreparedSource:
    """Adopt bounded role triples with their joint already-owned exact terms."""
    budget.consume("resources")
    budget.consume("states", len(triples))
    budget.consume("bytes", 64 + 32 * len(triples))
    dataset = Dataset()
    is_dataset = original.is_dataset and not union
    name: GraphSelection | None = None
    if is_dataset:
        name = cast(GraphSelection, selection) if selection is not None else "@default"
    target = (
        dataset.default_graph
        if not is_dataset or name == "@default"
        else dataset.graph(cast(URIRef | BNode, name))
    )
    for triple in triples:
        budget.consume("work")
        budget.consume("triples")
        target.add(triple)
    for value in (original.document_iri, original.effective_url):
        if value is not None:
            bound_text(value, budget)
    return PreparedSource._from_owned(
        dataset,
        is_dataset=is_dataset,
        document_iri=original.document_iri,
        effective_url=original.effective_url,
        literal_fidelity=original.literal_fidelity,
        max_triples=budget.limits.max_triples,
        graph_fidelity="complete",
        graph_name=None if isinstance(name, BNode) else name,
    )


def _disjoint(
    asserted: tuple[GeneralizedTriple, ...],
    derived: tuple[GeneralizedTriple, ...],
    budget: Budget,
) -> None:
    """Qualify disjoint exact roles inside their shared peer blank capture."""
    keys = set()
    for triple in asserted:
        budget.consume("work")
        budget.consume("states")
        budget.consume("bytes", 96)
        for node in triple:
            bound_node(node, budget)
        keys.add(tuple(term_key(node) for node in triple))
    for triple in derived:
        budget.consume("work")
        for node in triple:
            bound_node(node, budget)
        if tuple(term_key(node) for node in triple) in keys:
            raise ResourceError("Entailment derived facts overlap asserted RDF")


def prepare_entailment(
    source: Graph | PreparedSource,
    *,
    regime: EntailmentRegime,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> EntailmentResult:
    """Prepare a declared finite owlrl fixed point and ordinary RDF projection.

    No SHACL option, ontology-import retrieval, JSON field requirement or source
    document is changed. RDFS and OWL-RL use the maintained implementations with
    improved datatype handling, axiomatic_triples=False and datatype_axioms=False.
    The complete final generalized consequences are retained separately, sharing
    exact term identity with the ordinary asserted/derived/effective snapshots.
    A union is deliberately flattened and recorded; ordinary physical selections
    retain their pin and document metadata. Blank graph outputs retain an owned
    Dataset plus explicit result.graph_name; pass that selector to .graph() or
    validate_shapes rather than inventing a same-label graph name. Backend errors
    and unavailable support
    and incomplete work produce no derived/effective checked artifact.
    """
    operation = Operation(limits, budget, timeout, cancel)
    if regime not in ("rdfs", "owl-rl") or type(union) is not bool:
        raise InputError(
            "Entailment requires explicit rdfs/owl-rl regime and boolean union"
        )
    issues = IssueCollector(operation)
    asserted = derived = effective = None
    generalized: tuple[GeneralizedTriple, ...] = ()
    backend = ""
    selection: QueryTerm | None = None
    selected_union = union
    completed = 0
    comparisons: list[Report] = []
    try:
        original = (
            source
            if isinstance(source, PreparedSource)
            else prepare_rdf(source, limits=operation.limits, budget=operation)
        )
        if original.literal_fidelity not in ("source_terms", "supplied_terms"):
            raise InputError(
                "Entailment requires established original RDF literal terms"
            )
        if graph_name is not None:
            bound_text(graph_name, operation)
        selected_union = union or original.union
        for name in (graph_name, original.graph_name):
            if isinstance(name, Literal):
                raise InputError("Literal values cannot select an RDF graph")
            if isinstance(name, BNode) and not any(
                owned is name for owned in original._graph_names(budget=operation)
            ):
                raise InputError(
                    "Blank graph selection lacks original source ownership"
                )
        graph = original._read(graph_name, union=union, budget=operation)
        selected_name = (
            original.graph_name if original.graph_name is not None else graph_name
        )
        if selected_name not in (None, "@default") and not selected_union:
            selection = (
                selected_name
                if isinstance(selected_name, BNode)
                else URIRef(selected_name)
            )
        rows = encode_rows(graph, operation)
        operation.consume("states", len(graph))
        operation.consume("bytes", 64 + 8 * len(graph))
        asserted = _snapshot(
            cast(tuple[GeneralizedTriple, ...], tuple(graph)),
            original,
            selection,
            selected_union,
            operation,
        )
        request = {
            "regime": regime,
            "selection": encode_term(selection, operation),
            "asserted": rows,
            "limits": asdict(operation.limits),
            "allowances": {kind: operation.remaining(kind) for kind in _KINDS},
        }
        operation.consume("bytes", json_bound(request, operation))
        allowances = {kind: operation.remaining(kind) for kind in _KINDS}
        request["allowances"] = allowances
        payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
        raw = execute_worker(
            "bijux_ontology.rdf.entailment.worker",
            payload,
            cap=operation.limits.max_total_bytes,
            timeout=max(0.000001, operation.deadline - time.monotonic()),
            cancel=cancel,
        )
        response = decode_json(
            raw,
            max_bytes=operation.limits.max_total_bytes,
            max_depth=operation.limits.max_depth,
        )
        if not isinstance(response, dict) or not isinstance(
            response.get("status"), str
        ):
            raise ResourceError("Invalid entailment worker response")
        if response["status"] != "unavailable":
            consumed = response.get("consumed")
            if not isinstance(consumed, dict) or set(consumed) != set(_KINDS):
                raise ResourceError("Entailment worker omitted cumulative counters")
            for kind in _KINDS:
                amount = consumed[kind]
                if type(amount) is not int or not 0 <= amount <= allowances[kind]:
                    raise ResourceError("Invalid entailment worker counter")
                operation.consume(kind, amount)
        backend_value = response.get("backend", "")
        if not isinstance(backend_value, str):
            raise ResourceError("Invalid entailment backend identity")
        bound_text(backend_value, operation)
        backend = str(backend_value)
        if response["status"] == "exhausted":
            raise LimitExceeded(
                "Entailment did not complete within its closure allowance"
            )
        if response["status"] != "result":
            reason = response.get("reason", "")
            if not isinstance(reason, str):
                raise ResourceError("Invalid entailment capability reason")
            bound_text(reason, operation)
            issues.append(
                Issue(
                    "rdf.entailment_unverified",
                    "Install bijux-ontology[shacl] for owlrl entailment"
                    if response["status"] == "unavailable"
                    else {
                        "unsupported_datatype": (
                            "A literal datatype is outside the maintained OWL-RL map"
                        ),
                        "imports": (
                            "Entailment does not resolve required ontology imports"
                        ),
                    }.get(
                        reason,
                        "Maintained entailment could not qualify "
                        "complete selected closure",
                    ),
                    kind="unverified",
                )
            )
        else:
            blanks: dict[str, BNode] = {}
            peer_selection = decode_term(response.get("selection"), blanks, operation)
            if peer_selection is not None and not isinstance(
                peer_selection, (URIRef, BNode)
            ):
                raise ResourceError("Invalid entailment graph selection")
            asserted_rows = decode_rows(
                response.get("asserted"), blanks, operation, ordinary=True
            )
            derived_rows = decode_rows(
                response.get("derived"), blanks, operation, ordinary=True
            )
            generalized = decode_rows(
                response.get("generalized"), blanks, operation, ordinary=False
            )
            if any(
                isinstance(s, (URIRef, BNode)) and isinstance(p, URIRef)
                for s, p, _ in generalized
            ):
                raise ResourceError(
                    "Ordinary triple mislabeled as generalized entailment"
                )
            candidate = _snapshot(
                asserted_rows, original, peer_selection, selected_union, operation
            )
            if asserted is None:
                raise ResourceError("Original selected asserted snapshot is absent")
            comparison = compare_rdf(
                asserted,
                candidate,
                budget=operation,
                timeout_seconds=max(0.000001, operation.deadline - time.monotonic()),
                cancel=operation.cancel,
            )
            retain_report(comparison, operation, issues)
            operation.consume("states")
            operation.consume("bytes", 64)
            comparisons.append(comparison)
            if not comparison.verified:
                raise ResourceError("Entailment asserted RDF failed source integrity")
            _disjoint(asserted_rows, derived_rows, operation)
            asserted = candidate
            selection = peer_selection
            derived = _snapshot(
                derived_rows, original, selection, selected_union, operation
            )
            operation.consume("states", len(asserted_rows) + len(derived_rows))
            effective = _snapshot(
                asserted_rows + derived_rows,
                original,
                selection,
                selected_union,
                operation,
            )
            completed = 1
    except LimitExceeded as error:
        issues.exhausted(str(error), location="rdf.entailment")
    except (InputError, ResourceError) as error:
        try:
            issues.append(
                Issue("rdf.entailment_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="rdf.entailment")
    if not completed:
        derived = effective = None
        generalized = ()
    report = Report(
        "rdf.entailment",
        completed,
        tuple(issues),
        requested=1,
        complete=bool(completed),
        regime="owlrl-" + regime + "-ordinary-rdf-projection",
        children=tuple(comparisons),
    )
    output_selection: GraphSelection | QueryTerm | None = selection
    if asserted is not None and asserted.graph_name == "@default":
        output_selection = "@default"
    return EntailmentResult(
        asserted,
        derived,
        effective,
        generalized,
        report,
        regime,
        backend,
        graph_name=output_selection,
        union=selected_union,
    )
