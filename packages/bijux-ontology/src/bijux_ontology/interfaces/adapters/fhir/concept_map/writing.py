"""Actual native R5 production and explicitly loss-aware SSSOM projection."""

import threading
import time
from importlib import import_module
from typing import Literal

from bijux_ontology.errors import InputError, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.mappings.interchange import export_sssom, read_sssom

from ..identity import FhirCodeBinding, FhirCodeIdentity, capture_code_bindings
from ..json import decode_fhir_json
from .reading import (
    canonical,
    encode,
    failure,
    native_json,
    note,
    obj,
    read_concept_map,
    reserve,
)
from .types import FhirConceptMap, FhirConceptMapExport


def write_concept_map(
    concept_map: FhirConceptMap,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> FhirConceptMapExport:
    """Produce the native retained tree, including all conditions and metadata."""
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    content = None
    children: tuple[Report, ...] = ()
    try:
        if type(concept_map) is not FhirConceptMap:
            raise InputError("Use an owned ConceptMap")
        reserve(operation)
        captured = read_concept_map(
            concept_map.payload, budget=operation, timeout=timeout, cancel=cancel
        )
        children = (captured.report,)
        issues.retain_completed(captured.report)
        retain_report(captured.report, operation, issues)
        if captured.report.verified:
            _, produced = native_json(captured.payload, operation)
            operation.consume("work", 0)
            content = produced
        else:
            note(issues, "Original ConceptMap is not qualified for native production")
    except (
        InputError,
        ValueError,
        TypeError,
        ImportError,
        UnicodeError,
        MemoryError,
        RecursionError,
    ) as error:
        failure(error, issues)
    report = Report(
        "adapter.fhir_concept_map_export",
        int(content is not None),
        tuple(issues),
        requested=1,
        children=children,
        regime="fhir-r5-native-conceptmap-producer",
    )
    return FhirConceptMapExport(content if report.verified else None, (), report)


def export_concept_map_sssom(
    concept_map: FhirConceptMap,
    *,
    bindings: tuple[FhirCodeBinding, ...],
    prefixes: tuple[tuple[str, str], ...] = (),
    mapping_set_id: str,
    license: str,
    mapping_justification: str,
    loss: Literal["reject", "partial"] = "reject",
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> FhirConceptMapExport:
    """Project only unconditional equivalent/broader/narrower named-code rows.

    Directed SKOS exact/broad/narrow predicates are explicitly selected for this
    projection. Related/negative mappings, scopes, conditional values, products,
    noMap and fallback are never silently discarded into unconditional rows.
    Partial mode publishes only representable rows and retains omitted source
    paths with a required unverified loss report; it cannot certify the full map.
    Business versions remain explicit SSSOM source-version cells and bindings.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    reports: list[Report] = []
    omitted: list[tuple[str | int, ...]] = []
    content = None
    try:
        if type(concept_map) is not FhirConceptMap or loss not in {"reject", "partial"}:
            raise InputError("Select an owned ConceptMap and explicit loss policy")
        for text in (mapping_set_id, license, mapping_justification):
            bound_text(text, operation)
            absolute_iri(text)
        owned_bindings = capture_code_bindings(bindings, operation)
        reserve(operation, len(owned_bindings))
        identities = {b.identity: str(b.iri) for b in owned_bindings}
        if type(prefixes) is not tuple:
            raise InputError("SSSOM prefixes require an immutable explicit tuple")
        reserve(operation, len(prefixes))
        prefix_map = {}
        for pair in prefixes:
            if type(pair) is not tuple or len(pair) != 2:
                raise InputError("SSSOM prefixes require exact namespace pairs")
            prefix, namespace = pair
            for value in pair:
                if type(value) is not str or not value:
                    raise InputError("SSSOM prefix bindings need nonempty text")
                bound_text(value, operation)
            absolute_iri(namespace)
            if prefix in prefix_map:
                raise InputError("SSSOM prefix bindings must be distinct")
            prefix_map[prefix] = namespace
        try:
            ensure_converter = import_module("sssom.util").ensure_converter
        except ModuleNotFoundError as error:
            raise ResourceError(
                "Install bijux-ontology[sssom] for SSSOM export"
            ) from error
        reserve(operation, len(prefix_map) + 32)
        workspace = sum(len(p) + len(n) for p, n in prefix_map.items())
        operation.consume("bytes", 32 * workspace + 1024 * (len(prefix_map) + 32))
        operation.consume("work", workspace + 1)
        converter = ensure_converter(prefix_map, use_defaults=False)

        def compact(iri: str) -> str:
            """Use the maintained converter with explicit bindings and builtins only."""
            bound_text(iri, operation)
            operation.consume("work", len(iri) * (len(prefix_map) + 32) + 1)
            value = converter.compress(iri)
            if not isinstance(value, str):
                raise ResourceError("SSSOM IRI needs an explicit namespace binding")
            bound_text(value, operation)
            return value

        reserve(operation)
        captured = read_concept_map(
            concept_map.payload, budget=operation, timeout=timeout, cancel=cancel
        )
        reports.append(captured.report)
        issues.retain_completed(captured.report)
        retain_report(captured.report, operation, issues)
        if not captured.report.verified:
            raise ResourceError("Original ConceptMap source is not qualified")
        rows = []
        expected_rows = []
        relation = {
            "equivalent": "skos:exactMatch",
            "source-is-narrower-than-target": "skos:broadMatch",
            "source-is-broader-than-target": "skos:narrowMatch",
        }
        global_scope = (
            captured.source_scope is not None or captured.target_scope is not None
        )
        for group in captured.groups:
            operation.consume("work")
            source = (
                canonical(group.source, operation) if group.source is not None else None
            )
            target = (
                canonical(group.target, operation) if group.target is not None else None
            )
            if group.unmapped_json is not None:
                reserve(operation)
                omitted.append((*group.path, "unmapped"))
            for element in group.elements:
                operation.consume("work")
                if (
                    element.no_map
                    or not element.targets
                    or element.value_set is not None
                ):
                    reserve(operation)
                    omitted.append(element.path)
                for mapping in element.targets:
                    reserve(operation, 2)
                    raw = obj(decode_fhir_json(mapping.metadata_json, operation))
                    if (
                        global_scope
                        or source is None
                        or target is None
                        or element.code is None
                        or mapping.code is None
                        or mapping.conditions
                        or mapping.products
                        or raw.get("property")
                        or mapping.relationship not in relation
                    ):
                        omitted.append(mapping.path)
                        continue
                    first, second = (
                        FhirCodeIdentity(*source, element.code),
                        FhirCodeIdentity(*target, mapping.code),
                    )
                    if first not in identities or second not in identities:
                        raise ResourceError(
                            "Versioned codes need explicit reversible RDF bindings"
                        )
                    row = (
                        compact(identities[first]),
                        relation[mapping.relationship],
                        compact(identities[second]),
                        compact(mapping_justification),
                        compact(source[0]),
                        source[1] or "",
                        compact(target[0]),
                        target[1] or "",
                        str(raw.get("comment", "")),
                    )
                    for value in row:
                        bound_text(value, operation)
                        if any(char in value for char in ("\t", "\r", "\n", "#")):
                            raise ResourceError(
                                "SSSOM lexical framing cannot preserve this cell"
                            )
                    rows.append(row)
                    expected_rows.append((identities[first], identities[second]))
        if omitted:
            note(
                issues,
                "SSSOM omits required ConceptMap semantics; paths remain explicit",
            )
            if loss == "reject":
                raise ResourceError("Rejecting lossy ConceptMap-to-SSSOM projection")
        if not rows:
            raise ResourceError("No explicitly representable mapping rows remain")
        header = (
            "# mapping_set_id: "
            + encode(mapping_set_id, operation)
            + "\n# license: "
            + encode(license, operation)
            + "\n"
        )
        reserve(operation, len(prefix_map) + 1)
        header_parts = [header, "# curie_map:\n"]
        for prefix, namespace in prefix_map.items():
            prefix_json = encode(prefix, operation)
            namespace_json = encode(namespace, operation)
            operation.consume("bytes", len(prefix_json) + len(namespace_json) + 16)
            header_parts.append("#   " + prefix_json + ": " + namespace_json + "\n")
        operation.consume("bytes", sum(len(part) for part in header_parts))
        header = "".join(header_parts)
        columns = (
            "subject_id\tpredicate_id\tobject_id\tmapping_justification\t"
            "subject_source\tsubject_source_version\tobject_source\t"
            "object_source_version\tcomment\n"
        )
        size = (
            len(header)
            + len(columns)
            + sum(sum(len(v) + 1 for v in row) for row in rows)
        )
        operation.consume("bytes", 8 * size)
        operation.consume("work", size + 1)
        tsv = (
            header + columns + "".join("\t".join(row) + "\n" for row in rows)
        ).encode("utf-8")
        reserve(operation)
        native = read_sssom(
            tsv,
            mapping_set_id=mapping_set_id,
            license=license,
            budget=operation,
            timeout=max(0.001, operation.deadline - time.monotonic()),
            cancel=cancel,
        )
        reports.append(native.report)
        issues.retain_completed(native.report)
        retain_report(native.report, operation, issues)
        if native.report.verified and native.mapping_set is not None:
            operation.consume("work", len(expected_rows) + 1)
            observed = tuple(
                (row.subject, row.object) for row in native.mapping_set.rows
            )
            if observed != tuple(expected_rows):
                raise ResourceError("Native SSSOM changed explicit endpoint identities")
            reserve(operation)
            produced = export_sssom(native.mapping_set, budget=operation)
            reports.append(produced.report)
            issues.retain_completed(produced.report)
            retain_report(produced.report, operation, issues)
            operation.consume("work", 0)
            content = produced.content
    except (
        InputError,
        ValueError,
        TypeError,
        ImportError,
        UnicodeError,
        MemoryError,
        RecursionError,
    ) as error:
        failure(error, issues)
    report = Report(
        "adapter.fhir_concept_map_sssom",
        int(content is not None),
        tuple(issues),
        requested=1,
        children=tuple(reports),
        regime="fhir-r5-explicit-sssom-projection",
    )
    return FhirConceptMapExport(content, tuple(omitted), report)
