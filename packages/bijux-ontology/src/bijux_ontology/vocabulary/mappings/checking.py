"""Finite named-endpoint mapping policies over supplied vocabulary scopes."""

import json
from itertools import chain

from rdflib import URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.roles.classification import classify_statement, declaration_role
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.sources import source_role

from .types import MappingCheck, MappingPolicy, MappingSet, _iri, _iris


def _cardinality_evidence(
    requirement: str, endpoint: str, limit: int, observed: int, budget: Budget
) -> str:
    """Expose exact obligation roles without requiring diagnostic prose parsing."""
    bound_text(endpoint, budget)
    budget.consume("work", len(endpoint) + 1)
    budget.consume("bytes", 256 + 12 * len(endpoint))
    return json.dumps(
        {
            "requirement": requirement,
            "endpoint": endpoint,
            "limit": limit,
            "observed": observed,
        },
        sort_keys=True,
    )


_SUPPORTED = frozenset(
    str(predicate)
    for predicate in (
        SKOS.exactMatch,
        SKOS.closeMatch,
        SKOS.broadMatch,
        SKOS.narrowMatch,
        SKOS.relatedMatch,
    )
)


def _relation_conflict_evidence(
    subject: str, object: str, left: str, right: str, budget: Budget
) -> str:
    """Retain the exact directed pair and incompatible relation obligation."""
    size = 0
    for value in (subject, object, left, right):
        bound_text(value, budget)
        size += len(value)
    budget.consume("work", size + 1)
    budget.consume("bytes", 256 + 12 * size)
    return json.dumps(
        {
            "requirement": "relation_conflict",
            "subject": subject,
            "object": object,
            "relations": [left, right],
        },
        sort_keys=True,
    )


def _endpoint(
    iri: str,
    view: VocabularyView,
    kinds: frozenset[str],
    operation: Budget,
    issues: IssueCollector,
    where: str,
    declared: str = "",
) -> None:
    """Require supplied declaration evidence, never infer a global endpoint role."""
    info = view.lookup(iri)
    if info.status != "declared":
        issues.append(
            Issue(
                "mapping.endpoint_unverified",
                "Mapping endpoint is not declared in its selected vocabulary",
                where,
                kind="unverified",
                actual=iri,
            )
        )
        return
    operation.consume("states", 3 * len(info.kinds))
    actual = set()
    for kind in info.kinds:
        bound_text(kind, operation)
        actual.add(kind)
    if kinds and not actual.intersection(kinds):
        issues.append(
            Issue(
                "mapping.endpoint_kind",
                "Endpoint lacks an allowed explicitly asserted type",
                where,
                actual=iri,
            )
        )

    if declared and declared != str(RDFS.Resource):
        _iri(declared, operation)
        required = (
            "concept"
            if declared == str(SKOS.Concept)
            else declaration_role(URIRef(declared))
        )
        if required is None or declared == str(RDFS.Literal):
            issues.append(
                Issue(
                    "mapping.semantics_unverified",
                    "Declared endpoint type is outside named-entity interpretation",
                    where,
                    kind="unverified",
                    actual=declared,
                )
            )
            return
        actual_roles: set[str] = set()
        node = URIRef(iri)
        for edge in chain(view._outgoing.get(node, ()), view._incoming.get(node, ())):
            operation.consume("work")
            occurrences = classify_statement(
                (edge.subject, edge.predicate, edge.object),
                source_scope=edge.scope,
                budget=operation,
            )
            actual_roles.update(
                item.role
                for item in occurrences
                if item.term == node and item.position != "predicate"
            )
        if required == "property" and actual_roles.intersection(
            ("object_property", "datatype_property", "annotation_property")
        ):
            actual_roles.add("property")
        if required not in actual_roles:
            issues.append(
                Issue(
                    "mapping.endpoint_kind",
                    "Endpoint lacks its SSSOM-declared role in the supplied vocabulary",
                    where,
                    actual=iri,
                    expected=declared,
                )
            )


def check_mappings(
    mapping_set: MappingSet,
    *,
    subject_vocabulary: VocabularyView,
    object_vocabulary: VocabularyView,
    policy: MappingPolicy,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> MappingCheck:
    """Check all selected occurrences without equating mappings or confidence.

    Cardinalities count distinct endpoint IRIs under allowed predicates. Known
    maxima/conflicts remain findings when a later row cannot complete. Missing
    endpoint declarations remain unknown, rather than closed-world OWL claims.
    """
    operation = Operation(limits, budget, 30, None)
    issues = IssueCollector(operation)
    checked = 0
    requested = 1
    try:
        if not isinstance(mapping_set, MappingSet) or not isinstance(
            policy, MappingPolicy
        ):
            raise InputError("Mapping checks require an owned set and typed policy")
        if not isinstance(subject_vocabulary, VocabularyView) or not isinstance(
            object_vocabulary, VocabularyView
        ):
            raise InputError(
                "Mapping endpoints require explicitly scoped VocabularyView inputs"
            )
        requested = len(mapping_set.rows) + len(policy.subjects)
        allowed = frozenset(_iris(policy.relations, operation))
        subject_kinds = frozenset(_iris(policy.subject_kinds, operation))
        object_kinds = frozenset(_iris(policy.object_kinds, operation))
        subjects = _iris(policy.subjects, operation)
        conflicts = tuple(_iris(pair, operation) for pair in policy.conflicts)
        for view in (subject_vocabulary, object_vocabulary):
            for scope, _graph in view._scopes:
                bound_text(scope, operation)
            roles = tuple(
                dict.fromkeys(source_role(scope) for scope, _ in view._scopes)
            )
            view._vocabulary._read(roles, limits=operation.limits, budget=operation)
        targets: dict[str, set[str]] = {}
        sources: dict[str, set[str]] = {}
        relations: dict[tuple[str, str], set[str]] = {}
        uncertain: set[str] = set()
        bound_text(mapping_set.metadata_json, operation)
        operation.consume("states", len(mapping_set.metadata_json))
        operation.consume("work", len(mapping_set.metadata_json))
        operation.consume("bytes", 4 * len(mapping_set.metadata_json))
        metadata = object_value(
            decode_json(
                mapping_set.metadata_json,
                max_bytes=operation.limits.max_bytes,
                max_depth=operation.limits.max_depth,
            ),
            location="mapping metadata",
        )
        metadata_uninterpreted = bool(
            metadata.get("predicate_modifier") or metadata.get("other")
        )
        if metadata_uninterpreted:
            issues.append(
                Issue(
                    "mapping.semantics_unverified",
                    "Mapping-set qualifiers remain uninterpreted",
                    "metadata",
                    kind="unverified",
                )
            )
        for index, row in enumerate(mapping_set.rows):
            operation.consume("work")
            operation.consume("states")
            for value in (row.subject, row.predicate, row.object, row.justification):
                _iri(value, operation)
            for name, value in row.cells:
                bound_text(name, operation)
                bound_text(value, operation)
            where = "row:" + str(index)
            cells = dict(row.cells)
            row_uninterpreted = bool(cells.get("other")) or any(
                cells.get(name, "") for name in mapping_set.uninterpreted
            )
            if row_uninterpreted:
                issues.append(
                    Issue(
                        "mapping.semantics_unverified",
                        "Extension and other values remain uninterpreted",
                        where,
                        kind="unverified",
                    )
                )
            if row.predicate_type and row.predicate_type not in (
                str(OWL.ObjectProperty),
                str(RDF.Property),
                str(RDFS.Resource),
            ):
                issues.append(
                    Issue(
                        "mapping.semantics_unverified",
                        "Predicate kind is outside the finite relation profile",
                        where,
                        kind="unverified",
                        actual=row.predicate_type,
                    )
                )
            if cells.get("predicate_modifier", ""):
                issues.append(
                    Issue(
                        "mapping.semantics_unverified",
                        "Predicate modifiers remain uninterpreted",
                        where,
                        kind="unverified",
                    )
                )
            if row.predicate not in _SUPPORTED:
                issues.append(
                    Issue(
                        "mapping.semantics_unverified",
                        "Mapping relation is outside the finite SKOS profile",
                        where,
                        kind="unverified",
                        actual=row.predicate,
                    )
                )
            if row.predicate not in allowed:
                issues.append(
                    Issue(
                        "mapping.relation_forbidden",
                        "Mapping relation is forbidden by the application policy",
                        where,
                        actual=row.predicate,
                    )
                )
            if "https://w3id.org/sssom/NoTermFound" in (row.subject, row.object):
                issues.append(
                    Issue(
                        "mapping.semantics_unverified",
                        "Unmapped sentinel rows cannot establish endpoint cardinality",
                        where,
                        kind="unverified",
                    )
                )
                uncertain.add(row.subject)
                checked += 1
                continue
            _endpoint(
                row.subject,
                subject_vocabulary,
                subject_kinds,
                operation,
                issues,
                where,
                declared=row.subject_type,
            )
            _endpoint(
                row.object,
                object_vocabulary,
                object_kinds,
                operation,
                issues,
                where,
                declared=row.object_type,
            )
            supported_types = all(
                not kind
                or kind in (str(SKOS.Concept), str(RDFS.Resource))
                or declaration_role(URIRef(kind)) is not None
                for kind in (row.subject_type, row.object_type)
            )
            if (
                not supported_types
                or row.predicate not in _SUPPORTED
                or cells.get("predicate_modifier", "")
                or row_uninterpreted
                or metadata_uninterpreted
                or row.predicate_type
                not in (
                    "",
                    str(OWL.ObjectProperty),
                    str(RDF.Property),
                    str(RDFS.Resource),
                )
            ):
                uncertain.add(row.subject)
                checked += 1
                continue
            pair = (row.subject, row.object)
            operation.consume("states", 3)
            previous = relations.setdefault(pair, set())
            for left, right in conflicts:
                operation.consume("work")
                if (
                    row.predicate == left
                    and right in previous
                    or row.predicate == right
                    and left in previous
                ):
                    issues.append(
                        Issue(
                            "mapping.relation_conflict",
                            "Rows violate a declared incompatible-relation policy",
                            where,
                            actual=row.predicate,
                            evidence_json=_relation_conflict_evidence(
                                row.subject, row.object, left, right, operation
                            ),
                        )
                    )
            previous.add(row.predicate)
            if row.predicate in allowed:
                outgoing = targets.setdefault(row.subject, set())
                incoming = sources.setdefault(row.object, set())
                new_target, new_source = (
                    row.object not in outgoing,
                    row.subject not in incoming,
                )
                outgoing.add(row.object)
                incoming.add(row.subject)
                if (
                    new_target
                    and policy.maximum_targets is not None
                    and len(outgoing) > policy.maximum_targets
                ):
                    issues.append(
                        Issue(
                            "mapping.maximum_exceeded",
                            "Subject exceeds the declared distinct-target maximum",
                            where,
                            actual=row.subject,
                            evidence_json=_cardinality_evidence(
                                "maximum_targets",
                                row.subject,
                                policy.maximum_targets,
                                len(outgoing),
                                operation,
                            ),
                        )
                    )
                if (
                    new_source
                    and policy.maximum_sources is not None
                    and len(incoming) > policy.maximum_sources
                ):
                    issues.append(
                        Issue(
                            "mapping.maximum_exceeded",
                            "Object exceeds the declared distinct-source maximum",
                            where,
                            actual=row.object,
                            evidence_json=_cardinality_evidence(
                                "maximum_sources",
                                row.object,
                                policy.maximum_sources,
                                len(incoming),
                                operation,
                            ),
                        )
                    )
            checked += 1
        for subject in subjects:
            operation.consume("work")
            _endpoint(
                subject,
                subject_vocabulary,
                subject_kinds,
                operation,
                issues,
                "coverage",
            )
            if (
                len(targets.get(subject, ())) < policy.minimum_targets
                and subject not in uncertain
            ):
                issues.append(
                    Issue(
                        "mapping.minimum_missing",
                        "Selected subject lacks the required mapping count",
                        "coverage",
                        actual=subject,
                        evidence_json=_cardinality_evidence(
                            "minimum_targets",
                            subject,
                            policy.minimum_targets,
                            len(targets.get(subject, ())),
                            operation,
                        ),
                    )
                )
            checked += 1
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue("mapping.invalid_input", str(error), kind="input_error")
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped))
    return MappingCheck(
        Report(
            "mapping.application",
            checked,
            tuple(issues),
            requested=max(checked, requested),
            regime="application-mapping-contracts",
        )
    )
