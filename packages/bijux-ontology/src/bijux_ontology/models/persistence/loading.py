"""Compile and qualify inert model preparation across fresh application processes."""

import hashlib
from collections.abc import Mapping, Sequence
from typing import cast

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.codecs import models_from_data
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.json.encoding import json_bytes
from bijux_ontology.json.values import (
    array_value,
    decode_json,
    object_value,
    optional_string_value,
    string_value,
)
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.processing.resources import OfflineDocumentLoader
from bijux_ontology.models.contracts.preparation import PreparedModels
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.literals.checking import Regime
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary, _VocabularyPreparation

from .identity import capture_inputs, engine_identity, operation
from .inspection import inspection_data, read_inspection
from .sources import TermTable, read_source, read_terms, record, source_data
from .types import (
    ModelPreparationArtifact,
    PreparationIdentity,
    PreparationReuse,
    RestoredModels,
    _digest,
)

_FORMAT = "bijux-ontology.model-preparation"


class _Incompatible(InputError):
    """Current input, trust or semantic format cannot qualify saved preparation."""


def _options(
    value: object,
) -> tuple[object, dict[str, object], str | None, Regime, DirectionPolicy | None]:
    """Decode the closed context and policy inputs used by the existing plan owner."""
    data = record(value, {"context", "resources", "base", "regime", "rdf_direction"})
    context = object_value(data["context"], location="preparation context")
    if set(context) == {"absent"} and context["absent"] is True:
        selected_context: object = ABSENT
    elif set(context) == {"value"}:
        selected_context = context["value"]
    else:
        raise InputError(
            "Preparation context must distinguish absent and supplied values"
        )
    resources = object_value(data["resources"], location="preparation resources")
    regime = string_value(data["regime"])
    direction = optional_string_value(data["rdf_direction"])
    if regime not in ("application", "rdf", "owl-dl") or direction not in (
        None,
        "i18n-datatype",
        "compound-literal",
    ):
        raise InputError("Unsupported preparation policy")
    return (
        selected_context,
        resources,
        optional_string_value(data["base"]),
        cast(Regime, regime),
        cast(DirectionPolicy | None, direction),
    )


def _report(
    scope: str, issues: IssueCollector, child: Report | None, *, complete: bool
) -> Report:
    """Separate artifact qualification from fresh declaration and emission truth."""
    return Report(
        scope,
        checked=int(complete),
        requested=1,
        complete=complete,
        issues=tuple(issues),
        children=() if child is None else (child,),
    )


def _failure(error: InputError, issues: IssueCollector) -> None:
    """Preserve every paid native finding when artifact preparation stops late."""
    if isinstance(error, LimitExceeded):
        issues.exhausted(str(error))
        return
    try:
        issues.append(
            Issue(
                "models.artifact_incompatible"
                if isinstance(error, _Incompatible)
                else "models.artifact_input_error",
                str(error),
                kind="unverified"
                if isinstance(error, _Incompatible)
                else "input_error",
            )
        )
    except LimitExceeded as exhausted:
        issues.exhausted(str(exhausted))


def compile_model_artifact(
    models: Sequence[ModelDeclaration],
    source: RdfSource,
    *,
    imports: Mapping[str, RdfSource] | None = None,
    lookup: Sequence[RdfSource] = (),
    context: object | Absent = ABSENT,
    resources: Mapping[str, object] | None = None,
    base: str | None = None,
    regime: Regime = "application",
    rdf_direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    datatype_registry: DatatypeRegistry | None = None,
) -> ModelPreparationArtifact:
    """Compile exact immutable inputs through existing vocabulary/model owners.

    No passing report is serialized. All supplied import entries participate in
    invalidation, even when unused; live graphs, paths, streams and extensions
    are outside this initial closed str/bytes preparation profile.
    """
    allowance = operation(limits, budget)
    issues = IssueCollector(allowance)
    identity = None
    child = None
    plan = None
    try:
        inputs = capture_inputs(
            models,
            source,
            imports=imports,
            lookup=lookup,
            context=context,
            resources=resources,
            base=base,
            regime=regime,
            rdf_direction=rdf_direction,
            budget=allowance,
        )
        identity = inputs.identity
        selected_context, documents, selected_base, selected_regime, direction = (
            _options(inputs.options)
        )
        vocabulary = load_vocabulary(
            inputs.source,
            imports=inputs.imports,
            lookup=inputs.lookup,
            budget=allowance,
            preserve_literals=True,
            document_loader=OfflineDocumentLoader(documents, limits=allowance.limits),
        )
        plan = PreparedModels(
            models_from_data(inputs.declarations, budget=allowance),
            vocabulary,
            context=selected_context,
            resources=documents,
            base=selected_base,
            regime=selected_regime,
            rdf_direction=direction,
            datatype_registry=datatype_registry,
            budget=allowance,
        )
        retain_report(plan.declaration_report, allowance, issues)
        child = plan.declaration_report
        if not child.complete:
            return ModelPreparationArtifact(
                None,
                None,
                identity,
                _report("models.artifact.compile", issues, child, complete=False),
            )
        reader = vocabulary._read(budget=allowance)
        count = sum(len(graph) for _, graph in reader.scopes)
        allowance.consume("states", 8 * count)
        allowance.consume("bytes", 2048 * count)
        allowance.consume("work", count * max(1, count.bit_length()))
        view = VocabularyView(
            vocabulary, scopes=("local", "imports", "lookup"), budget=allowance
        )
        view._prepare_index(allowance)
        prepared = vocabulary._capture_preparation(budget=allowance)
        terms = TermTable(allowance)
        sources = [
            source_data(scope, owned, metadata, terms, allowance)
            for scope, owned, metadata in prepared.sources
        ]
        inspections = [
            inspection_data(key, index, terms, allowance)
            for key, index in prepared.inspections
        ]
        data = {
            "format": _FORMAT,
            "format_version": 1,
            "identity": {
                "inputs_sha256": identity.inputs_sha256,
                "engine_sha256": identity.engine_sha256,
            },
            "declarations": inputs.declarations,
            "options": inputs.options,
            "terms": terms.rows,
            "sources": sources,
            "inspection": inspections,
            "required_imports_qualified": prepared.required_imports_qualified,
            "extensions": [],
        }
        payload = json_bytes(data, allowance)
        allowance.consume("work", 1 + len(payload) // 4096)
        pin = hashlib.sha256(payload).hexdigest()
        return ModelPreparationArtifact(
            payload,
            pin,
            identity,
            _report("models.artifact.compile", issues, child, complete=True),
        )
    except InputError as error:
        if plan is not None:
            issues.retain_completed(plan.declaration_report)
        _failure(error, issues)
        return ModelPreparationArtifact(
            None,
            None,
            identity,
            _report("models.artifact.compile", issues, child, complete=False),
        )


def load_model_artifact(
    content: bytes,
    *,
    expected: PreparationIdentity,
    trusted_sha256: str,
    limits: Limits | None = None,
    budget: Budget | None = None,
    datatype_registry: DatatypeRegistry | None = None,
) -> RestoredModels:
    """Load externally pinned typed preparation and recheck actual declarations.

    The trusted digest must come from a caller-controlled build/application
    manifest. An embedded self-hash cannot authenticate derived semantic indexes.
    Every output is still checked afresh by the ordinary PreparedModels API.
    Custom datatype catalogues are explicit runtime bindings, never stored code.
    """
    allowance = operation(limits, budget)
    if not isinstance(expected, PreparationIdentity):
        raise InputError("Artifact loading requires a current PreparationIdentity")
    _digest(expected.inputs_sha256)
    _digest(expected.engine_sha256)
    _digest(trusted_sha256)
    issues = IssueCollector(allowance)
    child = None
    plan = None
    try:
        if type(content) is not bytes:
            raise InputError("Preparation loading requires immutable bytes")
        if len(content) > allowance.limits.max_bytes:
            raise LimitExceeded("Preparation artifact exceeds max_bytes")
        allowance.consume("work", 1 + len(content) // 4096)
        if hashlib.sha256(content).hexdigest() != trusted_sha256:
            raise _Incompatible(
                "Preparation artifact does not match its trusted digest"
            )
        if engine_identity(allowance) != expected.engine_sha256:
            raise _Incompatible(
                "Current semantic engine differs from preparation identity"
            )
        value = decode_json(
            content,
            max_bytes=allowance.limits.max_bytes,
            max_depth=allowance.limits.max_depth,
            budget=allowance,
        )
        raw = record(
            value,
            {
                "format",
                "format_version",
                "identity",
                "declarations",
                "options",
                "terms",
                "sources",
                "inspection",
                "required_imports_qualified",
                "extensions",
            },
        )
        if (
            raw["format"] != _FORMAT
            or type(raw["format_version"]) is not int
            or raw["format_version"] != 1
        ):
            raise _Incompatible("Unsupported preparation artifact format/version")
        if array_value(raw["extensions"]):
            raise _Incompatible(
                "Preparation extensions require explicit supported rebinding"
            )
        identity_data = record(raw["identity"], {"inputs_sha256", "engine_sha256"})
        actual = PreparationIdentity(
            string_value(identity_data["inputs_sha256"]),
            string_value(identity_data["engine_sha256"]),
        )
        if actual != expected:
            raise _Incompatible("Preparation input or semantic-engine identity changed")
        selected_context, documents, selected_base, regime, direction = _options(
            raw["options"]
        )
        declarations = models_from_data(raw["declarations"], budget=allowance)
        terms = read_terms(raw["terms"], allowance)
        sources = tuple(
            read_source(item, terms, allowance) for item in array_value(raw["sources"])
        )
        by_scope = {scope: source for scope, source, _ in sources}
        if len(by_scope) != len(sources):
            raise InputError("Preparation repeats a source role")
        inspections = tuple(
            read_inspection(item, terms, by_scope, allowance)
            for item in array_value(raw["inspection"])
        )
        qualified = raw["required_imports_qualified"]
        if type(qualified) is not bool or not qualified:
            raise InputError("Compiler import closure qualification is missing")
        vocabulary = Vocabulary._from_preparation(
            _VocabularyPreparation(sources, inspections, qualified), budget=allowance
        )
        plan = PreparedModels(
            declarations,
            vocabulary,
            context=selected_context,
            resources=documents,
            base=selected_base,
            regime=regime,
            rdf_direction=direction,
            datatype_registry=datatype_registry,
            budget=allowance,
        )
        retain_report(plan.declaration_report, allowance, issues)
        child = plan.declaration_report
        report = _report("models.artifact.load", issues, child, complete=child.complete)
        if not child.complete:
            return RestoredModels(None, report)
        return RestoredModels(
            plan,
            report,
            PreparationReuse(tuple(by_scope), tuple(key for key, _ in inspections)),
        )
    except InputError as error:
        if plan is not None:
            issues.retain_completed(plan.declaration_report)
        _failure(error, issues)
        return RestoredModels(
            None, _report("models.artifact.load", issues, child, complete=False)
        )
