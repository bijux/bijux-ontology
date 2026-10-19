"""Closed actual compilation, context processing and positive shape preparation."""

import hashlib
import importlib.metadata
import platform
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from threading import Event
from types import MappingProxyType
from uuid import uuid4

from rdflib import URIRef

from bijux_ontology._jsonschema import _measure
from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.codecs import models_from_data
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.jsonld.contexts.processing import inspect_context
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.resources import OfflineDocumentLoader
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.models.persistence.codecs import json_bytes
from bijux_ontology.models.persistence.identity import _source, capture_inputs
from bijux_ontology.models.persistence.loading import (
    _options,
    compile_model_artifact,
    load_model_artifact,
)
from bijux_ontology.models.persistence.types import PreparationIdentity
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.literals import Regime
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.patches.planning import capture_items
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.reports import Issue, Report
from bijux_ontology.shacl.coverage import ShaclCoverageContract
from bijux_ontology.shacl.session import ShaclSession

from .types import (
    PreparedRevision,
    RevisionPreparation,
    ShapePreparationProbe,
    _RevisionState,
)


def named_terms(values: Sequence[str], operation: Budget) -> tuple[str, ...]:
    """Capture exact named terms before lookup, deduplication or native execution."""
    selected = capture_items(values, str, operation)
    operation.consume("states", len(selected))
    operation.consume("bytes", 64 + 32 * len(selected))
    result = []
    seen = set()
    for value in selected:
        if type(value) not in (str, URIRef):
            raise InputError("Revision terms require ordinary strings or named IRIs")
        bound_text(value, operation)
        operation.consume("bytes", 8 * len(value) + 128)
        iri = str(absolute_iri(value))
        if iri in seen:
            raise InputError("Revision term selections must be distinct")
        seen.add(iri)
        result.append(iri)
    return tuple(result)


def preparation_capacity(operation: Budget, count: int = 1) -> None:
    """Reserve terminal receipts before fallible semantic preparation stages."""
    operation.consume("work", count)
    operation.consume("states", count * 8)
    operation.consume("bytes", count * 512)


def shape_bindings(
    roots: tuple[str, ...], focus: tuple[str, ...], operation: Budget
) -> tuple[tuple[URIRef, ...], tuple[URIRef, ...], ShaclCoverageContract]:
    """Prepay selected pair fanout and repeated maintained IRI qualification."""
    preparation_capacity(
        operation, len(roots) * len(focus) + len(roots) + len(focus) + 1
    )
    characters = len(focus) * sum(map(len, roots)) + len(roots) * sum(map(len, focus))
    operation.consume("work", characters)
    operation.consume("bytes", 8 * characters)
    coverage = ShaclCoverageContract(
        pairs=tuple((shape, node) for shape in roots for node in focus)
    )
    return (
        tuple(URIRef(root) for root in roots),
        tuple(URIRef(node) for node in focus),
        coverage,
    )


def prepare_revision(
    models: Sequence[ModelDeclaration],
    ontology: RdfSource,
    *,
    imports: Mapping[str, RdfSource] | None = None,
    lookup: Sequence[RdfSource] = (),
    context: object | Absent = ABSENT,
    resources: Mapping[str, object] | None = None,
    base: str | None = None,
    regime: Regime = "application",
    rdf_direction: DirectionPolicy | None = None,
    shapes: RdfSource | None = None,
    shape_roots: Sequence[str] = (),
    shape_probes: Sequence[ShapePreparationProbe] = (),
    expected: PreparationIdentity | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> RevisionPreparation:
    """Prepare one complete declared model/context/optional-shape revision.

    Original inputs must be immutable RDF str/bytes descriptors and closed JSON;
    Paths, streams, live graphs and loader callbacks are outside this profile.
    Existing data-only compilation owns declarations, imports, lookup and JSON
    before native processing. Its current engine/input identity and trusted output
    pin qualify actual loading, never a previous submission verdict.

    Supplied context processing and every selected shape's actual positive probe
    are required preparation. Shapes use explicit named roots and named-focus
    pairs with meta-SHACL preflight. Missing/unsupported/failed obligations cannot
    produce an activatable revision. Ordinary core RDF model preparation remains
    usable without optional JSON-LD or SHACL runtimes. Expected identities bind
    the full revision, including selected shapes, probes and installed engines.
    """
    operation = Operation(limits, budget, timeout, cancel)
    starting_bytes = operation._counts.get("bytes", 0)
    issues = IssueCollector(operation)
    reports: list[Report] = []
    prepared = None
    finished = False
    try:
        roots = named_terms(shape_roots, operation)
        raw_probes = capture_items(shape_probes, ShapePreparationProbe, operation)
        preparation_capacity(operation, len(raw_probes) + 8)
        captured_shape = None
        shape_data = None
        if shapes is not None:
            captured_shape, shape_data = _source(shapes, operation)
        probes = []
        probe_data = []
        for probe in raw_probes:
            if type(probe) is not ShapePreparationProbe:
                raise InputError("Shape probes require ordinary typed immutable inputs")
            source, descriptor = _source(probe.source, operation)
            focus = named_terms(probe.focus_nodes, operation)
            if not focus:
                raise InputError("A required shape probe needs nonempty named focus")
            probes.append((source, focus))
            probe_data.append({"source": descriptor, "focus": focus})
        if captured_shape is not None and (not roots or not probes):
            raise InputError("Selected shapes require roots and actual positive probes")
        if captured_shape is None and (roots or probes):
            raise InputError("Shape selections require an actual supplied shape source")
        captured = capture_inputs(
            models,
            ontology,
            imports=imports,
            lookup=lookup,
            context=context,
            resources=resources,
            base=base,
            regime=regime,
            rdf_direction=rdf_direction,
            budget=operation,
        )
        if expected is not None:
            if type(expected) is not PreparationIdentity:
                raise InputError(
                    "Expected preparation requires a current typed identity"
                )
            bound_text(expected.inputs_sha256, operation)
            bound_text(expected.engine_sha256, operation)
        selected_context, documents, selected_base, selected_regime, direction = (
            _options(captured.options)
        )
        declaration_nodes = _measure(captured.declarations, operation)
        operation.consume("bytes", 128 * declaration_nodes)
        operation.consume("work", 8 * declaration_nodes)
        artifact = compile_model_artifact(
            models_from_data(captured.declarations),
            captured.source,
            imports=captured.imports,
            lookup=captured.lookup,
            context=selected_context,
            resources=documents,
            base=selected_base,
            regime=selected_regime,
            rdf_direction=direction,
            budget=operation,
        )
        reports.append(artifact.report)
        retain_report(artifact.report, operation, issues)
        if (
            not artifact.report.verified
            or artifact.content is None
            or artifact.sha256 is None
        ):
            raise ResourceError("Required model compilation did not verify")
        restored = load_model_artifact(
            artifact.content,
            expected=captured.identity,
            trusted_sha256=artifact.sha256,
            budget=operation,
        )
        reports.append(restored.report)
        retain_report(restored.report, operation, issues)
        if not restored.report.verified or restored.prepared is None:
            raise ResourceError("Required compiled model loading did not verify")
        loader = OfflineDocumentLoader(documents, limits=operation.limits)
        if selected_context is not ABSENT:
            context_result = inspect_context(
                selected_context,
                base=selected_base,
                document_loader=loader,
                budget=operation,
            )
            reports.append(context_result.report)
            retain_report(context_result.report, operation, issues)
            if not context_result.report.verified:
                raise ResourceError("Required context preparation did not verify")
        ontology_source = shape_source = None
        if captured_shape is not None:
            ontology_source = prepare_source(
                captured.source, document_loader=loader, budget=operation
            )
            shape_source = prepare_source(
                captured_shape, document_loader=loader, budget=operation
            )
            for probe_source, focus in probes:
                preparation_capacity(operation)
                data = prepare_source(
                    probe_source, document_loader=loader, budget=operation
                )
                selected_roots, selected_focus, coverage = shape_bindings(
                    roots, focus, operation
                )
                operation.consume("work", 0)
                remaining = operation.deadline - time.monotonic()
                if remaining <= 0:
                    raise LimitExceeded("Revision preparation exceeded its deadline")
                with ShaclSession(
                    shape_source,
                    ontology=ontology_source,
                    imports=captured.imports,
                    use_shapes=selected_roots,
                    mode="pairs",
                    preflight=True,
                    coverage=coverage,
                    budget=operation,
                    timeout=remaining,
                ) as session:
                    probe_result = session.validate(
                        data,
                        focus_nodes=selected_focus,
                        cancel=operation.cancel,
                    )
                reports.append(probe_result.report)
                retain_report(probe_result.report, operation, issues)
                if not probe_result.report.verified:
                    raise ResourceError("A required actual positive shape probe failed")
        preparation_capacity(operation, 4)
        native_version = None
        if captured_shape is not None:
            try:
                native_version = importlib.metadata.version("pyshacl")
            except importlib.metadata.PackageNotFoundError as error:
                raise ResourceError(
                    "The qualified shape backend identity is unavailable"
                ) from error
        input_identity = json_bytes(
            {
                "model_inputs": captured.identity.inputs_sha256,
                "shapes": shape_data,
                "shape_roots": roots,
                "shape_probes": probe_data,
                "shape_contract": "named-core-pairs-meta-shacl",
                "limits": asdict(operation.limits),
            },
            operation,
        )
        engine_identity = json_bytes(
            {
                "compiled_models": captured.identity.engine_sha256,
                "python": platform.python_version(),
                "pyshacl": native_version,
            },
            operation,
        )
        operation.consume(
            "work", 2 + (len(input_identity) + len(engine_identity)) // 4096
        )
        identity = PreparationIdentity(
            hashlib.sha256(input_identity).hexdigest(),
            hashlib.sha256(engine_identity).hexdigest(),
        )
        if expected is not None and identity != expected:
            raise ResourceError("Closed revision differs from expected preparation")
        report = Report(
            "revision.preparation",
            1,
            tuple(issues),
            requested=1,
            children=tuple(reports),
            regime="closed-compiled-model-context-selected-shape-preparation",
        )
        state = _RevisionState(
            uuid4().hex,
            identity,
            artifact,
            restored.prepared,
            ontology_source,
            shape_source,
            MappingProxyType(captured.imports),
            roots,
            report,
            operation._counts.get("bytes", 0) - starting_bytes,
        )
        prepared = PreparedRevision._owned(state)
        finished = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="revision.preparation")
    except (InputError, OSError) as error:
        try:
            unknown = isinstance(error, ResourceError | OSError)
            issues.append(
                Issue(
                    "revision.preparation_unverified"
                    if unknown
                    else "revision.input_error",
                    str(error),
                    kind="unverified" if unknown else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped), location="revision.preparation")
    for report in reports:
        issues.retain_completed(report)
    return RevisionPreparation(
        prepared,
        Report(
            "revision.preparation",
            int(finished),
            tuple(issues),
            requested=1,
            complete=finished,
            children=tuple(reports),
            regime="closed-compiled-model-context-selected-shape-preparation",
        ),
    )
