"""Compile reusable typed definitions and explicit uses into ordinary SHACL RDF."""

import sys
import threading
from collections.abc import Sequence

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, SH, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.literals.checking import check_literal
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_datatype, term_key
from bijux_ontology.shacl.authoring.parameters import parameter_name
from bijux_ontology.shacl.execution.capabilities import ShaclConstraintRequirement
from bijux_ontology.shacl.profiles.resources import _capture, _owned
from bijux_ontology.shacl.profiles.types import Triple

from .types import ComponentShapes, ComponentUse, ShaclComponent

_RESERVED = frozenset(("this", "shapesGraph", "currentShape", "path", "PATH", "value"))
_ROLES = {
    "generic": SH.validator,
    "node": SH.nodeValidator,
    "property": SH.propertyValidator,
}


def _iri(value: str, budget: Budget) -> URIRef:
    """Bound an original identifier before parsing or copying its exact text."""
    if isinstance(value, BNode | Literal):
        raise InputError("Component identifiers require absolute IRIs")
    bound_text(value, budget)
    try:
        return URIRef(absolute_iri(str(value)))
    except InputError as error:
        raise InputError("Component identifiers must be absolute IRIs") from error


def compile_components(
    shapes: PreparedSource,
    components: Sequence[ShaclComponent],
    uses: Sequence[ComponentUse],
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> ComponentShapes:
    """Preserve an explicitly selected source while adding a finite typed catalog.

    Definitions may not shadow existing component parameter ownership. Mandatory
    bindings, validator roles and parameter datatypes are checked before exposing
    an artifact. Optional maintained query qualification is separate from core RDF
    construction. Supplied opaque assertions remain unchanged and make no new
    claim about their executability.
    """
    from .qualification import _qualify

    if not isinstance(shapes, PreparedSource):
        raise InputError("Component compilation requires an owned PreparedSource")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    source = None
    roots: list[URIRef | BNode] = []
    root_set: set[URIRef | BNode] = set()
    requirements: list[ShaclConstraintRequirement] = []
    query_jobs: list[tuple[ComponentUse, ShaclComponent, Node]] = []
    checked = 0
    try:
        if shapes.union or shapes.literal_fidelity not in (
            "source_terms",
            "supplied_terms",
        ):
            raise InputError("Component authoring requires exact selected physical RDF")
        if shapes.graph_name is not None:
            bound_node(shapes.graph_name, operation) if isinstance(
                shapes.graph_name, BNode | URIRef
            ) else bound_text(shapes.graph_name, operation)
        graph = shapes._read(budget=operation)
        triples: list[Triple] = []
        seen: set[Triple] = set()
        owned: set[int] = set()
        for triple in graph:
            operation.consume("work")
            operation.consume("states")
            operation.consume("triples")
            for node in triple:
                bound_node(node, operation)
                if isinstance(node, BNode):
                    owned.add(id(node))
            triples.append(triple)
            seen.add(triple)
        definitions = _capture(components, operation)
        applications = _capture(uses, operation)
        if not definitions or not applications:
            raise InputError("Components require explicit definitions and uses")
        catalog: dict[URIRef, ShaclComponent] = {}
        parameters: dict[URIRef, URIRef] = {}
        validators: dict[tuple[URIRef, str], Node] = {}

        def add(subject: Node, predicate: URIRef, value: Node) -> None:
            """Retain bounded new triples without touching source graph storage."""
            operation.consume("work")
            operation.consume("states")
            operation.consume("triples")
            for term in (subject, predicate, value):
                bound_node(term, operation)
            triple = subject, predicate, value
            if triple not in seen:
                seen.add(triple)
                triples.append(triple)

        for raw in definitions:
            if not isinstance(raw, ShaclComponent):
                raise InputError("Expected typed ShaclComponent definitions")
            component = _iri(raw.iri, operation)
            if component in catalog or (component, None, None) in graph:
                raise InputError(
                    "Component identities cannot shadow existing definitions"
                )
            catalog[component] = raw
            names: set[str] = set()
            if not raw.parameters or all(p.optional for p in raw.parameters):
                raise InputError("A component needs at least one mandatory parameter")
            add(component, RDF.type, SH.ConstraintComponent)
            for parameter in _capture(raw.parameters, operation):
                # The constructor already captured typed immutable parameter records.
                from bijux_ontology.shacl.authoring.parameters import ShaclParameter

                if not isinstance(parameter, ShaclParameter):
                    raise InputError("Expected typed component parameters")
                path = _iri(parameter.path, operation)
                name = parameter_name(str(path))
                if name in _RESERVED or name in names or path in parameters:
                    raise InputError(
                        "Component parameter names/ownership must be unique"
                    )
                if (None, SH.path, path) in graph:
                    for declaration in graph.subjects(SH.path, path):
                        operation.consume("work")
                        if (None, SH.parameter, declaration) in graph:
                            raise InputError(
                                "Parameter already belongs to a supplied component"
                            )
                names.add(name)
                parameters[path] = component
                node = BNode()
                add(component, SH.parameter, node)
                add(node, SH.path, path)
                if parameter.optional:
                    add(node, SH.optional, Literal(True))
                if parameter.datatype is not None:
                    add(node, SH.datatype, _iri(parameter.datatype, operation))
                if parameter.order is not None:
                    operation.consume("bytes", sys.getsizeof(parameter.order))
                    if sys.getsizeof(parameter.order) > operation.limits.max_bytes:
                        raise LimitExceeded(
                            "Component parameter order exceeds max_bytes"
                        )
                    if isinstance(
                        parameter.order, int
                    ) and parameter.order.bit_length() > 3 * min(
                        4096, operation.limits.max_bytes
                    ):
                        raise LimitExceeded(
                            "Component order exceeds bounded integer formatting"
                        )
                    if isinstance(parameter.order, int):
                        length = 1 + parameter.order.bit_length()
                    else:
                        # Decimal's maintained RDF encoder uses fixed notation.
                        # Reserve its coefficient witness before inspecting scale,
                        # then bound fixed output before any exponent expansion.
                        size = sys.getsizeof(parameter.order)
                        operation.consume("work", 4 * size)
                        operation.consume("states", 4 * size)
                        operation.consume("bytes", 32 * size)
                        parts = parameter.order.as_tuple()
                        assert isinstance(parts.exponent, int)
                        length = parts.sign + (
                            len(parts.digits) + parts.exponent
                            if parts.exponent >= 0
                            else max(len(parts.digits), 1 - parts.exponent) + 1
                        )
                    if length > operation.limits.max_bytes:
                        raise LimitExceeded("Component order exceeds max_bytes")
                    operation.consume("work", length)
                    operation.consume("bytes", length)
                    order_term = Literal(parameter.order)
                    if not check_literal(order_term, budget=operation).verified:
                        raise InputError(
                            "Generated component order is not a valid RDF number"
                        )
                    add(node, SH.order, order_term)
            prefix_node = BNode()
            prefix_names: dict[str, URIRef] = {}
            for pair in _capture(raw.prefixes, operation):
                assert isinstance(pair, tuple)
                prefix, namespace = pair
                bound_text(prefix, operation)
                namespace_iri = _iri(namespace, operation)
                if prefix in prefix_names:
                    raise InputError("Duplicate component prefix declarations")
                prefix_names[prefix] = namespace_iri
                declaration = BNode()
                add(prefix_node, SH.declare, declaration)
                add(declaration, SH.prefix, Literal(prefix))
                add(
                    declaration,
                    SH.namespace,
                    Literal(str(namespace_iri), datatype=XSD.anyURI),
                )
            for validator in raw.validators:
                operation.consume("work")
                key = component, validator.role
                if key in validators:
                    raise InputError(
                        "A component needs one unambiguous validator per role"
                    )
                node = BNode()
                validators[key] = node
                query_text = (
                    validator.ask if validator.ask is not None else validator.select
                )
                assert query_text is not None
                bound_text(query_text, operation)
                add(component, _ROLES[validator.role], node)
                add(
                    node,
                    RDF.type,
                    SH.SPARQLAskValidator
                    if validator.ask is not None
                    else SH.SPARQLSelectValidator,
                )
                add(
                    node,
                    SH.ask if validator.ask is not None else SH.select,
                    Literal(query_text),
                )
                if raw.prefixes:
                    add(node, SH.prefixes, prefix_node)
            if not raw.validators:
                raise InputError("A component needs an explicit validator")

        use_keys: set[tuple[Node, URIRef]] = set()
        for raw in applications:
            if not isinstance(raw, ComponentUse):
                raise InputError("Expected typed ComponentUse applications")
            bound_node(raw.shape, operation)
            if isinstance(raw.shape, BNode) and id(raw.shape) not in owned:
                raise InputError("Blank shape use lacks exact source ownership")
            shape = raw.shape
            if (shape, RDF.type, SH.NodeShape) not in graph and (
                shape,
                SH.path,
                None,
            ) not in graph:
                raise InputError(
                    "Component use must identify an existing node/property shape"
                )
            component = _iri(raw.component, operation)
            definition = catalog.get(component)
            if definition is None or (shape, component) in use_keys:
                raise InputError("Component use needs one supplied unique definition")
            use_keys.add((shape, component))
            by_path = {_iri(p.path, operation): p for p in definition.parameters}
            supplied: dict[URIRef, Node] = {}
            for pair in _capture(raw.bindings, operation):
                assert isinstance(pair, tuple)
                path, value = pair
                path = _iri(path, operation)
                bound_node(value, operation)
                if path not in by_path or path in supplied:
                    raise InputError(
                        "Binding requires one declared component parameter"
                    )
                if isinstance(value, BNode) and id(value) not in owned:
                    raise InputError("Anonymous parameter lacks exact source ownership")
                if not isinstance(value, URIRef | Literal | BNode):
                    raise InputError("Component bindings require supported RDF terms")
                supplied[path] = value
            for path, parameter in by_path.items():
                value = supplied.get(path)
                existing = []
                for previous in graph.objects(shape, path):
                    operation.consume("work")
                    existing.append(previous)
                if value is None and len(existing) == 1:
                    value = existing[0]
                if value is None:
                    if not parameter.optional:
                        raise InputError("A mandatory component parameter is unbound")
                    continue
                if len(existing) > 1 or any(
                    term_key(old) != term_key(value) for old in existing
                ):
                    raise InputError(
                        "Component binding conflicts with supplied shape values"
                    )
                if parameter.datatype is not None:
                    if not isinstance(value, Literal) or str(
                        literal_datatype(value)
                    ) != str(parameter.datatype):
                        raise InputError(
                            "Component parameter violates its declared datatype"
                        )
                    lexical = check_literal(value, budget=operation)
                    issues.extend(lexical.issues)
                    if not lexical.verified:
                        raise InputError(
                            "Component parameter lexical qualification failed"
                        )
                add(shape, path, value)
                if isinstance(shape, URIRef):
                    requirements.append(
                        ShaclConstraintRequirement(
                            str(shape), str(path), str(component)
                        )
                    )
            role = "property" if (shape, SH.path, None) in graph else "node"
            validator_node = validators.get(
                (component, role), validators.get((component, "generic"))
            )
            if validator_node is None:
                raise InputError("No applicable validator exists for this shape role")
            query_jobs.append((raw, definition, validator_node))
            if shape not in root_set:
                operation.consume("states")
                root_set.add(shape)
                roots.append(shape)
            checked += 1
        source = _owned(shapes, graph, triples, operation)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue("shacl.component_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    structural = Report(
        "shacl-component-structure",
        checked,
        tuple(issues),
        requested=max(1, checked),
        regime="explicit-shacl-component-authoring",
    )
    compiled = ComponentShapes(
        source, tuple(roots), tuple(requirements), structural, structural
    )
    return (
        _qualify(compiled, query_jobs, operation)
        if source is not None and structural.verified
        else compiled
    )
