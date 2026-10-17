"""Finite structural comparisons of declared RDF serialisation contracts.

Each model definition and each field occurrence is checked independently. A
registry index resolves child types without recursively expanding model fields;
recursive declarations therefore terminate without losing reused-child checks.
Results describe supported declarations, not general ontology consistency.
"""

from collections.abc import Sequence
from dataclasses import replace

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    ModelDeclaration,
    TermDeclaration,
)
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.owl.classes import ClassContracts
from bijux_ontology.rdf.literals import Regime, check_literal
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.view import Vocabulary

from ..errors import InputError, LimitExceeded
from ..reports import Issue, Report
from .constraints import _emission_issues, _field_issues, _value_contract_issues
from .propagation import ModelContracts
from .registry import index_models


def check_models(
    models: Sequence[ModelDeclaration],
    vocabulary: Vocabulary | Graph,
    *,
    allow_empty: bool = False,
    regime: Regime = "application",
    limits: Limits | None = None,
) -> Report:
    """Check finite model declarations against explicit supplied RDF statements.

    Named subclass/equivalence edges and named disjointness are supported.
    Domains/ranges constrain each present field occurrence; they never form a
    closed-world whitelist. Optional and union branches remain independent.
    """
    if regime not in ("application", "rdf", "owl-dl"):
        raise InputError("Unknown semantic regime")
    sources = (
        vocabulary
        if isinstance(vocabulary, Vocabulary)
        else load_vocabulary(vocabulary)
    )
    graph = sources.graph()
    budget = Budget(limits)
    contracts = ModelContracts(graph, regime, budget)
    classes = ClassContracts(graph, budget)
    registry: dict[str, ModelDeclaration] = {}
    issues = IssueCollector(budget)
    checked = 0
    complete = True
    try:
        budget.consume("triples", len(graph))
        registry = index_models(models, budget, issues)
        for model in registry.values():
            budget.consume("work")
            for field in model.fields:
                location = field.location or f"{model.identifier}.{field.name}"
                for index, branch in enumerate(field.values):
                    budget.consume("work")
                    branch_location = f"{location}[{index}]"
                    issues.extend(_value_contract_issues(branch, branch_location))
                    for sample_index, sample in enumerate(branch.samples):
                        budget.consume("work")
                        sample_location = f"{branch_location}.samples[{sample_index}]"
                        issues.extend(
                            check_literal(sample, location=sample_location).issues
                        )
                        expected = (
                            str(RDF.langString) if branch.language else branch.datatype
                        )
                        actual = str(
                            sample.datatype
                            or (RDF.langString if sample.language else XSD.string)
                        )
                        if (
                            expected is not None
                            and expected != actual
                            or branch.language is not None
                            and branch.language.lower()
                            != (sample.language or "").lower()
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
                model, registry, classes, sources, regime, contracts
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
                contextual, registry, classes, sources, regime, contracts
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
        allow_empty=allow_empty,
        complete=complete,
        regime=regime,
        families=("model-contracts",),
    )
