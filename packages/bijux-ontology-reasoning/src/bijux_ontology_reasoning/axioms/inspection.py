"""Actual maintained axiom projection with owned original input qualification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast

from bijux_ontology._json import object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report

from ..backend.bundles import qualify_backend
from ..backend.execution import (
    invoke_worker,
    qualify_capability,
    result_counter,
    result_text,
)
from ..backend.rdf_sources import stage_documents
from ..backend.types import OwlProfileBackend, ProfileDocument
from ..documents.preparation import stage_native_documents
from ..documents.types import OwlDocument
from . import types as t
from .decoding import ProjectionDecoder


def _selection(
    value: Sequence[str] | None, operation: Budget
) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InputError("Axiom families require a finite sequence or None")
    size = len(value)
    if size > 64:
        raise InputError("Axiom family selection exceeds the finite native inventory")
    operation.consume("states", size)
    chosen = []
    seen = set()
    for index in range(size):
        name = value[index]
        if type(name) is not str:
            raise InputError("Axiom family names must be plain strings")
        bound_text(name, operation)
        if not name or name in seen:
            raise InputError("Choose distinct nonempty axiom family names")
        seen.add(name)
        chosen.append(name)
    return tuple(chosen)


def _metadata(raw: object, decoder: ProjectionDecoder) -> dict[int, t.RdfParseMetadata]:
    result = {}
    for raw_record in decoder.records(raw):
        item = object_value(raw_record, location="OWL RDF loader metadata")
        index = result_counter(
            item.get("document"), decoder.budget, decoder.budget.limits.max_resources
        )
        present = decoder.boolean(item.get("present"))
        if index in result:
            raise ResourceError("Duplicate RDF loader document")
        numbers = tuple(
            result_counter(
                item.get(name), decoder.budget, decoder.budget.limits.max_triples
            )
            if present
            else None
            for name in ("unparsed", "guessed", "triples")
        )
        header = result_text(item.get("header"), decoder.budget, optional=not present)
        unparsed, guessed, triples = numbers
        result[index] = t.RdfParseMetadata(present, unparsed, guessed, triples, header)
    return result


def inspect_axioms(
    backend: OwlProfileBackend,
    source: OwlDocument | PreparedSource,
    *,
    imports: Mapping[str, OwlDocument | RdfSource | Payload] | None = None,
    axiom_types: Sequence[str] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> t.AxiomInspection:
    """Project actual selected OWLAPI objects into immutable finite records.

    None selects all actual axiom families; an explicit selection retains counts
    for every excluded family. Unsupported selected objects, parser losses and
    unavailable execution remain required unknown. Original native formats use
    their checked offline parser boundary. Prepared RDF uses the existing staged
    RDF parser and requires complete unparsed/guessed-declaration metadata.
    JVM work is opaque and bounded by the owned deadline/heap, while host result
    adoption shares the supplied Budget. No logical inference is performed.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    decoder = ProjectionDecoder(operation)
    documents: list[t.InspectedOntology] = []
    axioms: list[t.AxiomRecord] = []
    unsupported: list[t.UnsupportedAxiom] = []
    exclusions: list[t.AxiomExclusion] = []
    children: list[Report] = []
    capability = None
    selected = None
    complete = False
    try:
        selected = _selection(axiom_types, operation)
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="axioms-", dir=backend.artifacts) as job:
            directory = Path(job)
            originals: tuple[OwlDocument, ...] | tuple[ProfileDocument, ...]
            if isinstance(source, OwlDocument):
                originals, request = stage_native_documents(
                    source,
                    cast(Mapping[str, OwlDocument] | None, imports),
                    directory,
                    operation,
                )
                input_kind = "native"
            elif isinstance(source, PreparedSource):
                originals, request, _ = stage_documents(
                    source,
                    cast(Mapping[str, RdfSource | Payload] | None, imports),
                    directory,
                    operation,
                    issues,
                    children,
                )
                input_kind = "rdf"
            else:
                raise InputError(
                    "Axiom inspection requires an owned RDF or native source"
                )
            maximum = min(operation.remaining("issues"), operation.limits.max_states)
            if not maximum or not operation.remaining("states"):
                raise LimitExceeded(
                    "Axiom inspection has no remaining inventory allowance"
                )
            request.update(
                operation="axioms",
                input_kind=input_kind,
                axiom_types=selected,
                max_bytes=operation.limits.max_bytes,
                max_issues=maximum,
                max_resources=operation.limits.max_resources,
                max_nodes=operation.remaining("states"),
                projection_depth=max(1, min(64, operation.limits.max_depth // 4)),
            )
            data = invoke_worker(backend, request, directory, operation, heap_mb)
            operation.consume("states")
            capability = qualify_capability(data, backend, operation)
            children.append(capability.report)
            for raw in decoder.records(data.get("changes")):
                item = object_value(raw, location="OWL input interpretation loss")
                kind, value = (
                    decoder.text(item.get("kind")),
                    decoder.text(item.get("value")),
                )
                issues.append(
                    Issue(
                        "owl.axiom_input_loss",
                        "The original document has unrepresented native content",
                        expected=kind,
                        actual=value,
                        kind="unverified",
                    )
                )
            metadata = _metadata(data.get("loaders"), decoder)
            if data.get("input_kind") != input_kind:
                raise ResourceError("Axiom worker omitted its exact input kind")
            by_index = {}
            for raw in decoder.records(data.get("documents")):
                item = object_value(raw, location="inspected OWL ontology")
                index = result_counter(
                    item.get("document"), operation, len(originals) - 1
                )
                if index in by_index:
                    raise ResourceError("Duplicate inspected document")
                rdf = metadata.get(index)
                if input_kind == "rdf":
                    if rdf is None:
                        raise ResourceError("RDF inspection omitted loader metadata")
                    if not rdf.present or rdf.unparsed != 0 or rdf.guessed != 0:
                        issues.append(
                            Issue(
                                "owl.axiom_input_unverified",
                                "RDF parsing has missing, unparsed or guessed "
                                "statements",
                                kind="unverified",
                            )
                        )
                elif metadata:
                    raise ResourceError(
                        "Native inspection returned unrelated RDF metadata"
                    )
                ontology = result_text(
                    item.get("ontology_iri"), operation, optional=True
                )
                version = result_text(item.get("version_iri"), operation, optional=True)
                declared = tuple(
                    decoder.iri(value) for value in decoder.records(item.get("imports"))
                )
                # Total native count is scalar metadata; node retention is separate.
                count = result_counter(item.get("axioms"), operation, 2**31 - 1)
                annotations = decoder.annotations(item.get("annotations"), 1)
                operation.consume("states")
                document = t.InspectedOntology(
                    originals[index],
                    ontology,
                    version,
                    declared,
                    count,
                    annotations,
                    rdf,
                )
                documents.append(document)
                by_index[index] = document
            seen_counts = dict.fromkeys(by_index, 0)
            for raw in decoder.records(data.get("axioms")):
                item = object_value(raw, location="inspected OWL axiom")
                index = result_counter(
                    item.get("document"), operation, len(originals) - 1
                )
                if index not in by_index:
                    raise ResourceError("Unowned inspected axiom")
                kind = decoder.text(item.get("type"))
                if selected is not None and kind not in selected:
                    raise ResourceError("Axiom worker projected an unselected family")
                operation.consume("states")
                if item.get("unsupported") is True:
                    issues.append(
                        Issue(
                            "owl.axiom_unsupported",
                            "A selected maintained axiom has no complete "
                            "finite projection",
                            actual=kind,
                            kind="unverified",
                        )
                    )
                    unsupported.append(t.UnsupportedAxiom(by_index[index], kind))
                elif "unsupported" in item:
                    raise ResourceError("Invalid axiom support flag")
                else:
                    projected = decoder.axiom(item.get("value"))
                    axioms.append(t.AxiomRecord(by_index[index], projected))
                seen_counts[index] += 1
            for raw in decoder.records(data.get("exclusions")):
                item = object_value(raw, location="OWL axiom family exclusion")
                index = result_counter(
                    item.get("document"), operation, len(originals) - 1
                )
                kind = decoder.text(item.get("type"))
                count = result_counter(
                    item.get("count"), operation, operation.limits.max_states
                )
                if (
                    index not in by_index
                    or selected is None
                    or kind in selected
                    or not count
                ):
                    raise ResourceError("Invalid axiom family exclusion")
                operation.consume("states")
                exclusions.append(t.AxiomExclusion(by_index[index], kind, count))
                seen_counts[index] += count
            if data.get("status") == "exhausted":
                raise LimitExceeded(
                    "Native axiom projection exhausted its finite inventory"
                )
            if data.get("qualified") is not True or 0 not in by_index:
                raise ResourceError("Native axiom interpretation is incomplete")
            if input_kind == "rdf" and set(metadata) != set(by_index):
                raise ResourceError("RDF loader and axiom inventories differ")
            if any(
                seen_counts[index] != document.axiom_count
                for index, document in by_index.items()
            ):
                raise ResourceError("Native axiom inventory is incomplete")
            complete = (
                not unsupported
                and not issues
                and all(child.verified for child in children)
            )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.axioms")
    except (InputError, OSError):
        try:
            issues.append(
                Issue(
                    "owl.axioms_unverified",
                    "Selected native interpretation or typed projection is unavailable",
                    kind="unverified",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="owl.axioms")
    return t.AxiomInspection(
        tuple(documents),
        tuple(axioms),
        tuple(unsupported),
        tuple(exclusions),
        selected,
        capability,
        Report(
            "owl.axioms",
            int(complete),
            tuple(issues),
            requested=1,
            complete=complete,
            children=tuple(children),
            families=selected or (),
            regime="owlapi-selected-typed-inspection",
        ),
    )
