"""Actual maintained LinkML RDF serialization over owned closed schema views."""

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import islice
from threading import Lock
from typing import TYPE_CHECKING

from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.interfaces.adapters.contracts import ModelExtraction, RdfField
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.checking import check_models
from bijux_ontology.models.rdf_emission import RdfModelUsage, check_rdf_emission
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Issue, Report, combine_reports
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.view import Vocabulary

from .linkml import _charge_tree, _extract, _schema_snapshot

if TYPE_CHECKING:
    from linkml_runtime.utils.schemaview import SchemaView  # type: ignore[import-untyped]


@dataclass(frozen=True)
class RdfInstanceCheck:
    """Exact owned native RDF output and all declaration/sample checking truth."""

    source: PreparedSource | None
    report: Report


class RdfSerializer:
    """Reuse one closed schema/extraction for the maintained RDFLib dumper.

    Fixed options accept a prefix_map of explicit namespace strings. Ordinary
    absolute HTTP identifiers and registered CURIEs follow LinkML's public
    namespaces.uri_for operation. Base/percent-encoded/ordered-list and complex
    range output are not silently qualified. Schema, options and caller imports
    stay owned; replacing them requires constructing another profile.
    """

    def __init__(
        self,
        view: "SchemaView",
        *,
        options: Mapping[str, object],
        identifiers: Mapping[str, str] | None = None,
        field_contracts: Mapping[str, Mapping[str, RdfField]] | None = None,
        limits: Limits | None = None,
        budget: Budget | None = None,
    ) -> None:
        if budget is not None and limits is not None and limits != budget.limits:
            raise InputError(
                "Explicit limits conflict with the shared operation budget"
            )
        operation = budget or Budget(limits)
        self._limits = operation.limits
        _charge_tree(options, operation)
        _charge_tree(identifiers, operation)
        _charge_tree(field_contracts, operation)
        if set(options) - {"prefix_map"}:
            raise InputError("Supported LinkML RDF option is prefix_map")
        prefix_map = options.get("prefix_map", {})
        if not isinstance(prefix_map, Mapping):
            raise InputError("LinkML prefix_map requires a namespace mapping")
        self._prefixes = {}
        for prefix, namespace in prefix_map.items():
            if (
                not isinstance(prefix, str)
                or not prefix
                or prefix in {"@base", "_base"}
            ):
                raise InputError("LinkML RDF profile requires named namespace prefixes")
            self._prefixes[prefix] = absolute_iri(namespace)
        self._schema = _schema_snapshot(view, operation)
        for prefix, namespace in self._prefixes.items():
            self._schema.namespaces()[prefix] = namespace
        self._identifiers = dict(identifiers or {})
        self._extraction = _extract(
            self._schema,
            classes=None,
            identifiers=self._identifiers,
            field_contracts=field_contracts,
            serializer="rdflib",
            budget=operation,
        )
        self._dump_lock = Lock()

    @property
    def limits(self) -> Limits:
        """Retain the bounds used for this immutable prepared schema profile."""
        return self._limits

    @property
    def extraction(self) -> ModelExtraction:
        """Return immutable declarations and their independent qualification truth."""
        return self._extraction

    def check(
        self,
        instance: object,
        *,
        class_name: str,
        vocabulary: Vocabulary | Graph,
        limits: Limits | None = None,
        budget: Budget | None = None,
    ) -> RdfInstanceCheck:
        """Dump once, retain that exact source, and check actual selected subjects."""
        if limits is not None and limits != self.limits:
            raise InputError(
                "Reusable LinkML profile limits differ; prepare another profile"
            )
        if budget is not None and budget.limits != self.limits:
            raise InputError("Shared budget differs from the prepared LinkML profile")
        operation = budget or Budget(self.limits)
        reports = [self.extraction.report]
        source = None
        try:
            from linkml_runtime.dumpers import rdflib_dumper  # type: ignore[import-untyped]

            model_id = self._identifiers.get(class_name, class_name)
            model = next(
                (
                    item
                    for item in self.extraction.declarations
                    if item.identifier == model_id
                ),
                None,
            )
            if model is None:
                raise InputError(
                    "Selected LinkML class has no extracted declaration",
                    location=class_name,
                )
            _charge_tree(instance, operation)
            with self._dump_lock:
                graph = rdflib_dumper.as_rdf_graph(
                    instance, self._schema, prefix_map=dict(self._prefixes)
                )
                source = prepare_source(
                    graph, limits=operation.limits, budget=operation
                )
                identifier = self._schema.get_identifier_slot(class_name)
                if identifier is not None:
                    raw_id = getattr(instance, str(identifier.name))
                    subject = self._schema.namespaces().uri_for(raw_id)
                    absolute_iri(str(subject))
                else:
                    candidates = (
                        tuple(
                            islice(
                                source.graph().subjects(
                                    RDF.type, URIRef(model.types[0])
                                ),
                                2,
                            )
                        )
                        if model.types
                        else ()
                    )
                    subject = candidates[0] if len(candidates) == 1 else None
            if subject is None:
                reports.append(
                    Report(
                        "linkml-root",
                        requested=1,
                        issues=(
                            Issue(
                                "emission.node_unverified",
                                "Actual root subject is not unambiguous; "
                                "add an identifier slot",
                                class_name,
                                kind="unverified",
                            ),
                        ),
                    )
                )
            else:
                models = self.extraction.declarations
                supplied = (
                    vocabulary
                    if isinstance(vocabulary, Vocabulary)
                    else load_vocabulary(
                        vocabulary, limits=operation.limits, budget=operation
                    )
                )
                reports.append(
                    check_models(
                        models, supplied, limits=operation.limits, budget=operation
                    )
                )
                reports.append(
                    check_rdf_emission(
                        source,
                        models,
                        (RdfModelUsage(model_id, subject),),
                        limits=operation.limits,
                        budget=operation,
                    )
                )
        except Exception as error:
            exhausted = isinstance(error, (LimitExceeded, MemoryError))
            reports.append(
                Report(
                    "linkml-serialization",
                    requested=1,
                    complete=False,
                    issues=(
                        Issue(
                            "execution.limit_exceeded"
                            if exhausted
                            else "input.invalid",
                            f"{type(error).__name__}: {error}",
                            kind="exhausted" if exhausted else "input_error",
                        ),
                    ),
                )
            )
        return RdfInstanceCheck(source, combine_reports("linkml-instance", *reports))


def check_instance(
    instance: object,
    *,
    view: "SchemaView",
    vocabulary: Vocabulary | Graph,
    options: Mapping[str, object],
    class_name: str,
    identifiers: Mapping[str, str] | None = None,
    field_contracts: Mapping[str, Mapping[str, RdfField]] | None = None,
    limits: Limits | None = None,
) -> RdfInstanceCheck:
    """Qualify the selected schema and check one actual LinkML RDF dump offline."""
    operation = Budget(limits)
    try:
        serializer = RdfSerializer(
            view,
            options=options,
            identifiers=identifiers,
            field_contracts=field_contracts,
            budget=operation,
        )
    except Exception as error:
        exhausted = isinstance(error, (LimitExceeded, MemoryError))
        return RdfInstanceCheck(
            None,
            Report(
                "linkml-instance",
                requested=1,
                complete=False,
                issues=(
                    Issue(
                        "execution.limit_exceeded" if exhausted else "input.invalid",
                        f"{type(error).__name__}: {error}",
                        kind="exhausted" if exhausted else "input_error",
                    ),
                ),
            ),
        )
    return serializer.check(
        instance, class_name=class_name, vocabulary=vocabulary, budget=operation
    )
