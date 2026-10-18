"""Explicit consumer declaration, failure and representative output deltas."""

import json
from dataclasses import fields, replace
from typing import cast

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    TermDeclaration,
)
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.comparison import (
    _associated_occurrences,
    compare_observed_usages,
)
from bijux_ontology.jsonld.observations import ObservedUsage, at_path
from bijux_ontology.jsonld.selectors import (
    _bounded_location,
)
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.models.coverage import source_properties
from bijux_ontology.models.resource_roles.types import RoleUse
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report, combine_reports
from bijux_ontology.workflows.upgrades.assessment import (
    ContractChange,
    _contract_changes,
)

from .preparation import _unknown, _VersionCheck
from .types import (
    ApplicationDeclarationChange,
    ApplicationField,
    ApplicationSampleComparison,
    ApplicationValueChange,
    ApplicationVersion,
)


def _field(version: ApplicationVersion, binding: ApplicationField) -> FieldDeclaration:
    """Resolve only the explicitly named model and field, never infer correspondence."""
    return next(
        field
        for model in version.models.values()
        if model.identifier == binding.model
        for field in model.fields
        if field.name == binding.field
    )


def _declaration_value(
    declaration: TermDeclaration | ModelDeclaration | FieldDeclaration | RoleUse,
    aspect: str,
    budget: Budget,
) -> object:
    """Compare nested model contracts independently of their diagnostic origins."""
    if aspect == "fields" and isinstance(declaration, ModelDeclaration):
        budget.consume("work", len(declaration.fields))
        budget.consume("states", len(declaration.fields))
        return tuple(replace(field, location="") for field in declaration.fields)
    return getattr(declaration, aspect)


def _declarations(
    before: ApplicationVersion | None,
    after: ApplicationVersion,
    budget: Budget,
    issues: IssueCollector,
    changes: list[ApplicationDeclarationChange],
) -> None:
    """Compare matched declarations even when both semantic checks succeed."""
    for kind, left, right in (
        ("term", before.terms if before else {}, after.terms),
        ("model", before.models if before else {}, after.models),
        ("field", before.fields if before else {}, after.fields),
        ("role", before.role_uses if before else {}, after.role_uses),
    ):
        budget.consume("states", len(left) + len(right))
        for identifier in sorted(left.keys() | right.keys()):
            budget.consume("work")
            old_raw, new_raw = left.get(identifier), right.get(identifier)
            old: TermDeclaration | ModelDeclaration | FieldDeclaration | RoleUse
            new: TermDeclaration | ModelDeclaration | FieldDeclaration | RoleUse
            if old_raw is None or new_raw is None:
                issues.append(
                    Issue(
                        "upgrade.application_unverified",
                        "Stable consumer identity is not mapped on both versions",
                        identifier,
                        kind="unverified",
                    )
                )
                continue
            if kind == "field":
                assert before is not None
                old, new = (
                    _field(before, cast(ApplicationField, old_raw)),
                    _field(after, cast(ApplicationField, new_raw)),
                )
            else:
                assert not isinstance(old_raw, ApplicationField) and not isinstance(
                    new_raw, ApplicationField
                )
                old, new = old_raw, new_raw
            if old == new:
                continue
            aspects = tuple(
                item.name
                for item in fields(old)
                if item.name
                not in ("location", "name", "identifier", "field", "samples")
                and _declaration_value(old, item.name, budget)
                != _declaration_value(new, item.name, budget)
            )
            if not aspects:
                continue
            budget.consume("states")
            changes.append(
                ApplicationDeclarationChange(identifier, kind, aspects, old, new)
            )
            issues.append(
                Issue(
                    "upgrade.application_declaration_changed",
                    "Explicitly matched application declaration changed",
                    identifier,
                    severity="info",
                    actual=", ".join(aspects),
                )
            )


def _anchors(
    version: ApplicationVersion | None, sample: str | None, budget: Budget
) -> dict[str, str]:
    """Map established diagnostic locations only; never inspect diagnostic prose."""
    if version is None:
        return {}
    result: dict[str, str] = {}
    ambiguous: set[str] = set()

    def retain(raw: str, stable: str) -> None:
        if not raw:
            return
        if raw in result and result[raw] != stable:
            ambiguous.add(raw)
        result[raw] = stable

    for identifier, term in version.terms.items():
        retain(term.location or term.iri, "term/" + identifier)
    for identifier, model in version.models.items():
        retain(model.location or model.identifier, "model/" + identifier)
    for identifier, use in version.role_uses.items():
        retain(use.location, "role/" + identifier)
    for identifier, binding in version.fields.items():
        field = _field(version, binding)
        retain(field.location or f"{binding.model}.{field.name}", "field/" + identifier)
        if sample is not None and sample in binding.samples:
            retain(
                _bounded_location(binding.samples[sample], budget),
                "field/" + identifier,
            )
    return {raw: stable for raw, stable in result.items() if raw not in ambiguous}


def _contracts(
    name: str,
    old: Report,
    new: Report,
    before: ApplicationVersion | None,
    after: ApplicationVersion,
    sample: str | None,
    budget: Budget,
    target: list[ContractChange],
) -> None:
    """Reuse failure classification with explicit stable diagnostic correspondences."""
    originals: dict[int, Issue] = {}
    uncertain: set[int] = set()
    normalized = []
    for side, report, version in (("before", old, before), ("after", new, after)):
        anchors = _anchors(version, sample, budget)
        transformed = []
        for issue in report.issues:
            for raw in anchors:
                budget.consume("work", 1 + len(raw) + len(issue.location))
            matches = [
                (raw, stable)
                for raw, stable in anchors.items()
                if issue.location == raw
                or stable.startswith("field/")
                and (
                    issue.location.startswith(raw + "[")
                    or issue.location.startswith(raw + "/")
                    or issue.location.startswith(raw + ".")
                )
            ]
            if matches:
                raw, stable = max(matches, key=lambda pair: len(pair[0]))
                where = stable + issue.location[len(raw) :]
            else:
                where = side + "/unattributed/" + issue.location
            bound_text(where, budget)
            item = replace(issue, location=where, details=None)
            originals[id(item)] = issue
            if not matches:
                uncertain.add(id(item))
            transformed.append(item)
        normalized.append(
            Report(
                report.scope,
                report.checked,
                tuple(transformed),
                allow_empty=report.allow_empty,
                requested=report.requested,
                complete=report.complete,
            )
        )
    classified: list[ContractChange] = []
    _contract_changes(name, normalized[0], normalized[1], budget, classified)
    for delta in classified:
        status = delta.status
        if (
            any(
                id(issue) in uncertain
                for issue in (delta.before, delta.after)
                if issue is not None
            )
            or status == "introduced"
            and not old.verified
            or status == "resolved"
            and not new.verified
        ):
            status = "uncertain"
        target.append(
            ContractChange(
                status,
                name,
                originals.get(id(delta.before)),
                originals.get(id(delta.after)),
            )
        )


def _literal_values(usage: ObservedUsage, budget: Budget) -> tuple[str, ...]:
    """Preserve literal spelling and list groups without ordering outer RDF sets."""
    result: list[str] = []
    lists: dict[tuple[str, int | None, int | None], list[tuple[int | None, str]]] = {}
    for item in _associated_occurrences("/value", usage, budget):
        budget.consume("work")
        budget.consume("bytes", 64 + 12 * len(item.value_json))
        value = item.value
        if not isinstance(value, dict) or "@value" not in value:
            continue
        value.pop("@index", None)
        text = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        bound_text(text, budget)
        budget.consume("states")
        budget.consume("bytes", 64 + 2 * len(text) + 12 * len(item.location))
        if item.container == "list":
            key = (item.location, item.list_id, item.list_length)
            lists.setdefault(key, []).append((item.list_position, text))
        else:
            entry = json.dumps(
                ("unordered", item.location, text), separators=(",", ":")
            )
            bound_text(entry, budget)
            result.append(entry)
    for (where, _, length), members in lists.items():
        budget.consume("work", len(members))
        budget.consume(
            "bytes", 64 + 2 * sum(len(text) for _, text in members) + 12 * len(where)
        )
        entry = json.dumps(
            (
                "list",
                where,
                length,
                sorted(members, key=lambda pair: -1 if pair[0] is None else pair[0]),
            ),
            separators=(",", ":"),
        )
        bound_text(entry, budget)
        result.append(entry)
    budget.consume("work", sum(len(text) for text in result))
    return tuple(sorted(result))


def _source_field(
    version: ApplicationVersion,
    prepared: _VersionCheck,
    sample: str,
    binding: ApplicationField,
    budget: Budget,
) -> bool:
    """Require a real model source binding, not only a coincident RDF predicate."""
    selected = prepared.prepared.get(sample)
    if selected is None or selected.view is None:
        return False
    if sample in version.outputs:
        usages = version.outputs[sample].usages
    else:
        usages = next(
            (case.usages for case in version.cases if str(case.identifier) == sample),
            (),
        )
    model = next(
        model for model in version.models.values() if model.identifier == binding.model
    )
    field = _field(version, binding)
    keys = {field.key or field.name}
    if model.coverage is not None:
        budget.consume("states", len(model.coverage.aliases))
        budget.consume("bytes", 64 * len(model.coverage.aliases))
        for key, target in model.coverage.aliases:
            bound_text(key, budget)
            bound_text(target, budget)
            if target == field.name:
                keys.add(key)
    for usage in usages:
        budget.consume("work")
        if usage.model != binding.model or usage.entity or usage.reference is not None:
            continue
        try:
            node = at_path(selected.view._document, usage.path)
        except (KeyError, IndexError):
            continue
        if not isinstance(node, dict):
            continue
        reverse = node.get("@reverse")
        count = len(node) + (len(reverse) if isinstance(reverse, dict) else 0)
        budget.consume("states", count)
        budget.consume("bytes", 64 * count)
        for prop in source_properties(node, usage.path, budget):
            budget.consume("work", len(prop.key) + len(field.key or field.name) + 1)
            if prop.key in keys and prop.path == binding.samples[sample]:
                return True
    return False


def _sample(
    identifier: str,
    before: _VersionCheck,
    after: _VersionCheck,
    budget: Budget,
    issues: IssueCollector,
) -> ApplicationSampleComparison:
    """Compare explicitly paired field observations and one whole owned dataset."""
    old, new = before.samples.get(identifier), after.samples.get(identifier)
    left, right = before.prepared.get(identifier), after.prepared.get(identifier)
    observations: list[tuple[str, ObservedUsage | None, ObservedUsage | None]] = []
    pairs = []
    values = []
    left_fields = before.version.fields if before.version is not None else {}
    assert after.version is not None
    right_fields = after.version.fields
    required = {
        key for key, field in left_fields.items() if identifier in field.samples
    } | {key for key, field in right_fields.items() if identifier in field.samples}
    for consumer in sorted(required):
        budget.consume("work")
        selected: list[ObservedUsage | None] = []
        for source, fields_map in ((left, left_fields), (right, right_fields)):
            binding = fields_map.get(consumer)
            view = source.view if source is not None else None
            if binding is None or identifier not in binding.samples or view is None:
                selected.append(None)
                continue
            try:
                selected.append(view.observe(binding.samples[identifier]))
            except LimitExceeded:
                raise
            except InputError:
                selected.append(None)
        previous, current = selected
        observations.append((consumer, previous, current))
        if previous is None or current is None:
            issues.append(
                Issue(
                    "upgrade.application_unverified",
                    "Required field lacks established source correspondence",
                    consumer,
                    kind="unverified",
                )
            )
            continue
        associated = True
        for version, fields_map, usage, checked in (
            (before.version, left_fields, previous, before),
            (after.version, right_fields, current, after),
        ):
            assert version is not None
            declaration = _field(version, fields_map[consumer])
            if not _source_field(
                version, checked, identifier, fields_map[consumer], budget
            ):
                associated = False
            depth = min((item.depth for item in usage.occurrences), default=0)
            if not any(
                item.depth == depth
                and item.predicate == declaration.predicate
                and item.reverse == declaration.reverse
                for item in usage.occurrences
            ):
                associated = False
        if not associated:
            issues.append(
                Issue(
                    "upgrade.application_unverified",
                    "Selected source usage does not establish the declared field",
                    consumer,
                    kind="unverified",
                )
            )
            continue
        pairs.append((consumer, previous, current))
        if not any(u.limitation or u.dropped or u.empty for u in (previous, current)):
            a, b = _literal_values(previous, budget), _literal_values(current, budget)
            if a != b:
                values.append(ApplicationValueChange(consumer, a, b))
                issues.append(
                    Issue(
                        "upgrade.application_value_changed",
                        "Selected expanded literal representation changed; "
                        "RDF equality is reported separately",
                        consumer,
                        severity="info",
                    )
                )
    mapping = (
        compare_observed_usages(pairs, budget=budget)
        if pairs
        else _unknown(
            "application.mapping",
            "No matched actual field observations were supplied",
            budget,
        )
    )
    if len(pairs) != len(required):
        mapping = combine_reports(
            "application.mapping",
            mapping,
            _unknown(
                "application.mapping.missing",
                "Required field correspondence is incomplete",
                budget,
            ),
        )
    dataset = (
        compare_rdf(left.source, right.source, budget=budget)
        if left is not None
        and right is not None
        and left.source is not None
        and right.source is not None
        else _unknown(
            "application.dataset",
            "Both actual qualified RDF output snapshots are required",
            budget,
        )
    )
    return ApplicationSampleComparison(
        identifier, old, new, tuple(observations), tuple(values), mapping, dataset
    )
