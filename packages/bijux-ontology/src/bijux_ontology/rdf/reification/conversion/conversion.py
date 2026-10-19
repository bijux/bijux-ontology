"""Selected reversible statement descriptions, without assertion or OWL claims."""

import json
import time
from dataclasses import asdict
from threading import Event
from typing import cast

import pyoxigraph as ox
from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    BUDGET_KINDS,
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.rdf.patches.applying import _materialize
from bijux_ontology.rdf.patches.correspondence import compare_witnesses
from bijux_ontology.rdf.patches.planning import Captured, capture_source, edited
from bijux_ontology.rdf.patches.types import (
    PatchBlank,
    PatchLiteral,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    PatchValue,
    SnapshotWitness,
    validate_witness,
)
from bijux_ontology.rdf.reification.checking import check_reifications
from bijux_ontology.rdf.reification.types import ReificationContract
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_datatype
from bijux_ontology.rdf.transforms import Transformation
from bijux_ontology.rdf.triple_terms.protocol import Term, encode_term

from .types import (
    REIFIER_CONTRACTS,
    ReificationConversion,
    ReifierBlank,
    ReifierDataset,
    ReifierSelection,
)

_REIFIES = URIRef(str(RDF) + "reifies")
_STRICT = ReificationContract(require_type=True)
_COMPONENTS = (RDF.subject, RDF.predicate, RDF.object)


def _native(
    value: PatchValue, blanks: dict[PatchBlank, ox.BlankNode], operation: Budget
) -> Term:
    """Translate established portable identities, never source blank labels."""
    operation.consume("work")
    if isinstance(value, PatchBlank):
        return blanks[value]
    if isinstance(value, URIRef):
        bound_text(value, operation)
        return ox.NamedNode(str(value))
    for text in (value.lexical, value.datatype, value.language or ""):
        bound_text(text, operation)
    return (
        ox.Literal(value.lexical, language=value.language)
        if value.language
        else ox.Literal(value.lexical, datatype=ox.NamedNode(value.datatype))
    )


def _reference(value: Node, captured: Captured, operation: Budget) -> PatchValue:
    """Use the existing source owner for blank references and exact literal keys."""
    bound_node(value, operation)
    if isinstance(value, BNode):
        return captured.reference(value)
    if isinstance(value, URIRef):
        return value
    raise InputError("Reifier selectors require original resource terms")


def _portable(value: Node, captured: Captured, operation: Budget) -> PatchValue:
    """Retain exact inspected components through the established patch types."""
    bound_node(value, operation)
    if isinstance(value, Literal):
        return PatchLiteral(str(value), str(literal_datatype(value)), value.language)
    return _reference(value, captured, operation)


def _wire(quads: tuple[ox.Quad, ...], operation: Budget) -> list[object]:
    """Reuse maintained-term codecs with one explicit physical graph slot."""
    rows: list[object] = []
    for quad in quads:
        operation.consume("triples")
        operation.consume("states")
        operation.consume("bytes", 64)
        rows.append(
            [
                encode_term(quad.subject, operation),
                encode_term(quad.predicate, operation),
                encode_term(quad.object, operation),
                None
                if isinstance(quad.graph_name, ox.DefaultGraph)
                else encode_term(quad.graph_name, operation),
            ]
        )
    return rows


def _prove(quads: tuple[ox.Quad, ...], contract: str, operation: Operation) -> bytes:
    """Reserve host adoption before a fixed serializer/reparser native lifetime."""
    request: dict[str, object] = {
        "contract": contract,
        "quads": _wire(quads, operation),
        "limits": asdict(operation.limits),
        "allowances": dict.fromkeys(BUDGET_KINDS, 0),
    }
    size = json_bound(request, operation)
    operation.consume("bytes", size * 3)
    cap = min(operation.limits.max_bytes, max(4096, size * 6 + 4096))
    operation.consume("bytes", cap * 16)
    operation.consume("states", 16)
    operation.consume("work", cap * 2)
    allowances = {kind: operation.remaining(kind) // 2 for kind in BUDGET_KINDS}
    request["allowances"] = allowances
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
    received = dispatched = False
    try:
        operation.consume("work", 0)
        dispatched = True
        raw = execute_worker(
            "bijux_ontology.rdf.reification.conversion.worker",
            payload,
            cap=max(cap, len(payload)),
            timeout=operation.deadline - time.monotonic(),
            cancel=operation.cancel,
        )
        if len(raw) > cap:
            raise ResourceError("Reifier response exceeds reserved adoption capacity")
        response = object_value(decode_json(raw, max_bytes=cap), location="reifiers")
        if (
            set(response) != {"protocol", "status", "content", "message", "controls"}
            or type(response["protocol"]) is not int
            or response["protocol"] != 1
        ):
            raise ResourceError("Invalid reifier worker receipt")
        controls = object_value(response["controls"], location="controls")
        if set(controls) != set(BUDGET_KINDS):
            raise ResourceError("Reifier worker omitted cumulative counters")
        for kind in BUDGET_KINDS:
            amount = controls[kind]
            if type(amount) is not int or not 0 <= amount <= allowances[kind]:
                raise ResourceError("Invalid reifier control counter")
        for kind in BUDGET_KINDS:
            operation.shared.consume(kind, cast(int, controls[kind]))
        received = True
        operation.consume("work", 0)
        message = response["message"]
        if type(message) is not str:
            raise ResourceError("Invalid reifier diagnostic")
        bound_text(message, operation)
        if response["status"] == "exhausted":
            raise LimitExceeded(message)
        if response["status"] != "result":
            raise ResourceError(message or "Native reifier qualification failed")
        content = response["content"]
        if type(content) is not str or len(content) % 2:
            raise ResourceError("Invalid reifier serialization result")
        bound_text(content, operation)
        operation.consume("bytes", len(content) // 2)
        operation.consume("work", len(content) + 1)
        try:
            return bytes.fromhex(content)
        except ValueError as error:
            raise ResourceError("Invalid native reifier output bytes") from error
    finally:
        if dispatched and not received:
            for kind in BUDGET_KINDS:
                operation.shared.consume(kind, allowances[kind])


def _restore_witness(snapshot: ReifierDataset, operation: Budget) -> SnapshotWitness:
    """Derive RDF 1.1 descriptions from actual native reifying assertions."""
    validate_witness(snapshot.base, operation)
    operation.consume(
        "states", len(snapshot.blanks) + len(snapshot.selections) + len(snapshot.quads)
    )
    operation.consume(
        "bytes",
        128 * (len(snapshot.blanks) + len(snapshot.selections) + len(snapshot.quads)),
    )
    reverse: dict[str, PatchBlank] = {}
    for pair in snapshot.blanks:
        bound_text(pair.converted.value, operation)
        bound_text(pair.reference.token, operation)
        if pair.converted.value in reverse:
            raise ResourceError("Reifier blank correspondence is not one-to-one")
        reverse[pair.converted.value] = pair.reference

    def value(term: Term) -> PatchValue:
        """Keep lexical values and only this owned capture's established blanks."""
        encode_term(term, operation)
        if isinstance(term, ox.BlankNode):
            if term.value not in reverse:
                raise ResourceError("Native blank lacks original source correspondence")
            return reverse[term.value]
        if isinstance(term, ox.NamedNode):
            return URIRef(term.value)
        if isinstance(term, ox.Literal):
            return PatchLiteral(term.value, term.datatype.value, term.language)
        raise ResourceError(
            "Nested or unselected triple terms are outside this conversion"
        )

    selected = set(snapshot.selections)
    if len(selected) != len(snapshot.selections):
        raise ResourceError("Reifier selection correspondence is ambiguous")
    seen: set[
        tuple[ox.NamedNode | ox.BlankNode, ox.NamedNode | ox.BlankNode | None]
    ] = set()
    rows = []
    for quad in snapshot.quads:
        operation.consume("work")
        operation.consume("triples")
        operation.consume("states", 8)
        operation.consume("bytes", 1024)
        name = None if isinstance(quad.graph_name, ox.DefaultGraph) else quad.graph_name
        graph = cast(PatchResource | None, value(name) if name is not None else None)
        subject = cast(PatchResource, value(quad.subject))
        key = (quad.subject, name)
        if key in selected and quad.predicate.value == str(_REIFIES):
            if (
                key in seen
                or not isinstance(quad.object, ox.Triple)
                or isinstance(quad.object.object, ox.Triple)
            ):
                raise ResourceError(
                    "Selected reifier requires one unnested triple term"
                )
            seen.add(key)
            statement = quad.object
            rows.append(PatchQuad(subject, RDF.type, RDF.Statement, graph))
            for predicate, term in zip(
                _COMPONENTS,
                (statement.subject, statement.predicate, statement.object),
                strict=True,
            ):
                rows.append(PatchQuad(subject, predicate, value(term), graph))
        else:
            rows.append(
                PatchQuad(
                    subject, URIRef(quad.predicate.value), value(quad.object), graph
                )
            )
    if seen != selected:
        raise ResourceError(
            "A selected reifier has no complete native reifying assertion"
        )
    operation.consume("states", len(snapshot.graphs))
    operation.consume("bytes", 32 + 8 * len(snapshot.graphs))
    graphs = tuple(cast(PatchResource, value(name)) for name in snapshot.graphs)
    operation.consume("work", len(rows) + len(graphs))
    operation.consume("states", len(rows) + len(graphs))
    operation.consume("bytes", 128 * (len(rows) + len(graphs)))
    if len(set(rows)) != len(rows) or len(set(graphs)) != len(graphs):
        raise ResourceError(
            "Conversion would merge original RDF assertions or graph identities"
        )
    return SnapshotWitness(
        snapshot.base.kind, snapshot.base.graph_selection, tuple(rows), graphs
    )


def _same(left: SnapshotWitness, right: SnapshotWitness, operation: Budget) -> bool:
    """Compare exact established references, not independently guessed labels."""
    count = len(left.quads) + len(right.quads) + len(left.graphs) + len(right.graphs)
    operation.consume("work", count)
    operation.consume("states", count)
    operation.consume("bytes", 128 * count)
    return (
        left.kind == right.kind
        and left.graph_selection == right.graph_selection
        and set(left.quads) == set(right.quads)
        and set(left.graphs) == set(right.graphs)
    )


def _failure(issues: IssueCollector, reports: list[Report], error: InputError) -> None:
    """Preserve completed findings before adding the final bounded uncertainty."""
    for report in reports:
        issues.retain_completed(report)
    if isinstance(error, LimitExceeded):
        issues.exhausted(str(error))
        return
    try:
        issues.append(
            Issue(
                "rdf.reifier_conversion_unverified",
                str(error),
                kind="unverified"
                if isinstance(error, ResourceError | PatchUnavailable)
                else "input_error",
            )
        )
    except LimitExceeded as exhausted:
        issues.exhausted(str(exhausted))


def convert_reifications(
    source: PreparedSource,
    selections: tuple[ReifierSelection, ...],
    *,
    contract: str,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> ReificationConversion:
    """Convert explicit complete typed records without asserting their statement.

    All unselected ordinary assertions and reifier annotations remain unchanged.
    Exact original graph inventory and global blank correspondence accompany the
    N-Quads bytes. This is a checked reversible application mapping, not exact
    graph equality or general OWL annotation equivalence.
    """
    if (
        type(source) is not PreparedSource
        or type(contract) is not str
        or type(selections) is not tuple
        or any(type(item) is not ReifierSelection for item in selections)
    ):
        raise InputError(
            "Conversion requires an owned source and finite typed selections"
        )
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    reports: list[Report] = []
    candidate = None
    try:
        bound_text(contract, operation)
        if contract not in REIFIER_CONTRACTS:
            raise ResourceError("Unsupported statement conversion contract")
        if not selections:
            raise ResourceError(
                "No selected reification obligations establish conversion"
            )
        operation.consume("states", 16 * len(selections))
        operation.consume("bytes", 2048 * len(selections))
        metadata = SnapshotMetadata.capture(source, operation)
        captured = capture_source(source, "reifier:", operation)
        operation.consume("states", len(captured.witness.graphs))
        operation.consume("bytes", 64 * len(captured.witness.graphs))
        operation.consume("work", len(captured.witness.graphs))
        allowed_graphs = set(captured.witness.graphs)
        keys: set[tuple[PatchResource, PatchResource | None]] = set()
        grouped: dict[URIRef | BNode | None, list[URIRef | BNode]] = {}
        for selection in selections:
            node = cast(PatchResource, _reference(selection.node, captured, operation))
            graph = cast(
                PatchResource | None,
                _reference(selection.graph, captured, operation)
                if selection.graph is not None
                else None,
            )
            key = (node, graph)
            if key in keys:
                raise InputError("Reifier occurrences must be selected once")
            if graph is not None and graph not in allowed_graphs:
                raise ResourceError(
                    "Selected graph is outside the original physical scope"
                )
            keys.add(key)
            grouped.setdefault(selection.graph, []).append(selection.node)
        removals: list[PatchQuad] = []
        statements: list[
            tuple[PatchResource, PatchResource | None, tuple[PatchValue, ...]]
        ] = []
        for name, nodes in grouped.items():
            checked = check_reifications(
                source,
                _STRICT,
                nodes=tuple(nodes),
                graph_name=name if name is not None else "@default",
                budget=operation,
            )
            reports.append(checked.report)
            retain_report(checked.report, operation, issues)
            if not checked.report.verified:
                return ReificationConversion(
                    None,
                    Report(
                        "rdf.reifier_conversion",
                        issues=tuple(issues),
                        requested=len(selections),
                        complete=False,
                        children=tuple(reports),
                        regime=contract,
                    ),
                )
            for record in checked.inspection.records:
                assert record.statement is not None
                if any(triple[1] == _REIFIES for triple in record.statements):
                    raise ResourceError(
                        "Mixed legacy and modern reifier descriptions "
                        "are outside this profile"
                    )
                ref = cast(PatchResource, _reference(record.node, captured, operation))
                graph_ref = cast(
                    PatchResource | None,
                    _reference(record.graph, captured, operation)
                    if record.graph is not None
                    else None,
                )
                component = tuple(
                    _portable(term, captured, operation) for term in record.statement
                )
                for predicate, value in zip(_COMPONENTS, component, strict=True):
                    removals.append(PatchQuad(ref, predicate, value, graph_ref))
                removals.append(PatchQuad(ref, RDF.type, RDF.Statement, graph_ref))
                statements.append((ref, graph_ref, component))
        ordinary = edited(captured.witness, tuple(removals), (), (), (), operation)
        operation.consume(
            "states", len(captured.owned) + len(ordinary.quads) + len(statements)
        )
        operation.consume(
            "bytes", 256 * (len(captured.owned) + len(ordinary.quads) + len(statements))
        )
        native_blanks: dict[PatchBlank, ox.BlankNode] = {}
        blank_pairs: list[ReifierBlank] = []
        for original, ref in captured.owned.values():
            operation.consume("work")
            if ref not in native_blanks:
                converted = ox.BlankNode()
                native_blanks[ref] = converted
                blank_pairs.append(ReifierBlank(original, ref, converted))
        quads = []
        for quad in ordinary.quads:
            operation.consume("work")
            s = cast(
                ox.NamedNode | ox.BlankNode,
                _native(quad.subject, native_blanks, operation),
            )
            p = cast(ox.NamedNode, _native(quad.predicate, native_blanks, operation))
            g = (
                cast(
                    ox.NamedNode | ox.BlankNode,
                    _native(quad.graph, native_blanks, operation),
                )
                if quad.graph is not None
                else ox.DefaultGraph()
            )
            quads.append(
                ox.Quad(s, p, _native(quad.value, native_blanks, operation), g)
            )
        native_selections = []
        for node, graph_ref, component in statements:
            operation.consume("work")
            ns = cast(
                ox.NamedNode | ox.BlankNode, _native(node, native_blanks, operation)
            )
            ng = (
                cast(
                    ox.NamedNode | ox.BlankNode,
                    _native(graph_ref, native_blanks, operation),
                )
                if graph_ref is not None
                else None
            )
            native_subject, native_predicate, native_object = (
                _native(value, native_blanks, operation) for value in component
            )
            quads.append(
                ox.Quad(
                    ns,
                    ox.NamedNode(str(_REIFIES)),
                    ox.Triple(
                        cast(ox.NamedNode | ox.BlankNode, native_subject),
                        cast(ox.NamedNode, native_predicate),
                        native_object,
                    ),
                    ng or ox.DefaultGraph(),
                )
            )
            native_selections.append((ns, ng))
        operation.consume("states", len(captured.witness.graphs))
        operation.consume("bytes", 32 + 8 * len(captured.witness.graphs))
        graphs = tuple(
            cast(ox.NamedNode | ox.BlankNode, _native(name, native_blanks, operation))
            for name in captured.witness.graphs
        )
        owned_quads = tuple(quads)
        content = _prove(owned_quads, contract, operation)
        temporary = ReifierDataset._from_owned(
            owned_quads,
            graphs,
            tuple(native_selections),
            tuple(blank_pairs),
            captured.witness,
            source,
            metadata,
            content,
            contract,
        )
        restored = _restore_witness(temporary, operation)
        if not _same(captured.witness, restored, operation):
            raise ResourceError(
                "Actual native inverse does not restore selected description structure"
            )
        if not metadata.same_scope(SnapshotMetadata.capture(source, operation)):
            raise ResourceError("Original source metadata changed during conversion")
        candidate = temporary
    except (InputError, LimitExceeded) as error:
        _failure(issues, reports, error)
    return ReificationConversion(
        candidate,
        Report(
            "rdf.reifier_conversion",
            len(selections) if candidate is not None else 0,
            tuple(issues),
            requested=max(1, len(selections)),
            complete=candidate is not None,
            children=tuple(reports),
            regime=contract,
        ),
    )


def restore_reifications(
    snapshot: ReifierDataset,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> Transformation:
    """Derive and check a fresh RDF 1.1 owner from this actual mapped dataset.

    Arbitrary independently parsed modern datasets are outside this initial
    profile: their empty graph inventory and cross-capture blank correspondence
    are not established. Source quads are never used as inverse reconstruction.
    """
    if type(snapshot) is not ReifierDataset:
        raise InputError("Inverse conversion requires an owned ReifierDataset")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    candidate = None
    try:
        if snapshot.contract not in REIFIER_CONTRACTS:
            raise ResourceError("Unsupported owned reifier dataset profile")
        if not snapshot.metadata.same_scope(
            SnapshotMetadata.capture(snapshot.origin, operation)
        ):
            raise ResourceError(
                "Original source scope metadata no longer matches conversion"
            )
        if len(snapshot.content) > operation.limits.max_bytes:
            raise LimitExceeded("Owned reifier output exceeds current max_bytes")
        # Native string properties allocate Python text on access. The exact
        # checked serialization bounds that decoding workspace before reuse.
        operation.consume("work", len(snapshot.content) + 1)
        operation.consume("bytes", 8 * len(snapshot.content))
        _prove(snapshot.quads, snapshot.contract, operation)
        restored = _restore_witness(snapshot, operation)
        if not _same(snapshot.base, restored, operation):
            raise ResourceError(
                "Reifier dataset does not restore its declared original structure"
            )
        bindings: dict[str, BNode] = {}
        materialized = _materialize(
            restored, snapshot.origin, operation, bindings=bindings
        )
        actual = capture_source(materialized, "restored:", operation)
        fixed = tuple(
            (token, actual.reference(node).token) for token, node in bindings.items()
        )
        equal, _ = compare_witnesses(restored, actual.witness, operation, fixed=fixed)
        if not equal:
            raise ResourceError(
                "Materialized inverse failed exact owned snapshot postcondition"
            )
        candidate = materialized
    except (InputError, LimitExceeded) as error:
        _failure(issues, [], error)
    return Transformation(
        candidate,
        Report(
            "rdf.reifier_restoration",
            len(snapshot.selections) if candidate is not None else 0,
            tuple(issues),
            requested=max(1, len(snapshot.selections)),
            complete=candidate is not None,
            regime=snapshot.contract,
        ),
    )
