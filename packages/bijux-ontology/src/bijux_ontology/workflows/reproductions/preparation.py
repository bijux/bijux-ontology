"""Capture finite public request inputs without callbacks, paths or implicit fetch."""

from collections.abc import Sequence
from typing import TypeVar

from rdflib import BNode, Graph, URIRef

from bijux_ontology.declarations.codecs import models_from_data
from bijux_ontology.documents.patches.operations import snapshot
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.codecs import expectations_from_data
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.codecs import usages_from_data
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.patches.planning import Captured, capture_items, capture_source
from bijux_ontology.rdf.patches.types import PatchBlank, PatchResource
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, bound_text

from .types import (
    ContextRequest,
    EmissionRequest,
    JsonResource,
    RdfResource,
    ReproductionUnavailable,
    ShaclRequest,
    SkosRequest,
)

_T = TypeVar("_T", JsonResource, RdfResource)


def reserve(budget: Budget, count: int = 1) -> None:
    """Reserve controlled traversal and retained immutable/reference storage."""
    budget.consume("work", count)
    budget.consume("states", count)
    budget.consume("bytes", 96 * count)


def _budget(limits: Limits | None, budget: Budget | None) -> Budget:
    if (
        limits is not None
        and not isinstance(limits, Limits)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Reproduction preparation requires Limits and Budget")
    if budget is not None and limits is not None and limits != budget.limits:
        raise InputError("Reproduction limits conflict with the shared Budget")
    return budget if budget is not None else Budget(limits)


def _iri(value: str | None, budget: Budget) -> None:
    if value is not None:
        bound_text(value, budget)
        absolute_iri(value)


def json_resource(
    iri: str,
    document: object,
    *,
    effective_url: str | None = None,
    context_url: str | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> JsonResource:
    """Own an explicit offline JSON response and its independent URL metadata."""
    operation = _budget(limits, budget)
    if type(iri) is not str:
        raise InputError("JSON response lookup requires an absolute IRI")
    for value in (iri, effective_url, context_url):
        _iri(value, operation)
    _, content = snapshot(document, operation)
    reserve(operation)
    return JsonResource._owned(
        iri=iri,
        document_json=content,
        effective_url=effective_url,
        context_url=context_url,
    )


def rdf_resource(
    iri: str,
    content: bytes,
    *,
    format: str,
    document_iri: str | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> RdfResource:
    """Capture supplied RDF bytes; parsing/import semantics stay with native checks."""
    operation = _budget(limits, budget)
    if type(iri) is not str:
        raise InputError("RDF response lookup requires an absolute IRI")
    for value in (iri, document_iri):
        _iri(value, operation)
    bound_text(format, operation)
    if type(content) is not bytes or not format:
        raise InputError("RDF resources require bytes and an explicit syntax")
    if len(content) > operation.limits.max_bytes:
        raise LimitExceeded("RDF reproduction resource exceeds max_bytes")
    operation.consume("bytes", len(content))
    operation.consume("work", len(content))
    reserve(operation)
    return RdfResource._owned(
        iri=iri, content=content, format=format, document_iri=document_iri
    )


def capture_resources(
    values: Sequence[_T], kind: type[_T], budget: Budget
) -> tuple[_T, ...]:
    """Capture immutable registry entries before a native loader can consume them."""
    captured = capture_items(values, kind, budget)
    if len(captured) > budget.limits.max_resources:
        raise LimitExceeded("Reproduction registry exceeds max_resources")
    seen = set()
    for value in captured:
        iri = value.iri
        _iri(iri, budget)
        reserve(budget)
        if iri in seen:
            raise InputError("Reproduction registry contains duplicate lookup IRIs")
        seen.add(iri)
    return captured


def prepare_context_request(
    document: object,
    expectations: object,
    *,
    resources: Sequence[JsonResource] = (),
    base: str | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ContextRequest:
    """Own the ordinary public JSON expectation format and mandatory source paths."""
    operation = _budget(limits, budget)
    _iri(base, operation)
    _, document_json = snapshot(document, operation)
    expectation_data, expectations_json = snapshot(expectations, operation)
    if not expectations_from_data(expectation_data):
        raise InputError("Context reproduction needs nonempty required expectations")
    registry = capture_resources(resources, JsonResource, operation)
    reserve(operation)
    return ContextRequest._owned(
        document_json=document_json,
        expectations_json=expectations_json,
        resources=registry,
        base=base,
    )


def prepare_emission_request(
    document: object,
    models: object,
    usages: object,
    *,
    resources: Sequence[JsonResource] = (),
    base: str | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> EmissionRequest:
    """Capture public model declarations and mandatory ordinary source bindings."""
    operation = _budget(limits, budget)
    _iri(base, operation)
    _, document_json = snapshot(document, operation)
    model_data, models_json = snapshot(models, operation)
    usage_data, usages_json = snapshot(usages, operation)
    declarations = models_from_data(model_data)
    bindings = usages_from_data(usage_data)
    if not declarations or not bindings:
        raise InputError("Emission reproduction requires nonempty models and usages")
    if any(binding.reference is not None or binding.entity for binding in bindings):
        raise ReproductionUnavailable(
            "Entity-joined model bindings are outside the source reduction profile"
        )
    registry = capture_resources(resources, JsonResource, operation)
    reserve(operation)
    return EmissionRequest._owned(
        document_json=document_json,
        models_json=models_json,
        usages_json=usages_json,
        resources=registry,
        base=base,
    )


def _capture(source: PreparedSource | Graph, prefix: str, budget: Budget) -> Captured:
    if not isinstance(source, PreparedSource | Graph):
        raise InputError("RDF reproduction requires owned RDF or an ordinary Graph")
    prepared = (
        source
        if isinstance(source, PreparedSource)
        else prepare_rdf(source, require_complete_graphs=True, budget=budget)
    )
    captured = capture_source(prepared, prefix, budget)
    if captured.witness.kind == "dataset":
        raise ReproductionUnavailable(
            "Select one physical RDF graph before reproduction"
        )
    return captured


def prepare_skos_request(
    source: PreparedSource | Graph,
    *,
    concepts: Sequence[str],
    rules: Sequence[str],
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> SkosRequest:
    """Require explicit named concept roots and native rule names."""
    operation = _budget(limits, budget)
    roots = capture_items(concepts, str, operation)
    selected_rules = capture_items(rules, str, operation)
    if not roots or not selected_rules:
        raise InputError(
            "SKOS reproduction requires explicit nonempty concepts and rules"
        )
    for value in roots:
        _iri(value, operation)
    for value in selected_rules:
        bound_text(value, operation)
    captured = _capture(source, "skos", operation)
    reserve(operation)
    return SkosRequest._owned(
        source=captured.witness, concepts=roots, rules=selected_rules
    )


def _bindings(
    values: Sequence[URIRef | BNode], captured: Captured, budget: Budget
) -> tuple[PatchResource, ...]:
    result: list[PatchResource] = []
    for term in capture_items(values, object, budget):
        if not isinstance(term, URIRef | BNode):
            raise InputError(
                "Explicit SHACL roots require named or actual owned blank terms"
            )
        bound_node(term, budget)
        reserve(budget)
        result.append(captured.reference(term) if isinstance(term, BNode) else term)
    if not result:
        raise InputError(
            "SHACL reproduction requires explicit nonempty focus/shape pairs"
        )
    return tuple(result)


def prepare_shacl_request(
    data: PreparedSource | Graph,
    shapes: PreparedSource | Graph,
    *,
    focus_nodes: Sequence[URIRef | BNode],
    use_shapes: Sequence[URIRef | BNode],
    imports: Sequence[RdfResource] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ShaclRequest:
    """Capture supplied Core SHACL pair roots; trusted extensions remain unsupported."""
    operation = _budget(limits, budget)
    data_capture, shape_capture = (
        _capture(data, "data", operation),
        _capture(shapes, "shapes", operation),
    )
    focus = _bindings(focus_nodes, data_capture, operation)
    if any(isinstance(term, PatchBlank) for term in focus):
        raise ReproductionUnavailable(
            "Blank SHACL focus relocation is outside the named-focus reduction profile"
        )
    selected_shapes = _bindings(use_shapes, shape_capture, operation)
    registry = capture_resources(imports, RdfResource, operation)
    reserve(operation)
    return ShaclRequest._owned(
        data=data_capture.witness,
        shapes=shape_capture.witness,
        focus_nodes=focus,
        use_shapes=selected_shapes,
        imports=registry,
    )
