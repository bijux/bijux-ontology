"""Original-module qualification through the maintained native axiom parser."""

import hashlib
from collections.abc import Mapping, Sequence

from bijux_ontology.errors import InputError, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import IssueCollector, retain_report
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Report

from ..axioms.changes.checking import _owned, receipt
from ..axioms.codec.encoding import ConstructionEncoder
from ..axioms.inspection import inspect_axioms
from ..axioms.types import AxiomInspection, ProjectedAxiom
from ..backend.types import OwlProfileBackend
from ..documents.sources.native import prepare_owl_document
from ..documents.types import OwlDocument
from ..entailment.encoding import capture_obligations, encode_axiom
from ..explanations.types import SourceAxiom
from .types import AllowedReplacement


def reserve(operation: Operation, count: int = 1) -> None:
    """Prepay immutable result and handoff references before native execution."""
    operation.consume("work", count)
    operation.consume("states", 8 * count)
    operation.consume("bytes", 512 * count)


def origin(value: SourceAxiom, operation: Operation) -> SourceAxiom:
    """Own all caller-selected source metadata before any native invocation."""
    if type(value) is not SourceAxiom:
        raise InputError("Repair selections require ordinary SourceAxiom records")
    document, rendering, digest = value.document, value.rendering, value.sha256
    if type(rendering) is not str or type(digest) is not str:
        raise InputError("Source axiom rendering and hash must be ordinary text")
    bound_text(rendering, operation)
    bound_text(digest, operation)
    operation.consume("work", len(rendering) + 1)
    operation.consume("bytes", 4 * len(rendering))
    if hashlib.sha256(rendering.encode("utf-8")).hexdigest() != digest:
        raise InputError("Source axiom rendering differs from its native hash")
    reserve(operation)
    return SourceAxiom(_owned(document, operation), rendering, digest)


def controls(
    protected: Sequence[SourceAxiom],
    replacements: Sequence[AllowedReplacement],
    obligations: Sequence[ProjectedAxiom],
    operation: Operation,
) -> tuple[
    tuple[SourceAxiom, ...],
    tuple[AllowedReplacement, ...],
    tuple[ProjectedAxiom, ...],
]:
    """Capture explicit domain choices and nonempty supported consumer questions."""
    selected = tuple(
        origin(value, operation)
        for value in capture_items(protected, SourceAxiom, operation)
    )
    authored = []
    for value in capture_items(replacements, AllowedReplacement, operation):
        if type(value) is not AllowedReplacement or type(value.label) is not str:
            raise InputError("Allowed replacements require ordinary typed records")
        bound_text(value.label, operation)
        if not value.label or value.label.isspace():
            raise InputError("An authored replacement requires a meaningful label")
        chosen = origin(value.origin, operation)
        additions, _ = ConstructionEncoder(operation).axioms(value.additions)
        if not additions:
            raise InputError("Use deletion, rather than an empty authored replacement")
        reserve(operation)
        authored.append(AllowedReplacement(chosen, additions, value.label))
    queries = capture_obligations(obligations, operation)
    if not queries:
        raise InputError("Repairs require nonempty consumer entailments")
    for query in queries:
        encode_axiom(query, operation)
    return selected, tuple(authored), queries


class OriginBridge:
    """Cache one exact qualified original-module inventory and native rendering map.

    A functional-syntax wrapper is parsed by OWLAPI, never by a second grammar.
    A rendering is usable only when it denotes one supported annotated axiom
    actually present in the named original module. The transaction separately
    enforces exact root membership when that axiom is removed.
    """

    def __init__(
        self,
        backend: OwlProfileBackend,
        source: OwlDocument,
        imports: Mapping[str, OwlDocument] | None,
        operation: Operation,
        heap_mb: int,
        issues: IssueCollector,
        reports: list[Report],
    ) -> None:
        """Keep only already captured immutable source and execution ownership."""
        self.backend = backend
        self.source = source
        self.imports = imports
        self.operation = operation
        self.heap_mb = heap_mb
        self.issues = issues
        self.reports = reports
        self.inspection: AxiomInspection | None = None
        self.cache: dict[tuple[object, ...], ProjectedAxiom] = {}

    def _inspection(self, document: OwlDocument) -> AxiomInspection:
        """Publish paid native evidence before any fallible report adoption."""
        reserve(self.operation)
        result = inspect_axioms(
            self.backend,
            document,
            imports=self.imports if document is self.source else None,
            budget=self.operation,
            heap_mb=self.heap_mb,
        )
        self.reports.append(result.report)
        retain_report(result.report, self.operation, self.issues)
        if not result.report.verified:
            raise ResourceError("Required source-axiom inspection is not qualified")
        return result

    def resolve(self, value: SourceAxiom) -> ProjectedAxiom:
        """Require exact owner, original membership and preserved annotations."""
        operation = self.operation
        value = origin(value, operation)
        reserve(operation)
        key = (*receipt(value.document), value.sha256)
        if key in self.cache:
            return self.cache[key]
        if self.inspection is None:
            self.inspection = self._inspection(self.source)
        actual = self.inspection
        operation.consume("work", len(actual.documents) + 1)
        if not any(
            isinstance(item.source, OwlDocument)
            and receipt(item.source) == receipt(value.document)
            for item in actual.documents
        ):
            raise InputError(
                "Source axiom owner is outside the actual required closure"
            )
        # Encoding and wrapper storage are paid before concatenation or parsing.
        operation.consume("bytes", 512 + 8 * len(value.rendering))
        wrapped = prepare_owl_document(
            "Ontology(<urn:bijux:ontology:source-axiom> " + value.rendering + ")",
            format="functional",
            document_iri="urn:bijux:ontology:source-axiom",
            effective_url=value.document.effective_url,
            budget=operation,
        )
        decoded = self._inspection(wrapped)
        if len(decoded.axioms) != 1 or len(decoded.documents) != 1:
            raise ResourceError("Source rendering has no single supported native axiom")
        axiom = decoded.axioms[0].axiom
        operation.consume("work", (len(value.rendering) + 1) * (len(actual.axioms) + 1))
        if not any(
            isinstance(row.document.source, OwlDocument)
            and receipt(row.document.source) == receipt(value.document)
            and row.axiom == axiom
            for row in actual.axioms
        ):
            raise InputError("Source rendering is not an annotated axiom in its owner")
        self.cache[key] = axiom
        return axiom
