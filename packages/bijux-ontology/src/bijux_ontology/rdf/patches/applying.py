"""Transactional owned application with checked base and materialized postcondition."""

import threading
from collections.abc import Sequence
from typing import cast

from rdflib import BNode, Dataset, URIRef
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_text, literal_term
from bijux_ontology.rdf.transforms import Transformation
from bijux_ontology.reports import Issue

from .codecs import patch_to_data
from .correspondence import Operation, blank_tokens, compare_witnesses
from .planning import Rejected, capture_items, capture_source, edited, report
from .types import (
    DatasetPatch,
    PatchBlank,
    PatchBlankBinding,
    PatchLiteral,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    PatchValue,
    SnapshotWitness,
)


def _materialize(
    witness: SnapshotWitness,
    original: PreparedSource,
    operation: Operation,
    *,
    bindings: dict[str, BNode] | None = None,
) -> PreparedSource:
    """Own exact records and optionally expose their fresh anonymous identities.

    The optional empty caller dictionary receives bindings only after complete
    materialization. Each token denotes the actual node in the returned owner,
    never a parser label or a node from the original input snapshot.
    """
    if bindings is not None and (type(bindings) is not dict or bindings):
        raise InputError("Materialization bindings require an empty plain dictionary")
    operation.consume("states", len(witness.quads) + len(witness.graphs))
    dataset = Dataset()
    blanks: dict[str, BNode] = {}

    def node(term: PatchValue) -> Node:
        """Allocate one fresh owned blank for every shared portable reference."""
        operation.consume("work")
        if isinstance(term, PatchBlank):
            if term.token not in blanks:
                operation.consume("states")
                operation.consume("bytes", 64)
                blanks[term.token] = BNode()
            return blanks[term.token]
        if isinstance(term, PatchLiteral):
            for text in (term.lexical, term.datatype, term.language or ""):
                bound_text(text, operation)
            return literal_term(
                term.lexical, datatype=term.datatype, language=term.language
            )
        bound_text(term, operation)
        return URIRef(term)

    for graph in witness.graphs:
        operation.consume("resources")
        dataset.graph(cast(URIRef | BNode, node(graph)))
    for quad in witness.quads:
        operation.consume("work")
        operation.consume("triples")
        output_graph = (
            dataset.default_graph
            if quad.graph is None
            else dataset.graph(cast(URIRef | BNode, node(quad.graph)))
        )
        output_graph.add((node(quad.subject), node(quad.predicate), node(quad.value)))
    selection: GraphSelection | None = None
    if witness.kind == "named":
        assert witness.graph_selection is not None
        selection = cast(URIRef | BNode, node(witness.graph_selection))
    elif witness.kind == "default":
        selection = "@default"
    operation.consume("work", len(witness.quads) + len(witness.graphs))
    operation.consume("triples", len(witness.quads))
    for metadata in (original.document_iri, original.effective_url):
        if metadata is not None:
            bound_text(metadata, operation)
    result = PreparedSource._from_owned(
        dataset,
        is_dataset=witness.kind != "graph",
        document_iri=original.document_iri,
        effective_url=original.effective_url,
        literal_fidelity=original.literal_fidelity,
        graph_fidelity="complete",
        graph_name=selection if not isinstance(selection, BNode) else None,
        max_triples=operation.limits.max_triples,
    )
    if isinstance(selection, BNode):
        # Selection now refers to the exact fresh graph allocated by node().
        result.graph_name = selection
    if bindings is not None:
        operation.consume("states", len(blanks))
        operation.consume("bytes", 64 * len(blanks))
        operation.consume("work", len(blanks))
        bindings.update(blanks)
    return result


def apply_rdf_patch(
    base: PreparedSource,
    patch: DatasetPatch,
    *,
    blank_bindings: Sequence[PatchBlankBinding] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> Transformation:
    """Publish an independent candidate only after both structural conditions pass.

    Required affected base blanks must resolve uniquely or have jointly verified
    explicit bindings to actual owned nodes. Untouched symmetric blanks need no
    invented identity. Failures never alter the supplied snapshot or publish an
    incompletely checked candidate.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    candidate = None
    try:
        if not isinstance(patch, DatasetPatch):
            raise InputError("RDF patch application requires a typed DatasetPatch")
        # The public codec qualifies every externally supplied edit before hashing.
        patch_to_data(patch, budget=operation)
        supplied = capture_items(blank_bindings, PatchBlankBinding, operation)
        captured = capture_source(base, "input:", operation)
        fixed: list[tuple[str, str]] = []
        used = set()
        base_tokens = blank_tokens(patch.base, operation)
        for binding in supplied:
            operation.consume("work", len(fixed) + 1)
            bound_text(binding.token, operation)
            target = captured.reference(binding.node).token
            if (
                binding.token not in base_tokens
                or binding.token in used
                or any(pair[1] == target for pair in fixed)
            ):
                raise InputError(
                    "Explicit patch blank bindings require distinct known base "
                    "tokens and owned nodes"
                )
            operation.consume("states")
            fixed.append((binding.token, target))
            used.add(binding.token)
        operation.consume(
            "states",
            len(patch.removals)
            + len(patch.additions)
            + len(patch.removed_graphs)
            + len(patch.added_graphs),
        )
        affected = {
            term.token
            for quad in (*patch.removals, *patch.additions)
            for term in (quad.subject, quad.value, quad.graph)
            if isinstance(term, PatchBlank) and term.token in base_tokens
        }
        affected.update(
            term.token
            for term in (*patch.removed_graphs, *patch.added_graphs)
            if isinstance(term, PatchBlank) and term.token in base_tokens
        )
        accepted, pairs = compare_witnesses(
            patch.base,
            captured.witness,
            operation,
            required=tuple(affected),
            fixed=tuple(fixed),
        )
        if not accepted:
            raise Rejected(
                "rdf.patch_precondition_failed",
                "The supplied snapshot does not satisfy the patch base and binding "
                "precondition",
            )
        actual_tokens = blank_tokens(captured.witness, operation)
        target_tokens = blank_tokens(patch.target, operation)
        fresh: dict[str, PatchBlank] = {}

        def mapped(
            term: PatchValue | None, *, removal: bool = False
        ) -> PatchValue | None:
            """Resolve retained references and allocate explicitly fresh identities."""
            if not isinstance(term, PatchBlank):
                return term
            operation.consume("work")
            if term.token in base_tokens:
                if term.token not in pairs:
                    raise PatchUnavailable(
                        "An affected base blank reference was not established"
                    )
                return PatchBlank(pairs[term.token])
            if removal or term.token not in target_tokens:
                raise Rejected(
                    "rdf.patch_removal_missing",
                    "An edit references a blank outside its required base or target",
                )
            if term.token not in fresh:
                operation.consume("states")
                token = "created:" + str(len(fresh))
                while token in actual_tokens:
                    operation.consume("work")
                    token += ":"
                actual_tokens.add(token)
                fresh[term.token] = PatchBlank(token)
            return fresh[term.token]

        def quad(value: PatchQuad, *, removal: bool = False) -> PatchQuad:
            """Own one resolved quad while preserving all original lexical fields."""
            operation.consume("work")
            return PatchQuad(
                cast(PatchResource, mapped(value.subject, removal=removal)),
                value.predicate,
                cast(PatchValue, mapped(value.value, removal=removal)),
                cast(PatchResource | None, mapped(value.graph, removal=removal)),
            )

        removals = tuple(quad(value, removal=True) for value in patch.removals)
        additions = tuple(quad(value) for value in patch.additions)
        removed_graphs = tuple(
            cast(PatchResource, mapped(value, removal=True))
            for value in patch.removed_graphs
        )
        added_graphs = tuple(
            cast(PatchResource, mapped(value)) for value in patch.added_graphs
        )
        witness = edited(
            captured.witness,
            removals,
            additions,
            removed_graphs,
            added_graphs,
            operation,
        )
        witness_tokens = blank_tokens(witness, operation)
        operation.consume("work", len(pairs) + len(fresh))
        operation.consume("states", len(pairs) + len(fresh))
        retained = []
        for original, actual_token in pairs.items():
            if original in target_tokens:
                if actual_token not in witness_tokens:
                    raise Rejected(
                        "rdf.patch_postcondition_failed",
                        "The target requires an established base blank "
                        "removed by edits",
                    )
                retained.append((actual_token, original))
        retained.extend((actual.token, token) for token, actual in fresh.items())
        accepted, _ = compare_witnesses(
            witness, patch.target, operation, fixed=tuple(retained)
        )
        if not accepted:
            raise Rejected(
                "rdf.patch_postcondition_failed",
                "The target contradicts established retained or newly allocated "
                "blank identities",
            )
        prospective = _materialize(witness, base, operation)
        actual = capture_source(prospective, "result:", operation)
        accepted, _ = compare_witnesses(actual.witness, patch.target, operation)
        if not accepted:
            raise Rejected(
                "rdf.patch_postcondition_failed",
                "The materialized candidate does not reconstruct the requested "
                "target snapshot",
            )
        operation.consume("work", 0)
        candidate = prospective
    except LimitExceeded as error:
        issues.exhausted(str(error), location="rdf.patch")
    except (PatchUnavailable, ResourceError) as error:
        try:
            issues.append(Issue("rdf.patch_unverified", str(error), kind="unverified"))
        except LimitExceeded as limit:
            issues.exhausted(str(limit), location="rdf.patch")
    except Rejected as error:
        try:
            issues.append(Issue(error.code, str(error), kind="input_error"))
        except LimitExceeded as limit:
            issues.exhausted(str(limit), location="rdf.patch")
    return Transformation(
        candidate,
        report("rdf.patch_application", issues, complete=candidate is not None),
    )
