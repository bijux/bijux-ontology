"""Finite structural comparisons of declared RDF serialisation contracts.

Each model definition and each field occurrence is checked independently. A
registry index resolves child types without recursively expanding model fields;
recursive declarations therefore terminate without losing reused-child checks.
Results describe supported declarations, not general ontology consistency.
"""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    ModelDeclaration,
    TermDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.models.contracts.constraints import (
    _emission_issues,
    _field_issues,
    _value_contract_issues,
)
from bijux_ontology.models.contracts.propagation import ModelContracts
from bijux_ontology.models.contracts.registry import index_models
from bijux_ontology.models.emission.direction import project_direction
from bijux_ontology.models.emission.matching import trait_matches
from bijux_ontology.models.value_constraints.checking import retain_enumeration_report
from bijux_ontology.owl.obligations import (
    ObligationResult,
    OntologyObligation,
    prove_obligations,
)
from bijux_ontology.owl.relations.classes import ClassContracts
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.literals.checking import Regime, check_literal
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary


def check_models(
    models: Sequence[ModelDeclaration],
    vocabulary: Vocabulary | Graph,
    *,
    allow_empty: bool = False,
    regime: Regime = "application",
    limits: Limits | None = None,
    budget: Budget | None = None,
    rdf_direction: DirectionPolicy | None = None,
    datatype_registry: DatatypeRegistry | None = None,
) -> Report:
    """Check finite model declarations against explicit supplied RDF statements.

    Named subclass/equivalence edges and named disjointness are supported.
    Domains/ranges constrain each present field occurrence; they never form a
    closed-world whitelist. Optional and union branches remain independent.
    """
    if rdf_direction not in (None, "i18n-datatype", "compound-literal"):
        raise InputError("Unknown RDF direction policy")
    if datatype_registry is not None and not isinstance(
        datatype_registry, DatatypeRegistry
    ):
        raise InputError("Expected an explicit DatatypeRegistry")
    if regime not in ("application", "rdf", "owl-dl"):
        raise InputError("Unknown semantic regime")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    budget = budget if budget is not None else Budget(limits)
    sources = (
        vocabulary
        if isinstance(vocabulary, Vocabulary)
        else load_vocabulary(vocabulary, budget=budget)
    )
    registry: dict[str, ModelDeclaration] = {}
    issues = IssueCollector(budget)
    enumeration_reports: dict[tuple[int, bool], Report] = {}
    checked = 0
    complete = True
    try:
        graph = sources._read(budget=budget).graph
        contracts = ModelContracts(
            graph, regime, budget, datatype_registry=datatype_registry
        )
        classes = ClassContracts(graph, budget)
        budget.consume("triples", len(graph))
        registry = index_models(models, budget, issues)
        for model in registry.values():
            budget.consume("work")
            for field in model.fields:
                location = field.location or f"{model.identifier}.{field.name}"
                for index, branch in enumerate(field.values):
                    budget.consume("work")
                    retain_enumeration_report(
                        branch.constraints,
                        enumeration_reports,
                        budget=budget,
                        issues=issues,
                    )
                    branch_location = f"{location}[{index}]"
                    issues.extend(_value_contract_issues(branch, branch_location))
                    for sample_index, sample in enumerate(branch.samples):
                        budget.consume("work")
                        sample_location = f"{branch_location}.samples[{sample_index}]"
                        if (
                            rdf_direction is not None
                            and sample.datatype is not None
                            and str(sample.datatype).startswith(
                                "https://www.w3.org/ns/i18n#"
                            )
                        ):
                            _, projected = project_direction(
                                Graph(), sample, rdf_direction, budget
                            )
                            if projected is None:
                                issues.append(
                                    Issue(
                                        "models.direction_unverified",
                                        "Sample direction encoding is not qualified",
                                        sample_location,
                                        kind="unverified",
                                    )
                                )
                            elif (
                                branch.datatype is not None
                                and branch.datatype != str(sample.datatype)
                                or not trait_matches(
                                    branch.language, projected.language, fold=True
                                )
                                or not trait_matches(
                                    branch.direction, projected.direction
                                )
                            ):
                                issues.append(
                                    Issue(
                                        "models.sample_contract_conflict",
                                        "Sample metadata differs "
                                        "from its declared branch",
                                        sample_location,
                                    )
                                )
                            continue
                        issues.extend(
                            check_literal(
                                sample,
                                location=sample_location,
                                datatype_registry=datatype_registry,
                                budget=budget,
                            ).issues
                        )
                        expected = (
                            str(RDF.langString)
                            if isinstance(branch.language, str)
                            else branch.datatype
                        )
                        actual = str(
                            sample.datatype
                            or (RDF.langString if sample.language else XSD.string)
                        )
                        if (
                            expected is not None
                            and expected != actual
                            or not trait_matches(
                                branch.language, sample.language, fold=True
                            )
                            or not trait_matches(branch.direction, None)
                        ):
                            issues.append(
                                Issue(
                                    "models.sample_contract_conflict",
                                    "Sample metadata differs from the declared "
                                    "serialisation branch",
                                    sample_location,
                                    expected=expected,
                                    actual=actual,
                                )
                            )
                    if branch.model is not None and branch.model not in registry:
                        issues.append(
                            Issue(
                                "input.model_reference_missing",
                                "Value branch references an unknown model",
                                branch_location,
                                kind="input_error",
                                actual=branch.model,
                            )
                        )
        relevant: set[str] = set()
        for model in registry.values():
            budget.consume("work")
            declared = list(model.types)
            for field in model.fields:
                for value in field.values:
                    declared.extend(value.types)
                    if value.container == "@list":
                        declared.append(str(RDF.List))
                if not field.predicate.startswith("@"):
                    contract = contracts.property(field)
                    declared.extend(
                        str(item)
                        for item in (*contract.parents, *contract.values)
                        if isinstance(item, URIRef)
                    )
            ancestors, _ = classes.ancestors(declared)
            relevant.update(ancestors)
        issues.extend(
            Issue(
                "models.disjoint_group_unverified",
                "Disjoint group is malformed or contains anonymous expressions",
                "owl:AllDisjointClasses",
                kind="unverified",
            )
            for _group in classes.prepare_disjoint(relevant)
        )
        checked = 0
        for model in registry.values():
            budget.consume("work")
            location = model.location or model.identifier
            checked += 1
            if not model.types:
                issues.append(
                    Issue(
                        "models.type_unverified",
                        "Model has no declared RDF class",
                        location,
                        kind="unverified",
                    )
                )
            else:
                membership = check_vocabulary(
                    sources,
                    [
                        TermDeclaration(
                            iri, (str(OWL.Class), str(RDFS.Class)), location
                        )
                        for iri in model.types
                    ],
                    regime="owl-dl" if regime == "owl-dl" else "rdf",
                    budget=budget,
                )
                checked += membership.checked
                issues.extend(membership.issues)
            for conflict in classes.conflicts(model.types):
                issues.append(
                    Issue(
                        "models.disjoint_conflict",
                        "Model declares explicitly disjoint simultaneous types",
                        location,
                        actual=conflict,
                    )
                )
            _, anonymous = classes.ancestors(model.types)
            if anonymous:
                issues.append(
                    Issue(
                        "models.expression_unverified",
                        "Model class uses an unsupported anonymous expression",
                        location,
                        kind="unverified",
                    )
                )
            issues.extend(_emission_issues(model, classes, contracts))
            count, findings = _field_issues(
                model,
                registry,
                classes,
                sources,
                regime,
                contracts,
                rdf_direction=rdf_direction,
            )
            checked += count
            issues.extend(findings)
        for obligation in contracts.obligations(registry):
            contextual = replace(
                registry[obligation.identifier], types=obligation.types
            )
            contextual_issues: list[Issue] = []
            for conflict in classes.conflicts(obligation.types):
                contextual_issues.append(
                    Issue(
                        "models.incoming_contract_conflict",
                        "Incoming constraints and child emissions imply disjoint types",
                        obligation.identifier,
                        actual=conflict,
                    )
                )
            count, findings = _field_issues(
                contextual,
                registry,
                classes,
                sources,
                regime,
                contracts,
                rdf_direction=rdf_direction,
            )
            checked += count
            contextual_issues.extend(
                replace(
                    issue, code=issue.code.replace("models.", "models.contextual_", 1)
                )
                for issue in findings
            )
            for origin in obligation.locations:
                issues.extend(
                    replace(issue, location=f"{origin} -> {issue.location}")
                    for issue in contextual_issues
                )
    except LimitExceeded as error:
        complete = False
        # The exhaustion marker is retained even when the issue allowance is full.
        list.append(
            issues, Issue("models.limit_exceeded", str(error), kind="exhausted")
        )
    return Report(
        "models",
        checked,
        tuple(issues),
        children=tuple(enumeration_reports.values()),
        allow_empty=allow_empty,
        complete=complete,
        regime=regime,
        families=("model-contracts",),
    )


@dataclass(frozen=True)
class ModelAssessment:
    """Asserted model checks alongside independent selected ontology answers.

    ``model_report`` preserves the existing finite model-checking contract.
    ``report`` also requires every explicitly selected obligation to be established.
    Narrow proofs never remove independent model restrictions or establish whole
    ontology consistency. Each answer retains its exact typed question.
    """

    model_report: Report
    obligations: tuple[ObligationResult, ...]

    @property
    def report(self) -> Report:
        """Require raw model truth and every explicitly selected proof question."""
        reports = []
        for result in self.obligations:
            question = result.obligation
            issues: tuple[Issue, ...] = ()
            if not result.complete or result.status != "established":
                issues = (
                    Issue(
                        "ontology.obligation_exhausted"
                        if not result.complete
                        else "ontology.obligation_unknown"
                        if result.status == "unknown"
                        else "ontology.obligation_contradicted",
                        "Selected ontology obligation could not be completed"
                        if not result.complete
                        else "Selected ontology obligation remains unknown"
                        if result.status == "unknown"
                        else "Selected ontology obligation is contradicted",
                        question.location or question.kind,
                        kind="exhausted"
                        if not result.complete
                        else "unverified"
                        if result.status == "unknown"
                        else "finding",
                        expected=question.required,
                        actual=", ".join(question.actual),
                        evidence_json=json.dumps(
                            {
                                "obligation": asdict(question),
                                "basis": result.basis,
                                "witness": [
                                    [node.n3() for node in edge]
                                    for edge in result.witness
                                ],
                            }
                        ),
                    ),
                )
            reports.append(
                Report(
                    f"ontology.{question.kind}",
                    checked=int(result.complete),
                    issues=issues,
                    requested=1,
                    complete=result.complete,
                    regime=question.regime,
                    families=("ontology-obligations",),
                )
            )
        return combine_reports("model_assessment", self.model_report, *reports)

    @property
    def outstanding(self) -> tuple[Issue, ...]:
        """Retain every unresolved model obligation, including relevant restrictions."""
        return tuple(
            issue for issue in self.model_report.issues if issue.kind == "unverified"
        )


def assess_models(
    models: Sequence[ModelDeclaration],
    vocabulary: Vocabulary | Graph,
    *,
    obligations: Sequence[OntologyObligation] = (),
    allow_empty: bool = False,
    regime: Regime = "application",
    limits: Limits | None = None,
) -> ModelAssessment:
    """Expose narrow proofs without discarding independent model unknowns.

    Each selected question retains its own semantic regime. Request consistency
    explicitly when needed; the native engine leaves that separate question
    unknown. A named domain/range proof cannot discharge class restrictions.
    """
    sources = (
        vocabulary
        if isinstance(vocabulary, Vocabulary)
        else load_vocabulary(vocabulary, limits=limits)
    )
    report = check_models(
        models, sources, allow_empty=allow_empty, regime=regime, limits=limits
    )
    answers = prove_obligations(sources._read().graph, obligations, limits=limits)
    return ModelAssessment(report, answers)
