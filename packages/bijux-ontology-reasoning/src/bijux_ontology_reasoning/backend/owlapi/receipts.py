"""Owned native input/profile/engine receipt adoption for finite DL operations."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from bijux_ontology.diagnostics.errors import ResourceError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.json.values import object_value

from ...documents.types import OwlDocument
from ..types import LoaderQualification, ProfileDocument, ReasonerIdentity
from .protocol import result_boolean, result_counter, result_records, result_text


def _reasoner(raw: object, budget: Budget) -> ReasonerIdentity:
    """Require the exact maintained engine identity and strict datatype policy."""
    item = object_value(raw, location="DL reasoner identity")
    name = result_text(item.get("name"), budget)
    artifact = result_text(item.get("artifact_version"), budget)
    native = result_text(item.get("native_version"), budget)
    ignored = result_boolean(item.get("ignore_unsupported_datatypes"))
    if (name, artifact, native, ignored) != ("HermiT", "1.4.5.519", "1.4.1.513", False):
        raise ResourceError("DL reasoner differs from the qualified pinned profile")
    budget.consume("states")
    assert name is not None and artifact is not None and native is not None
    return ReasonerIdentity(name, artifact, native, ignored)


def _loaders(
    raw: object,
    originals: tuple[OwlDocument | ProfileDocument, ...],
    budget: Budget,
    issues: IssueCollector,
) -> tuple[LoaderQualification, ...]:
    """Adopt owned RDF parser receipts and retain every actual interpretation gap."""
    result = []
    seen = set()
    for record in result_records(raw, budget):
        item = object_value(record, location="DL RDF loader receipt")
        index = result_counter(item.get("document"), budget, len(originals) - 1)
        original = originals[index]
        if index in seen or not isinstance(original, ProfileDocument):
            raise ResourceError("DL loader receipt has ambiguous original ownership")
        seen.add(index)
        present = result_boolean(item.get("present"))
        counts = tuple(
            result_counter(item.get(name), budget, budget.limits.max_triples)
            if present
            else None
            for name in ("unparsed", "guessed", "triples")
        )
        header = result_text(item.get("header"), budget, optional=not present)
        unparsed, guessed, triples = counts
        budget.consume("states")
        receipt = LoaderQualification(
            original, present, unparsed, guessed, triples, header
        )
        result.append(receipt)
        if not receipt.qualified:
            issues.append(
                Issue(
                    "owl.diagnosis_loader_unverified",
                    "Original RDF has incomplete or guessed native interpretation",
                    kind="unverified",
                )
            )
    return tuple(result)


@dataclass(frozen=True)
class QualifiedReceipt:
    """Qualified original closure and actual native answers, never a saved verdict."""

    raw_consistent: bool | None
    in_profile: bool | None
    native_error: str | None
    loaders: tuple[LoaderQualification, ...]
    reasoner: ReasonerIdentity | None
    qualified: bool = False


def reasoning_header(data: Mapping[str, object], operation: Budget) -> QualifiedReceipt:
    """Retain raw scalar answers before later receipt qualification can exhaust."""
    operation.consume("states", 2)
    operation.consume("bytes", 256)
    if type(data.get("consistent")) not in (bool, type(None)):
        raise ResourceError("DL operation returned an invalid consistency value")
    raw_consistent = cast(bool | None, data["consistent"])
    in_profile = (
        None if data.get("dl_profile") is None else result_boolean(data["dl_profile"])
    )
    native_error = result_text(data.get("error"), operation, optional=True)
    return QualifiedReceipt(raw_consistent, in_profile, native_error, (), None)


def qualify_reasoning_result(
    data: Mapping[str, object],
    originals: tuple[OwlDocument | ProfileDocument, ...],
    kind: str,
    operation: Budget,
    issues: IssueCollector,
    *,
    header: QualifiedReceipt,
    scope: Literal[
        "diagnosis",
        "entailment",
        "relationships",
        "datatypes",
        "models",
        "explanations",
    ],
) -> QualifiedReceipt:
    """Adopt only complete original scope, native profile and pinned engine receipts."""
    if data.get("input_kind") != kind:
        raise ResourceError("DL operation omitted its exact input interpretation")
    observed = set()
    for raw in result_records(data.get("documents"), operation):
        record = object_value(raw, location="DL loaded document")
        index = result_counter(record.get("document"), operation, len(originals) - 1)
        if index in observed:
            raise ResourceError("DL document ownership is ambiguous")
        observed.add(index)
        for name in ("ontology_iri", "version_iri"):
            result_text(record.get(name), operation, optional=True)
        result_counter(record.get("axioms"), operation, 2**31 - 1)
        for iri in result_records(record.get("imports"), operation):
            result_text(iri, operation)
    if len(observed) != len(originals):
        raise ResourceError("DL operation did not load every required document")
    loaders = _loaders(data.get("loaders"), originals, operation, issues)
    if (kind == "rdf" and len(loaders) != len(originals)) or (
        kind == "native" and loaders
    ):
        raise ResourceError("DL operation omitted exact RDF loader scope")
    for raw in result_records(data.get("changes"), operation):
        record = object_value(raw, location="DL native input loss")
        loss_kind = result_text(record.get("kind"), operation)
        value = result_text(record.get("value"), operation)
        issues.append(
            Issue(
                f"owl.{scope}_input_loss",
                "Original native content is not completely represented",
                expected=loss_kind,
                actual=value,
                kind="unverified",
            )
        )
    for raw in result_records(data.get("violations"), operation):
        text = result_text(raw, operation)
        issues.append(
            Issue(
                f"owl.{scope}_profile_unsupported",
                "Logical diagnosis requires the OWL 2 DL profile"
                if scope == "diagnosis"
                else "Entailment requires the OWL 2 DL profile"
                if scope == "entailment"
                else "Datatype compatibility requires the OWL 2 DL profile"
                if scope == "datatypes"
                else "Model compatibility requires the OWL 2 DL profile"
                if scope == "models"
                else "Logical explanations require the OWL 2 DL profile"
                if scope == "explanations"
                else "Inferred relationships require the OWL 2 DL profile",
                actual=text,
                kind="unverified",
            )
        )
    reasoner = (
        _reasoner(data["reasoner"], operation)
        if data.get("reasoner") is not None
        else None
    )
    qualified = (
        data.get("qualified") is True
        and header.in_profile is True
        and reasoner is not None
        and all(item.qualified for item in loaders)
        and not issues
    )
    return QualifiedReceipt(
        header.raw_consistent,
        header.in_profile,
        header.native_error,
        loaders,
        reasoner,
        qualified,
    )
