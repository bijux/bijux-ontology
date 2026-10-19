"""Owned reusable declaration truth with fresh, bounded actual-output checking."""

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import cast
from uuid import uuid4

from rdflib import Graph

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.json.values import _measure, own_json
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.resources import OfflineDocumentLoader
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.context import same_context
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.literals.checking import Regime
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary


def _prepared_json(value: object, operation: Budget) -> object:
    """Prepay closed JSON snapshots and serialization before allocating copies."""
    count = _measure(value, operation)
    operation.consume("work", 12 * count + 1)
    operation.consume("bytes", 128 + 64 * count)
    return own_json(
        value,
        max_bytes=operation.limits.max_bytes,
        max_depth=operation.limits.max_depth,
    )


@dataclass(frozen=True, init=False)
class PreparedModels:
    """Compile one immutable semantic snapshot; never cache an output's success.

    Construction checks declarations once. Subsequent calls recheck actual
    outputs, including child obligations, using fixed construction limits.
    JSON-LD resources must be closed parsed snapshots; paths and arbitrary
    loader callbacks are not retained. Changed declarations, imports, contexts
    or resources require constructing another plan. Revision values are opaque
    snapshot identities, not RDF canonical or semantic content hashes.
    """

    models: tuple[ModelDeclaration, ...]
    declaration_report: Report
    limits: Limits
    revision: str
    resource_versions: Mapping[str, str]
    _context_json: str | None
    _loader: OfflineDocumentLoader
    _base: str | None
    _rdf_direction: DirectionPolicy | None
    _vocabulary: Vocabulary

    def __init__(
        self,
        models: Sequence[ModelDeclaration],
        vocabulary: Vocabulary | Graph,
        *,
        context: object | Absent = ABSENT,
        resources: Mapping[str, object] | None = None,
        base: str | None = None,
        limits: Limits | None = None,
        budget: Budget | None = None,
        regime: Regime = "application",
        rdf_direction: DirectionPolicy | None = None,
    ) -> None:
        if rdf_direction not in (None, "i18n-datatype", "compound-literal"):
            raise InputError("Unsupported RDF direction policy")
        if budget is not None and not isinstance(budget, Budget):
            raise InputError("Prepared models require a Budget")
        if limits is not None and not isinstance(limits, Limits):
            raise InputError("Prepared models require Limits")
        if budget is not None and limits is not None and budget.limits != limits:
            raise InputError("Explicit limits conflict with the preparation budget")
        operation = budget if budget is not None else Budget(limits)
        selected = operation.limits
        if isinstance(cast(object, models), (str, bytes)) or not isinstance(
            models, Sequence
        ):
            raise InputError("Prepared models require a finite declaration sequence")
        operation.consume("states", len(models))
        operation.consume("bytes", 64 + 16 * len(models))
        declarations = tuple(models)
        supplied = resources if resources is not None else {}
        if not isinstance(supplied, Mapping):
            raise InputError("Prepared JSON-LD resources require a closed mapping")
        operation.consume("resources", len(supplied))
        snapshots = {}
        for url, document in supplied.items():
            bound_text(url, operation)
            absolute_iri(url)
            if isinstance(document, Path):
                raise InputError("Prepared JSON-LD resources require parsed snapshots")
            snapshots[url] = _prepared_json(document, operation)
        if base is not None:
            bound_text(base, operation)
            absolute_iri(base)
        context_json = None
        if context is not ABSENT:
            owned_context = _prepared_json(context, operation)
            context_json = json.dumps(owned_context, ensure_ascii=False)
        owned = (
            vocabulary
            if isinstance(vocabulary, Vocabulary)
            else load_vocabulary(vocabulary, budget=operation)
        )
        reader = owned._read(budget=operation)
        report = check_models(
            declarations,
            owned,
            regime=regime,
            rdf_direction=rdf_direction,
            budget=operation,
        )
        revision = uuid4().hex
        names = ["declarations", *(name for name, _ in reader.scopes)]
        if context_json is not None:
            names.append("context")
        if base is not None:
            names.append("base")
        names.extend(f"jsonld:{url}" for url in sorted(snapshots))
        if rdf_direction is not None:
            names.append("rdf_direction")
        object.__setattr__(self, "_vocabulary", owned)
        object.__setattr__(self, "models", declarations)
        object.__setattr__(self, "declaration_report", report)
        object.__setattr__(self, "limits", selected)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(
            self,
            "resource_versions",
            MappingProxyType(
                {name: f"{revision}:{index}" for index, name in enumerate(names)}
            ),
        )
        object.__setattr__(self, "_context_json", context_json)
        object.__setattr__(
            self, "_loader", OfflineDocumentLoader(snapshots, limits=selected)
        )
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_rdf_direction", rdf_direction)

    def _document(self, document: object, operation: Budget) -> object:
        """Apply only the explicitly owned external context, preserving conflicts."""
        if self._context_json is None:
            return document
        bound_text(self._context_json, operation)
        operation.consume("work", len(self._context_json) + 1)
        operation.consume("bytes", 128 + 64 * len(self._context_json))
        context = json.loads(self._context_json)
        output = _prepared_json(document, operation)
        if isinstance(output, dict):
            if "@context" in output and not same_context(
                output["@context"], context, operation
            ):
                raise InputError("Prepared context conflicts with actual output")
            return {**output, "@context": context}
        if isinstance(output, list):
            return {"@context": context, "@graph": output}
        raise InputError("An external context requires object or array output")

    def _result(self, report: Report, *, scope: str = "models.prepared") -> Report:
        """Retain raw declaration failures in every individually scoped result."""
        return combine_reports(scope, self.declaration_report, report)

    def _failure(
        self,
        error: InputError,
        *,
        scope: str = "model-emission",
        operation: Budget | None = None,
    ) -> Report:
        """Describe stopped scope without discarding compiled declaration truth."""
        if operation is not None and not isinstance(error, LimitExceeded):
            try:
                operation.consume("issues")
            except LimitExceeded as issue_limit:
                error = issue_limit
        exhausted = isinstance(error, LimitExceeded)
        return self._result(
            Report(
                scope,
                requested=1,
                complete=False,
                issues=(
                    Issue(
                        "execution.limit_exceeded" if exhausted else "input.invalid",
                        str(error),
                        kind="exhausted" if exhausted else "input_error",
                    ),
                ),
            ),
            scope=scope if scope == "models.batch.remaining" else "models.prepared",
        )

    def _check(
        self, document: object, usages: Sequence[ModelUsage], operation: Budget
    ) -> Report:
        """Use one fresh or shared batch budget without changing the plan."""
        try:
            operation.consume("issues", len(self.declaration_report.issues))
            report = check_emission(
                self._document(document, operation),
                self.models,
                usages,
                vocabulary=self._vocabulary,
                base=self._base,
                document_loader=self._loader,
                rdf_direction=self._rdf_direction,
                budget=operation,
            )
        except InputError as error:
            return self._failure(error, operation=operation)
        return self._result(report)

    def check(
        self,
        document: object,
        usages: Sequence[ModelUsage],
        *,
        budget: Budget | None = None,
    ) -> Report:
        """Check JSON-LD with a fresh or compatible enclosing budget."""
        if budget is not None and not isinstance(budget, Budget):
            raise InputError("Prepared JSON-LD checking requires a Budget")
        if budget is not None and budget.limits != self.limits:
            raise InputError("JSON-LD checking budget conflicts with prepared limits")
        return self._check(
            document, usages, budget if budget is not None else Budget(self.limits)
        )

    def check_rdf(
        self,
        source: PreparedSource,
        usages: Sequence[RdfModelUsage],
        *,
        graph_name: GraphSelection | None = None,
        union: bool = False,
        budget: Budget | None = None,
    ) -> Report:
        """Check owned RDF with a fresh allowance or a compatible enclosing budget."""
        if budget is not None and not isinstance(budget, Budget):
            raise InputError("Prepared RDF checking requires a Budget")
        if budget is not None and budget.limits != self.limits:
            raise InputError("RDF checking budget conflicts with prepared limits")
        operation = budget if budget is not None else Budget(self.limits)
        try:
            operation.consume("issues", len(self.declaration_report.issues))
            report = check_rdf_emission(
                source,
                self.models,
                usages,
                vocabulary=self._vocabulary,
                graph_name=graph_name,
                union=union,
                rdf_direction=self._rdf_direction,
                budget=operation,
            )
        except InputError as error:
            return self._failure(error, scope="rdf-emission", operation=operation)
        return self._result(report)

    def check_many(
        self,
        documents: Iterable[object],
        usages: Sequence[ModelUsage],
        *,
        max_records: int = 1000,
    ) -> Iterator[Report]:
        """Stream bounded records with one cumulative budget and an explicit tail.

        Each record has its own report. At most one additional input is read to
        determine whether the record cap left an unchecked tail. Exhausted work
        stops with an incomplete remaining-scope report; outputs are not retained.
        Caller iterator execution keeps its ordinary in-process lifetime.
        """
        if type(max_records) is not int or max_records <= 0:
            raise InputError("max_records requires a positive integer")
        if isinstance(documents, (str, bytes, Mapping)):
            raise InputError("A batch requires an iterable of actual documents")
        operation = Budget(self.limits)
        try:
            if isinstance(cast(object, usages), (str, bytes)) or not isinstance(
                usages, Sequence
            ):
                raise InputError("A batch requires a finite binding sequence")
            operation.consume("states", len(usages))
            bindings = tuple(usages)
            iterator = iter(documents)
        except InputError as error:
            yield self._failure(error, scope="models.batch.remaining")
            return
        except TypeError as error:
            yield self._failure(InputError(str(error)), scope="models.batch.remaining")
            return
        sentinel = object()
        for _index in range(max_records):
            try:
                document = next(iterator, sentinel)
                if document is sentinel:
                    return
                operation.consume("states")
            except LimitExceeded as error:
                yield self._failure(error, scope="models.batch.remaining")
                return
            except Exception as error:
                yield self._failure(
                    InputError(str(error)), scope="models.batch.remaining"
                )
                return
            report = self._check(document, bindings, operation)
            yield report
            if any(issue.kind == "exhausted" for issue in report.issues):
                yield self._failure(
                    LimitExceeded(
                        "Batch stopped; remaining record scope was not checked"
                    ),
                    scope="models.batch.remaining",
                )
                return
        try:
            if next(iterator, sentinel) is sentinel:
                return
        except Exception as error:
            yield self._failure(InputError(str(error)), scope="models.batch.remaining")
            return
        yield self._failure(
            LimitExceeded("max_records left an unchecked batch tail"),
            scope="models.batch.remaining",
        )
