"""Read-only BioSamples characteristic checks over explicit local contracts."""

import json
from collections.abc import Sequence
from typing import cast

from rdflib import Graph, Literal, URIRef

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.json.paths import bounded_location
from bijux_ontology.json.values import owned_json
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary
from bijux_ontology_adapters.vocabulary_bindings.controlled.algebra import (
    compile_allowed,
)

from .schemas import CORE_SHA256, CORE_VERSION, check_schema, resource
from .types import (
    CharacteristicBinding,
    CharacteristicOccurrence,
    SampleCheck,
    SchemaResource,
)


def _allowed(
    value: URIRef | Literal,
    evidence: ValueEnumeration | None,
    where: str,
    issues: IssueCollector,
) -> None:
    """Use qualified closed absence or explicit exclusion, retaining open unknowns."""
    if evidence is None or value in evidence.values:
        return
    denied = value in evidence.excluded or evidence.qualified
    issues.append(
        Issue(
            "biosamples.value_disallowed" if denied else "biosamples.value_unverified",
            "Characteristic value is outside its explicit allowed evidence",
            where,
            kind="finding" if denied else "unverified",
        )
    )


def _policies(
    bindings: Sequence[CharacteristicBinding],
    budget: Budget,
    issues: IssueCollector,
    reports: list[Report],
) -> dict[str, tuple[CharacteristicBinding, tuple[ValueEnumeration | None, ...]]]:
    """Qualify metadata before indexing and compile existing finite set evidence."""
    result = {}
    for binding in capture(bindings, budget):
        if not isinstance(binding, CharacteristicBinding):
            raise InputError("Expected explicit CharacteristicBinding values")
        bound_text(binding.attribute, budget)
        for kind in capture(binding.kinds, budget):
            bound_text(kind, budget)
            absolute_iri(kind)
        if binding.attribute in result:
            raise InputError("Each attribute requires one unambiguous binding")
        selected = []
        for index, expression in enumerate(
            (binding.terms, binding.texts, binding.units)
        ):
            evidence = (
                compile_allowed(expression, budget=budget)
                if expression is not None
                else None
            )
            if evidence is not None:
                for term in (*evidence.values, *evidence.excluded):
                    budget.consume("work")
                    if not isinstance(term, URIRef if index == 0 else Literal):
                        raise InputError(
                            "Allowed evidence has the wrong native value kind"
                        )
                raw = evidence.qualification_report()
                retain_report(raw, budget, issues)
                reports.append(raw)
            selected.append(evidence)
        result[binding.attribute] = binding, tuple(selected)
    if not result:
        raise InputError("Select at least one characteristic obligation")
    return result


def _rows(
    sample: dict[str, object],
    policies: dict[
        str, tuple[CharacteristicBinding, tuple[ValueEnumeration | None, ...]]
    ],
    rows: list[CharacteristicOccurrence],
    terms: list[TermDeclaration],
    budget: Budget,
    issues: IssueCollector,
) -> int:
    """Retain every native characteristic occurrence without merging repetitions."""
    characteristics = sample.get("characteristics", {})
    if not isinstance(characteristics, dict):
        raise InputError("Expected native characteristic arrays")
    checked = 0
    for attribute, values in characteristics.items():
        if not isinstance(values, list):
            raise InputError("Expected native characteristic arrays")
        policy = policies.get(attribute)
        for index, raw in enumerate(values):
            budget.consume("work")
            budget.consume("states")
            budget.consume("bytes", 256)
            if not isinstance(raw, dict):
                raise InputError("Expected a native characteristic object")
            path = ("characteristics", attribute, index)
            where = bounded_location(path, budget)
            iris = raw.get("ontologyTerms", [])
            # Structural validation has already established all cell types.
            occurrence = CharacteristicOccurrence(
                attribute,
                index,
                where,
                raw.get("text"),
                raw.get("unit"),
                tuple(capture(iris, budget)),
                raw.get("tag"),
            )
            rows.append(occurrence)
            if policy is None:
                if iris:
                    issues.append(
                        Issue(
                            "biosamples.binding_missing",
                            "Ontology-bearing attribute needs an explicit binding",
                            where,
                            kind="unverified",
                        )
                    )
                continue
            checked += 1
            binding, allowed = policy
            for key, required, evidence in (
                ("text", binding.require_text, allowed[1]),
                ("unit", binding.require_unit, allowed[2]),
            ):
                cell = raw.get(key)
                cell_where = bounded_location((*path, key), budget)
                if cell is None and (required or evidence is not None):
                    issues.append(
                        Issue(
                            "biosamples.value_missing",
                            "Characteristic lacks its selected text or unit value",
                            cell_where,
                        )
                    )
                elif cell is not None:
                    _allowed(Literal(cell), evidence, cell_where, issues)
            if binding.require_terms and not iris:
                issues.append(
                    Issue(
                        "biosamples.value_missing",
                        "Characteristic lacks its required ontology terms",
                        bounded_location((*path, "ontologyTerms"), budget),
                    )
                )
            for ordinal, iri in enumerate(iris):
                budget.consume("work")
                budget.consume("states")
                cell_where = bounded_location((*path, "ontologyTerms", ordinal), budget)
                try:
                    absolute_iri(iri)
                except InputError:
                    issues.append(
                        Issue(
                            "biosamples.term_invalid",
                            "Ontology term is not an absolute IRI",
                            cell_where,
                        )
                    )
                    continue
                _allowed(URIRef(iri), allowed[0], cell_where, issues)
                terms.append(
                    TermDeclaration(iri, kinds=binding.kinds, location=cell_where)
                )
    for attribute in policies:
        if not characteristics.get(attribute):
            issues.append(
                Issue(
                    "biosamples.characteristic_missing",
                    "Selected characteristic has no actual occurrence",
                    bounded_location(("characteristics", attribute), budget),
                )
            )
    return checked


def check_sample(
    sample: object,
    *,
    core: SchemaResource,
    checklist: SchemaResource,
    bindings: Sequence[CharacteristicBinding],
    vocabulary: Vocabulary | Graph | None,
    resources: Sequence[SchemaResource] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> SampleCheck:
    """Validate a pinned BioSamples shape and local checklist before term checks.

    No network acquisition or submission occurs. Versioned resources resolve only
    within this call. The first profile supports the published core identified by
    CORE_VERSION/CORE_SHA256, finite draft-07 checklists and four published linear
    regexes. Unknown assertions, recursion or missing references remain unknown.
    """
    owned_budget = operation(limits, budget)
    issues = IssueCollector(owned_budget)
    structural: list[Report] = []
    semantic: list[Report] = []
    rows: list[CharacteristicOccurrence] = []
    identities: list[tuple[str, str, str]] = []
    sample_json = None
    checked = 0
    complete = False
    try:
        owned, input_cost = owned_json(sample, owned_budget)
        owned_budget.consume("bytes", 8 * input_cost + 64)
        sample_json = json.dumps(owned, ensure_ascii=False, allow_nan=False)
        inventory = []
        documents = []
        for selected in (core, checklist, *capture(resources, owned_budget)):
            document, digest = resource(selected, owned_budget)
            inventory.append((selected.uri, document))
            documents.append(document)
            identities.append((selected.uri, selected.version, digest))
        if len({item[0] for item in identities}) != len(identities):
            raise InputError("Resources require distinct versioned identities")
        if core.version != CORE_VERSION or identities[0][2] != CORE_SHA256:
            raise InputError("Core schema is outside the supported pinned profile")
        policies = _policies(bindings, owned_budget, issues, semantic)
        for selected, document in zip((core, checklist), documents[:2], strict=True):
            raw_report = check_schema(
                owned,
                input_cost,
                selected,
                document,
                tuple(inventory),
                owned_budget,
                issues,
            )
            structural.append(raw_report)
        if all(report.verified for report in structural):
            if not isinstance(owned, dict):
                raise InputError("BioSamples samples require JSON objects")
            terms: list[TermDeclaration] = []
            checked = _rows(
                cast(dict[str, object], owned),
                policies,
                rows,
                terms,
                owned_budget,
                issues,
            )
            if terms:
                if vocabulary is None:
                    issues.append(
                        Issue(
                            "biosamples.vocabulary_missing",
                            "Required ontology declarations need a supplied vocabulary",
                            kind="unverified",
                        )
                    )
                else:
                    report = check_vocabulary(vocabulary, terms, budget=owned_budget)
                    retain_report(report, owned_budget, issues)
                    semantic.append(report)
            complete = True
        else:
            # Native negative findings are complete structural evidence; they do
            # not establish that unexecuted semantic obligations were checked.
            issues.append(
                Issue(
                    "biosamples.semantics_unchecked",
                    "Structural findings prevent characteristic qualification",
                    kind="unverified",
                )
            )
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("BioSamples checking exceeded its cumulative allowance")
    except Exception:
        try:
            issues.append(
                Issue(
                    "biosamples.contract_unverified",
                    "Sample, local schema or sidecar contract could not be qualified",
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("BioSamples diagnostics exhausted their allowance")
    report = Report(
        "biosamples.sample",
        checked,
        tuple(issues),
        requested=checked if complete and checked else checked + 1,
        complete=complete,
        children=(*structural, *semantic),
        regime="biosamples-local-draft-07-explicit-bindings",
    )
    return SampleCheck(
        sample_json,
        tuple(rows),
        tuple(identities),
        tuple(structural),
        tuple(semantic),
        report,
    )
