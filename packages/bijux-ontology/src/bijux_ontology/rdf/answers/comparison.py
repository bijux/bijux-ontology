"""Result bag identity through maintained RDF isomorphism and explicit value proofs."""

import time
from threading import Event
from typing import Literal as Choice
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.terms import bound_node, literal_term, term_key
from bijux_ontology.rdf.value_comparison.comparing import compare_literal_values

from .types import AnswerArtifact, AnswerSemantics

_BASE = "urn:bijux:ontology:answer:"


def _graph(
    artifact: AnswerArtifact, semantics: AnswerSemantics, budget: Budget
) -> Graph:
    """Encode distinct row/cell/result-blank roles without collapsing a result bag."""
    count = len(artifact.variables)
    budget.consume("states", 3 * count + 1)
    budget.consume("bytes", 128 * (count + 1))
    budget.consume("work", count * max(1, count.bit_length()))
    names = sorted(artifact.variables)
    indices = {name: i for i, name in enumerate(artifact.variables)}
    graph = Graph()

    def add(subject: Node, predicate: Node, value: Node) -> None:
        """Pay graph insertion and control storage before mutation."""
        budget.consume("work")
        budget.consume("states")
        budget.consume("triples")
        budget.consume("bytes", 192)
        graph.add((subject, predicate, value))

    for i, name in enumerate(names):
        bound_text(name, budget)
        add(
            URIRef(_BASE + "header"),
            URIRef(_BASE + "column:" + str(i)),
            literal_term(name),
        )
    seen: set[tuple[object, ...]] = set()
    blanks: dict[str, BNode] = {}
    ordinal = 0
    for row in artifact.rows:
        budget.consume("states", len(row) + 1)
        budget.consume("bytes", 128 * (len(row) + 1))
        for cell in row:
            if cell is not None:
                bound_node(cell, budget)
                budget.consume(
                    "bytes",
                    8
                    * (
                        len(str(cell))
                        + len(str(getattr(cell, "datatype", "") or ""))
                        + len(str(getattr(cell, "language", "") or ""))
                        + 64
                    ),
                )
        ordered = tuple(row[indices[name]] for name in names)
        key = tuple(term_key(cell) if cell is not None else None for cell in ordered)
        if semantics == "set":
            if key in seen:
                continue
            seen.add(key)
        root = BNode()
        add(root, RDF.type, URIRef(_BASE + "Row"))
        if semantics == "sequence":
            add(
                root,
                URIRef(_BASE + "ordinal"),
                literal_term(str(ordinal), datatype=str(XSD.integer)),
            )
        ordinal += 1
        for i, cell in enumerate(ordered):
            slot = BNode()
            add(root, URIRef(_BASE + "column:" + str(i)), slot)
            add(
                slot, RDF.type, URIRef(_BASE + ("Unbound" if cell is None else "Bound"))
            )
            if cell is not None:
                if isinstance(cell, BNode):
                    cell = blanks.setdefault(str(cell), BNode())
                    add(cell, RDF.type, URIRef(_BASE + "ResultBlank"))
                add(slot, URIRef(_BASE + "value"), cell)
    return graph


def _value_rows(
    left: AnswerArtifact,
    right: AnswerArtifact,
    semantics: AnswerSemantics,
    regime: Choice["sparql-1.1", "owl2-datatype-map"],
    row_pairs: tuple[tuple[int, int], ...] | None,
    blank_pairs: tuple[tuple[BNode, BNode], ...],
    operation: Operation,
    issues: IssueCollector,
    children: list[Report],
) -> None:
    """Check caller correspondence without guessing value or blank matching."""
    if row_pairs is None:
        if semantics != "sequence":
            raise InputError("Value bag/set comparison requires explicit row_pairs")
        if len(left.rows) != len(right.rows):
            issues.append(Issue("rdf.answer_changed", "Answer row counts differ"))
            return
        operation.consume("states", len(left.rows))
        row_pairs = tuple((i, i) for i in range(len(left.rows)))
    if type(row_pairs) is not tuple or type(blank_pairs) is not tuple:
        raise InputError("Correspondences require immutable tuples")
    operation.consume("states", len(row_pairs) * 3 + len(blank_pairs) * 3 + 2)
    operation.consume("bytes", 128 * (len(row_pairs) + len(blank_pairs) + 1))
    a_seen: set[int] = set()
    b_seen: set[int] = set()
    for pair in row_pairs:
        operation.consume("work")
        if type(pair) is not tuple or len(pair) != 2:
            raise InputError("Malformed answer row correspondence")
        a, b = pair
        if (
            type(a) is not int
            or type(b) is not int
            or not 0 <= a < len(left.rows)
            or not 0 <= b < len(right.rows)
        ):
            raise InputError("Answer row correspondence is outside its artifact")
        if semantics != "set" and (a in a_seen or b in b_seen):
            raise InputError("Bag row correspondence must be bijective")
        if semantics == "sequence" and a != b:
            raise InputError("Sequence row correspondence must preserve positions")
        a_seen.add(a)
        b_seen.add(b)
    if len(a_seen) != len(left.rows) or len(b_seen) != len(right.rows):
        raise InputError("Value correspondence must cover both complete tables")
    owned: list[dict[int, BNode]] = []
    for artifact in (left, right):
        inventory: dict[int, BNode] = {}
        for row in artifact.rows:
            for cell in row:
                operation.consume("work")
                if cell is not None:
                    bound_node(cell, operation)
                if isinstance(cell, BNode):
                    operation.consume("states")
                    inventory[id(cell)] = cell
        owned.append(inventory)
    bindings: dict[int, int] = {}
    targets: set[int] = set()
    for blank_pair in blank_pairs:
        operation.consume("work")
        if type(blank_pair) is not tuple or len(blank_pair) != 2:
            raise InputError("Malformed answer blank correspondence")
        blank_a, blank_b = blank_pair
        if (
            owned[0].get(id(blank_a)) is not blank_a
            or owned[1].get(id(blank_b)) is not blank_b
            or id(blank_a) in bindings
            or id(blank_b) in targets
        ):
            raise InputError(
                "Blank correspondence requires actual distinct artifact references"
            )
        bindings[id(blank_a)] = id(blank_b)
        targets.add(id(blank_b))
    if set(bindings) != set(owned[0]) or targets != set(owned[1]):
        issues.append(
            Issue(
                "rdf.answer_correspondence_unverified",
                "Value mode requires complete explicit global blank correspondence",
                kind="unverified",
            )
        )
        return
    operation.consume(
        "states", len(right.variables) + len(row_pairs) * len(left.variables) + 1
    )
    operation.consume(
        "bytes", 256 * (len(right.variables) + len(row_pairs) * len(left.variables) + 1)
    )
    positions = {v: i for i, v in enumerate(right.variables)}
    for a, b in row_pairs:
        for i, name in enumerate(left.variables):
            operation.consume("work")
            x, y = left.rows[a][i], right.rows[b][positions[name]]
            if isinstance(x, Literal) and isinstance(y, Literal):
                result = compare_literal_values(x, y, regime=regime, budget=operation)
                children.append(result.report)
                retain_report(result.report, operation, issues)
                if result.relation == "indeterminate":
                    continue
                equal = result.relation == "equal"
            elif isinstance(x, BNode) and isinstance(y, BNode):
                equal = bindings.get(id(x)) == id(y)
            else:
                equal = (
                    term_key(x) == term_key(y)
                    if x is not None and y is not None
                    else x is y
                )
            if not equal:
                issues.append(
                    Issue(
                        "rdf.answer_changed",
                        "Mapped answer cells differ",
                        f"rows/{a}/{name}",
                    )
                )


def compare_answers(
    left: AnswerArtifact,
    right: AnswerArtifact,
    *,
    semantics: AnswerSemantics = "bag",
    regime: Choice["rdf-term", "sparql-1.1", "owl2-datatype-map"] = "rdf-term",
    row_pairs: tuple[tuple[int, int], ...] | None = None,
    blank_pairs: tuple[tuple[BNode, BNode], ...] = (),
    require_external_complete: bool = False,
    strict_variable_order: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10,
    cancel: Event | None = None,
) -> Report:
    """Compare finite artifacts, independently of external query execution truth.

    Exact identity uses one global maintained blank bijection, without requiring
    uniqueness. Value mode checks explicit row correspondence, or aligned sequence
    positions, through the existing named literal regime. No value matcher or
    reasoner is inferred. Variable names align columns; serialized order remains
    available and can be required explicitly.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    checked = 0
    complete = True
    try:
        if type(left) is not AnswerArtifact or type(right) is not AnswerArtifact:
            raise InputError("Comparison requires owned answer artifacts")
        if not isinstance(cast(object, semantics), str) or semantics not in {
            "bag",
            "set",
            "sequence",
        }:
            raise InputError("Select bag, set or sequence answer semantics")
        if not isinstance(cast(object, regime), str) or regime not in {
            "rdf-term",
            "sparql-1.1",
            "owl2-datatype-map",
        }:
            raise InputError("Unsupported answer comparison regime")
        if (
            type(require_external_complete) is not bool
            or type(strict_variable_order) is not bool
        ):
            raise InputError("Answer scope choices require booleans")
        for artifact in (left, right):
            if not artifact.report.verified:
                issues.append(
                    Issue(
                        "rdf.answer_source_unverified",
                        "Input does not establish a complete finite answer artifact",
                        kind="unverified",
                    )
                )
                complete = False
            if require_external_complete and artifact.external_complete is not True:
                issues.append(
                    Issue(
                        "rdf.answer_external_unverified",
                        "External query completeness was not supplied",
                        kind="unverified",
                    )
                )
                complete = False
            for name in artifact.variables:
                bound_text(name, operation)
                operation.consume("states")
        if not complete:
            return Report(
                "rdf.answers.comparison",
                requested=1,
                complete=False,
                issues=tuple(issues),
            )
        if (
            left.kind != right.kind
            or set(left.variables) != set(right.variables)
            or strict_variable_order
            and left.variables != right.variables
        ):
            issues.append(
                Issue("rdf.answer_changed", "Answer kind or variables differ")
            )
        elif left.kind == "ASK":
            if left.answer != right.answer:
                issues.append(Issue("rdf.answer_changed", "ASK booleans differ"))
        elif regime == "rdf-term":
            if row_pairs is not None or blank_pairs:
                raise InputError(
                    "Exact answer comparison finds its own global correspondence"
                )
            before = _graph(left, semantics, operation)
            after = _graph(right, semantics, operation)
            remaining = operation.deadline - time.monotonic()
            if remaining <= 0:
                raise LimitExceeded("Answer comparison exceeded its deadline")
            proof = compare_rdf(
                before,
                after,
                budget=operation,
                timeout_seconds=remaining,
                cancel=cancel,
            )
            children.append(proof)
            retain_report(proof, operation, issues)
        else:
            _value_rows(
                left,
                right,
                semantics,
                regime,
                row_pairs,
                blank_pairs,
                operation,
                issues,
                children,
            )
        checked = 1
    except LimitExceeded as error:
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error))
        complete = False
    except InputError as error:
        try:
            issues.append(Issue("rdf.answer_input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
        complete = False
    return Report(
        "rdf.answers.comparison",
        checked,
        tuple(issues),
        requested=1,
        complete=complete,
        regime=regime,
        children=tuple(children),
        exclusions=()
        if require_external_complete
        else ("external-query-completeness",),
    )
