"""Finite rule-program qualification and exact snapshot transport."""

from decimal import Decimal

from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, SH, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.patches.types import (
    PatchBlank,
    PatchLiteral,
    PatchValue,
    SnapshotWitness,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_term
from bijux_ontology.shacl.functions import (
    FunctionPlan,
    ShaclFunctionCatalog,
    prepare_functions,
)
from bijux_ontology.shacl.profiles.extraction import extract_profile
from bijux_ontology.shacl.queries import _CASTS, analyze_query


class RuleUnavailable(InputError):
    """Required rule semantics exceed this explicitly supported finite profile."""


def selected_names(values: object, budget: Budget) -> tuple[str, ...]:
    """Capture a finite indexed IRI inventory before hashing or reporting it."""
    from collections.abc import Sequence

    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Rule owners require an explicit IRI sequence")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    names: list[str] = []
    seen: set[str] = set()
    for index in range(count):
        budget.consume("work")
        try:
            name = values[index]
        except IndexError as error:
            raise InputError(
                "Rule selections changed during indexed capture"
            ) from error
        if not isinstance(name, str) or isinstance(name, BNode | Literal):
            raise InputError("Rule owner selections require named IRIs")
        bound_text(name, budget)
        name = absolute_iri(str(name))
        if name in seen:
            raise InputError("Rule owner selections must be distinct")
        seen.add(name)
        names.append(name)
    if not names:
        raise RuleUnavailable("Rules require a nonempty explicit owner selection")
    return tuple(names)


def single_scope(witness: SnapshotWitness) -> None:
    """Exclude aggregate and anonymous partitions without inventing correspondence."""
    if witness.kind == "dataset" or isinstance(witness.graph_selection, PatchBlank):
        raise RuleUnavailable(
            "Rules require one plain or explicitly pinned named/default graph"
        )


def graph_from_witness(witness: SnapshotWitness, budget: Budget) -> Graph:
    """Decode existing exact term records into one private maintained graph."""
    single_scope(witness)
    graph = Graph(bind_namespaces="none")
    blanks: dict[str, BNode] = {}

    def node(value: PatchValue) -> Node:
        """Preserve joint blank identity only inside this one decoded role."""
        budget.consume("work")
        if isinstance(value, PatchBlank):
            if value.token not in blanks:
                budget.consume("states")
                budget.consume("bytes", 64)
                blanks[value.token] = BNode()
            return blanks[value.token]
        if isinstance(value, PatchLiteral):
            return literal_term(
                value.lexical, datatype=value.datatype, language=value.language
            )
        return value

    for quad in witness.quads:
        budget.consume("triples")
        budget.consume("states")
        graph.add((node(quad.subject), node(quad.predicate), node(quad.value)))
    return graph


def namespaces(source: PreparedSource, budget: Budget) -> list[list[str]]:
    """Retain the actual supplied query prefix environment without fetching it."""
    result = []
    for prefix, namespace in source._read(budget=budget).namespaces():
        budget.consume("work")
        budget.consume("states")
        bound_text(prefix, budget)
        bound_node(namespace, budget)
        result.append([prefix, str(namespace)])
    return result


def bind_namespaces(graph: Graph, value: object, budget: Budget) -> None:
    """Validate finite typed namespace pairs before maintained query parsing."""
    if not isinstance(value, list):
        raise InputError("Malformed rule namespace inventory")
    budget.consume("states", len(value))
    for pair in value:
        budget.consume("work")
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(v, str) for v in pair)
        ):
            raise InputError("Malformed rule namespace entry")
        for text in pair:
            bound_text(text, budget)
        graph.bind(pair[0], URIRef(absolute_iri(pair[1])), replace=True)


def _order(graph: Graph, node: Node, budget: Budget) -> Decimal:
    """Read exact orders without numeric promotion or equal-order ambiguity."""
    values = []
    for value in graph.objects(node, SH.order):
        budget.consume("work")
        bound_node(value, budget)
        values.append(value)
        if len(values) > 1:
            raise InputError("A rule or owner has multiple orders")
    if not values:
        return Decimal(0)
    value = values[0]
    if (
        not isinstance(value, Literal)
        or value.datatype not in (XSD.integer, XSD.decimal)
        or not check_literal(value, budget=budget).verified
    ):
        raise RuleUnavailable(
            "Rule orders require qualified integer or decimal literals"
        )
    order = Decimal(str(value))
    if not order.is_finite():
        raise InputError("Rule order must be finite")
    return order


def _one(graph: Graph, node: Node, predicate: URIRef, budget: Budget) -> Node:
    """Reject missing or ambiguous mandatory rule parameters before execution."""
    values = []
    for value in graph.objects(node, predicate):
        budget.consume("work")
        budget.consume("states")
        values.append(value)
        if len(values) > 1:
            raise InputError("A rule parameter must have exactly one value")
    if len(values) != 1:
        raise InputError("A required rule parameter is absent")
    return values[0]


def qualify_program(
    graph: Graph, names: tuple[str, ...], functions: FunctionPlan, budget: Budget
) -> None:
    """Use maintained shapes, Core extraction and controlled CONSTRUCT analysis."""
    from pyshacl.shape import Shape
    from pyshacl.shapes_graph import ShapesGraph

    forbidden = frozenset(
        (
            SH.target,
            SH.sparql,
            SH.js,
            SH.jsFunctionName,
            SH.jsLibrary,
            SH.jsLibraryURL,
            OWL.imports,
        )
    )
    ambient = frozenset(
        (
            SH.SPARQLFunction,
            SH.JSFunction,
            SH.ConstraintComponent,
            SH.SPARQLTarget,
            SH.SPARQLTargetType,
            SH.JSTarget,
            SH.JSTargetType,
        )
    )
    for subject, predicate, value in graph:
        budget.consume("work")
        for term in (subject, predicate, value):
            bound_node(term, budget)
        if predicate in forbidden or predicate == RDF.type and value in ambient:
            raise RuleUnavailable(
                "Ambient queries, targets, functions, JavaScript "
                "and imports are unsupported"
            )
    supplied = ShapesGraph(graph)
    enabled = frozenset(URIRef(name) for name in functions.names)
    owner_orders: set[Decimal] = set()
    for name in names:
        budget.consume("work")
        owner = URIRef(name)
        if not any(graph.triples((owner, None, None))):
            raise RuleUnavailable("A selected rule owner is absent")
        order = _order(graph, owner, budget)
        if order in owner_orders:
            raise RuleUnavailable(
                "Selected rule owners require distinct explicit execution orders"
            )
        owner_orders.add(order)
        if any(graph.objects(owner, SH.path)):
            raise RuleUnavailable("Rule owners must be node shapes")
        shape = Shape(supplied, owner)
        orders: set[Decimal] = set()
        count = 0
        for rule in graph.objects(owner, SH.rule):
            budget.consume("work")
            budget.consume("states")
            count += 1
            if not isinstance(rule, URIRef | BNode):
                raise InputError("Rule definitions require RDF resources")
            for selected in (owner, rule):
                for flag in graph.objects(selected, SH.deactivated):
                    if (
                        not isinstance(flag, Literal)
                        or flag.datatype != XSD.boolean
                        or not check_literal(flag, budget=budget).verified
                    ):
                        raise InputError("Rule deactivation requires a valid boolean")
                    if str(flag) in ("true", "1"):
                        raise RuleUnavailable("Required rule execution is deactivated")
            order = _order(graph, rule, budget)
            if order in orders:
                raise RuleUnavailable(
                    "Rules of one owner require distinct execution orders"
                )
            orders.add(order)
            kinds = {
                kind
                for kind in graph.objects(rule, RDF.type)
                if kind in (SH.TripleRule, SH.SPARQLRule)
            }
            if len(kinds) != 1:
                raise RuleUnavailable("A rule needs exactly one supported rule kind")
            if SH.TripleRule in kinds:
                for parameter in (SH.subject, SH.predicate, SH.object):
                    term = _one(graph, rule, parameter, budget)
                    if (
                        isinstance(term, BNode)
                        or parameter == SH.subject
                        and not isinstance(term, URIRef)
                        or parameter == SH.predicate
                        and not isinstance(term, URIRef)
                    ):
                        raise RuleUnavailable(
                            "Triple rules support sh:this and constant RDF terms only"
                        )
                if any(graph.objects(rule, SH.construct)):
                    raise InputError("Triple rules cannot also carry a CONSTRUCT")
            else:
                text = _one(graph, rule, SH.construct, budget)
                analysis = analyze_query(shape, rule, text, budget)
                if analysis.form != "ConstructQuery":
                    raise InputError("A SPARQL rule requires a CONSTRUCT query")
                if any(
                    call.iri not in _CASTS and call.iri not in enabled
                    for call in analysis.calls
                ):
                    raise RuleUnavailable(
                        "A CONSTRUCT calls an unavailable explicit function"
                    )
                graph.set((rule, SH.construct, Literal(analysis.text)))
            for condition in graph.objects(rule, SH.condition):
                budget.consume("work")
                if not isinstance(condition, URIRef | BNode):
                    raise InputError("Rule conditions require Core shape resources")
                dataset = Dataset()
                budget.consume("triples", len(graph))
                for triple in graph:
                    budget.consume("work")
                    dataset.default_graph.add(triple)
                owned = PreparedSource._from_owned(
                    dataset,
                    is_dataset=False,
                    document_iri=None,
                    effective_url=None,
                    literal_fidelity="supplied_terms",
                )
                profile = extract_profile(owned, (condition,), budget=budget)
                if not profile.structural_report.verified:
                    raise RuleUnavailable(
                        "Rule conditions require a complete finite Core profile"
                    )
        if not count:
            raise RuleUnavailable("A required selected owner has no rules")

    selected_owners = frozenset(URIRef(name) for name in names)
    discard = []
    for triple in graph.triples((None, SH.rule, None)):
        budget.consume("work")
        if triple[0] not in selected_owners:
            budget.consume("states")
            discard.append(triple)
    for triple in discard:
        budget.consume("work")
        graph.remove(triple)


def function_plan(
    graph: Graph | None, names: tuple[str, ...], budget: Budget
) -> FunctionPlan:
    """Reuse the explicit maintained catalog, retaining unavailable capabilities."""
    if graph is None:
        return FunctionPlan(Graph(bind_namespaces="none"))
    plan = prepare_functions(
        ShaclFunctionCatalog(graph, selected=names, trusted=True),
        budget.limits,
        budget=budget,
    )
    if any(issue.kind == "exhausted" for issue in plan.issues):
        raise LimitExceeded("Function qualification exhausted the operation allowance")
    if plan.issues:
        raise RuleUnavailable(
            "The explicit function catalog is incomplete or unsupported"
        )
    budget.consume("work", len(plan.graph))
    budget.consume("triples", len(plan.graph))
    budget.consume("states", len(plan.names))
    return plan
