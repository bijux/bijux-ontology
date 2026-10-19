"""Faithful read-only query and SHACL-SPARQL application-key projections."""

from collections.abc import Sequence

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

from bijux_ontology.data.query_rules.types import QueryRule
from bijux_ontology.declarations.keys import KeyContract
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.paths.traversal import finite_path_steps
from bijux_ontology.rdf.paths.types import capture_path
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.sources.loading import prepare_rdf

from .types import KeyProjection

_SH = Namespace("http://www.w3.org/ns/shacl#")


def _text(parts: Sequence[str], budget: Budget, separator: str = "") -> str:
    """Reserve bounded generated syntax before joining or retaining it."""
    size = sum(len(part) for part in parts) + len(separator) * max(0, len(parts) - 1)
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Generated key query exceeds max_bytes")
    budget.consume("work", size + len(parts))
    budget.consume("bytes", 4 * size)
    value = separator.join(parts)
    if len(value.encode("utf-8")) > budget.limits.max_bytes:
        raise LimitExceeded("Generated key query exceeds max_bytes")
    return value


def _identity(left: str, right: str, budget: Budget) -> str:
    """Express RDF 1.1 string identity despite optional xsd:string metadata."""
    return _text(
        (
            "(sameTerm(",
            left,
            ",",
            right,
            ") || (isLiteral(",
            left,
            ") && isLiteral(",
            right,
            ") && DATATYPE(",
            left,
            ") = <http://www.w3.org/2001/XMLSchema#string> && DATATYPE(",
            right,
            ") = <http://www.w3.org/2001/XMLSchema#string> && STR(",
            left,
            ") = STR(",
            right,
            ")))",
        ),
        budget,
    )


def _pattern(
    start: str,
    end: str,
    steps: tuple[tuple[URIRef, bool], ...],
    stem: str,
    budget: Budget,
) -> str:
    """Render finite steps with explicit RDF identity at intermediate joins.

    Separate adjacent variables preserve the RDF 1.1 identity of plain and
    explicitly typed strings without relying on physical RDFLib term indexes.
    """
    budget.consume("states", 3 * len(steps))
    budget.consume("bytes", 128 * len(steps))
    parts: list[str] = []
    previous = start
    for index, (predicate, inverse) in enumerate(steps):
        budget.consume("work")
        incoming = start if index == 0 else f"?{stem}_i{index}"
        outgoing = end if index == len(steps) - 1 else f"?{stem}_n{index}"
        if index:
            parts.append(
                _text(("FILTER(", _identity(previous, incoming, budget), ") "), budget)
            )
        subject, value = (outgoing, incoming) if inverse else (incoming, outgoing)
        parts.append(_text((subject, " <", str(predicate), "> ", value, " . "), budget))
        previous = outgoing
    return _text(parts, budget)


def project_record_key(
    contract: KeyContract,
    subjects: Sequence[URIRef],
    *,
    scope: QueryScope,
    complete: bool,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> KeyProjection:
    """Project named focus records in one graph with exact singleton RDF keys.

    The shapes expect the caller to select the same data graph before execution.
    Query rules carry that exact selection. This projection does not merge source
    records, normalize business strings, approximate OWL values or prove that a
    caller-supplied corpus is complete. Its qualification report remains required
    beside raw backend reports; incomplete scope can never be waived by a pass.
    """
    if not isinstance(contract, KeyContract) or not isinstance(scope, QueryScope):
        raise InputError("Expected typed key contract and query scope")
    if not isinstance(subjects, Sequence) or isinstance(subjects, str | bytes):
        raise InputError("Key projection requires finite named RDF subjects")
    if type(complete) is not bool:
        raise InputError("Declare whether the projected corpus is complete")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Projection limits conflict with the shared Budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    rules: list[QueryRule] = []
    shapes = None
    checked = 0
    finished = True
    count = len(subjects)
    anchor = ""
    try:
        bound_text(contract.name, operation)
        anchor = contract.name
        bound_text(scope.kind, operation)
        if scope.graph_name is not None:
            bound_text(str(scope.graph_name), operation)
        operation.consume("states", count)
        targets = []
        seen: set[URIRef] = set()
        for index in range(count):
            operation.consume("work")
            subject = subjects[index]
            if not isinstance(subject, URIRef):
                raise InputError("Projected keys require named RDF subjects")
            bound_text(str(subject), operation)
            absolute_iri(str(subject))
            if subject in seen:
                raise InputError("Projected key targets must be unique")
            seen.add(subject)
            targets.append(subject)
        supported = scope.kind in ("default", "named") and not isinstance(
            scope.graph_name, BNode
        )
        supported = (
            supported
            and contract.absence == "reject"
            and contract.multiplicity == "reject"
        )
        paths = []
        finite_paths = []
        for component in contract.components:
            captured = capture_path(component.path, budget=operation)
            supported = (
                supported
                and component.equality == "rdf-term"
                and component.normalization == "none"
            )
            finite = finite_path_steps(captured, operation)
            if finite is None:
                supported = False
            finite_paths.append(finite)
            # Captured metadata was bounded before rendering recursive syntax.
            paths.append(captured.readable)
        if not supported:
            issues.append(
                Issue(
                    "data.key_projection_unverified",
                    "Projection requires named singleton RDF keys in one graph",
                    contract.name,
                    kind="unverified",
                )
            )
        elif not targets:
            issues.append(
                Issue(
                    "data.key_projection_unverified",
                    "No required projected record targets were supplied",
                    contract.name,
                    kind="unverified",
                )
            )
        else:
            graph = Graph(bind_namespaces="none")
            shape = BNode()

            def add(
                subject: URIRef | BNode,
                predicate: URIRef,
                value: URIRef | BNode | Literal,
            ) -> None:
                """Charge each generated statement before adding owned storage."""
                operation.consume("work")
                operation.consume("triples")
                operation.consume("states")
                operation.consume("bytes", 256)
                graph.add((subject, predicate, value))

            add(shape, RDF.type, _SH.NodeShape)
            for target in targets:
                add(shape, _SH.targetNode, target)
            operation.consume("states", 2 * len(targets))
            names = [_text(("<", str(target), ">"), operation) for target in targets]
            membership = _text(("(", _text(names, operation, ","), ")"), operation)
            bindings = _text(
                (
                    "{",
                    _text(
                        [
                            _text(("{ BIND(", name, " AS ?this) }"), operation)
                            for name in names
                        ],
                        operation,
                        " UNION ",
                    ),
                    "}",
                ),
                operation,
            )

            def constraint(
                name: str, body: str, shacl_body: str, variables: str = "?this"
            ) -> None:
                """Share faithful WHERE conditions; SHACL supplies its own focus."""
                query = _text(
                    ("SELECT ", variables, " WHERE { ", bindings, " ", body, " }"),
                    operation,
                )
                shacl_query = _text(
                    ("SELECT ", variables, " WHERE { ", shacl_body, " }"), operation
                )
                operation.consume("states", 2)
                rules.append(
                    QueryRule(
                        _text((contract.name, ".", name), operation), query, scope
                    )
                )
                node = BNode()
                add(shape, _SH.sparql, node)
                add(node, _SH.select, Literal(shacl_query, normalize=False))
                add(node, _SH.message, Literal("Application key " + name))

            for index, (path, steps) in enumerate(
                zip(paths, finite_paths, strict=True)
            ):
                assert steps is not None
                constraint(
                    str(index) + ".absent",
                    _text(
                        (
                            "FILTER NOT EXISTS { ",
                            _pattern("?this", "?value", steps, "v", operation),
                            " }",
                        ),
                        operation,
                    ),
                    _text(("FILTER NOT EXISTS { ?this ", path, " ?value }"), operation),
                )
                unequal = _text(
                    ("FILTER(!", _identity("?a", "?b", operation), ")"), operation
                )
                constraint(
                    str(index) + ".multiple",
                    _text(
                        (
                            _pattern("?this", "?a", steps, "a", operation),
                            _pattern("?this", "?b", steps, "b", operation),
                            unequal,
                        ),
                        operation,
                    ),
                    _text(
                        ("?this ", path, " ?a . ?this ", path, " ?b . ", unequal),
                        operation,
                    ),
                )
            body = []
            shacl_body = []
            for index, (path, steps) in enumerate(
                zip(paths, finite_paths, strict=True)
            ):
                assert steps is not None
                left, right = f"?left{index}", f"?right{index}"
                equal = _text(
                    ("FILTER(", _identity(left, right, operation), ") "), operation
                )
                body.append(
                    _text(
                        (
                            _pattern("?this", left, steps, "l" + str(index), operation),
                            _pattern(
                                "?other", right, steps, "r" + str(index), operation
                            ),
                            equal,
                        ),
                        operation,
                    )
                )
                shacl_body.append(
                    _text(
                        (
                            "?this ",
                            path,
                            " ",
                            left,
                            " . ?other ",
                            path,
                            " ",
                            right,
                            " . ",
                            equal,
                        ),
                        operation,
                    )
                )
            selection = _text(
                ("FILTER(?other IN ", membership, ") FILTER(!sameTerm(?this,?other))"),
                operation,
            )
            body.append(selection)
            shacl_body.append(selection)
            constraint(
                "collision",
                _text(body, operation),
                _text(shacl_body, operation),
                "?this ?other",
            )
            shapes = prepare_rdf(graph, budget=operation)
            checked = len(targets)
        if not complete:
            issues.append(
                Issue(
                    "data.key_corpus_unverified",
                    "The projected record corpus is not declared complete",
                    contract.name,
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        finished = False
        issues.exhausted(str(error), location=anchor)
    return KeyProjection(
        Report(
            "record-key-projection",
            checked,
            tuple(issues),
            requested=count,
            complete=finished,
            regime="application-keys",
            families=("application-keys",),
        ),
        tuple(rules),
        shapes,
    )
