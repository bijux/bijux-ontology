"""Compare explicit instance releases without inventing a schema-upgrade event."""

import threading
from collections.abc import Sequence
from itertools import chain

from rdflib import BNode, Literal, URIRef

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.patches.correspondence import Operation, compare_witnesses
from bijux_ontology.rdf.patches.planning import (
    Captured,
    _plan_captured_patch,
    anonymous,
    capture_items,
    capture_source,
    replace_witness,
)
from bijux_ontology.rdf.patches.types import (
    BlankNodePair,
    DatasetPatch,
    PatchBlank,
    PatchLiteral,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    PatchValue,
)
from bijux_ontology.rdf.paths import RdfPath, capture_path
from bijux_ontology.rdf.terms import bound_text, literal_term
from bijux_ontology.rdf.value_comparison.comparing import compare_literal_values
from bijux_ontology.rdf.value_comparison.types import (
    LiteralValueComparison,
)
from bijux_ontology.reports import Issue, Report

from .observations import (
    EntityKey,
    SelectedEntity,
    observe,
    record_support,
    select_entity,
)
from .types import (
    InstanceEntity,
    InstanceEntityPair,
    InstanceFieldChange,
    InstanceFieldPolicy,
    InstanceRelease,
    InstanceReleaseComparison,
    InstanceSharedNodeChange,
)


def _reserve(budget: Budget, count: int = 1) -> None:
    """Prepay owned result/container references before constructing them."""
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)


def _normalize(
    values: tuple[PatchValue, ...],
    mapping: dict[PatchBlank, PatchBlank],
    budget: Budget,
) -> tuple[PatchValue, ...]:
    """Apply only established blank identity; exact IRI/literal spelling is retained."""
    _reserve(budget, len(values))
    budget.consume("work", len(values))
    return tuple(
        mapping.get(value, value) if isinstance(value, PatchBlank) else value
        for value in values
    )


def _literal(value: PatchLiteral) -> Literal:
    """Use the existing lexical-preserving constructor for native value comparison."""
    return literal_term(
        value.lexical,
        datatype=None if value.language else value.datatype,
        language=value.language,
    )


def _relation(
    left: PatchValue,
    right: PatchValue,
    policy: InstanceFieldPolicy,
    budget: Budget,
    issues: IssueCollector,
    proofs: list[LiteralValueComparison],
) -> bool | None:
    """Keep exact identity distinct from caller-selected literal value equality."""
    budget.consume("work")
    if (
        policy.equality == "rdf-term"
        or not isinstance(left, PatchLiteral)
        or not isinstance(right, PatchLiteral)
    ):
        return left == right
    _reserve(budget)
    size = sum(
        len(text)
        for value in (left, right)
        for text in (value.lexical, value.datatype, value.language or "")
    )
    budget.consume("work", size)
    budget.consume("bytes", 32 * size + 128)
    comparison = compare_literal_values(
        _literal(left),
        _literal(right),
        regime=policy.equality,
        budget=budget,
    )
    issues.retain_completed(comparison.report)
    proofs.append(comparison)
    retain_report(comparison.report, budget, issues)
    return (
        True
        if comparison.relation == "equal"
        else False
        if comparison.relation == "different"
        else None
    )


def _included(
    left: tuple[PatchValue, ...],
    right: tuple[PatchValue, ...],
    policy: InstanceFieldPolicy,
    budget: Budget,
    issues: IssueCollector,
    proofs: list[LiteralValueComparison],
) -> bool | None:
    """Establish set inclusion without choosing a greedy value pairing."""
    unknown = False
    for value in left:
        found = False
        uncertain = False
        for candidate in right:
            result = _relation(value, candidate, policy, budget, issues, proofs)
            found |= result is True
            uncertain |= result is None
        if not found and not uncertain:
            return False
        unknown |= not found
    return None if unknown else True


def _accepted(
    before: tuple[PatchValue, ...],
    after: tuple[PatchValue, ...],
    policy: InstanceFieldPolicy,
    budget: Budget,
    issues: IssueCollector,
    proofs: list[LiteralValueComparison],
) -> bool | None:
    """Apply finite ordered-prefix or set semantics under the named equality regime."""
    if policy.change == "allow":
        return True
    if policy.collection == "list":
        if (
            len(before) > len(after)
            or policy.change == "immutable"
            and len(before) != len(after)
        ):
            return False
        unknown = False
        for left, right in zip(before, after, strict=False):
            equal = _relation(left, right, policy, budget, issues, proofs)
            if equal is False:
                return False
            unknown |= equal is None
        return None if unknown else True
    included = _included(before, after, policy, budget, issues, proofs)
    if policy.change == "append-only" or included is False:
        return included
    reverse = _included(after, before, policy, budget, issues, proofs)
    return (
        False
        if reverse is False
        else None
        if included is None or reverse is None
        else True
    )


def _policies(
    values: Sequence[InstanceFieldPolicy], operation: Budget
) -> tuple[tuple[InstanceFieldPolicy, RdfPath], ...]:
    """Capture actual path objects and explicit policy choices before evaluation."""
    captured = capture_items(values, InstanceFieldPolicy, operation)
    result: list[tuple[InstanceFieldPolicy, RdfPath]] = []
    names: set[str] = set()
    for value in captured:
        bound_text(value.name, operation)
        if not value.name or value.name.isspace() or value.name in names:
            raise InputError("Instance field policy names must be nonempty and unique")
        if (
            value.change not in ("immutable", "append-only", "allow")
            or value.equality not in ("rdf-term", "sparql-1.1", "owl2-datatype-map")
            or value.collection not in ("set", "list")
        ):
            raise InputError("Unknown explicit instance field policy")
        _reserve(operation, 2)
        names.add(value.name)
        result.append((value, capture_path(value.path, budget=operation)))
    return tuple(result)


def _entities(
    release: InstanceRelease, captured: Captured, operation: Budget
) -> dict[EntityKey, SelectedEntity]:
    """Validate actual selected subjects before treating absence as record deletion."""
    result: dict[EntityKey, SelectedEntity] = {}
    for entity in capture_items(release.entities, InstanceEntity, operation):
        selected = select_entity(release.source, captured, entity, operation)
        if selected.key in result:
            raise InputError(
                "Selected instance entities must be unique per physical graph"
            )
        _reserve(operation)
        result[selected.key] = selected
    return result


def _shared(
    patch: DatasetPatch, operation: Budget
) -> tuple[InstanceSharedNodeChange, ...]:
    """Expose shared-resource reference/description changes from the actual patch."""
    touched: set[PatchResource] = set()
    for quad in chain(patch.removals, patch.additions):
        operation.consume("work")
        _reserve(operation, 2)
        touched.add(quad.subject)
        if isinstance(quad.value, URIRef | PatchBlank):
            touched.add(quad.value)
    result: list[InstanceSharedNodeChange] = []
    for node in touched:
        groups: list[tuple[PatchQuad, ...]] = []
        for witness, incoming in (
            (patch.base, True),
            (patch.target, True),
            (patch.base, False),
            (patch.target, False),
        ):
            matches: list[PatchQuad] = []
            for quad in witness.quads:
                operation.consume("work")
                if (quad.value if incoming else quad.subject) == node:
                    _reserve(operation)
                    matches.append(quad)
            _reserve(operation)
            groups.append(tuple(matches))
        before_in, after_in, before_out, after_out = groups
        operation.consume("work", sum(map(len, groups)))
        _reserve(operation, sum(map(len, groups)))
        if max(len(before_in), len(after_in)) > 1 and (
            set(before_in) != set(after_in) or set(before_out) != set(after_out)
        ):
            _reserve(operation)
            result.append(
                InstanceSharedNodeChange(
                    node, before_in, after_in, before_out, after_out
                )
            )
    return tuple(result)


def compare_instance_releases(
    before: InstanceRelease,
    after: InstanceRelease,
    *,
    fields: Sequence[InstanceFieldPolicy] = (),
    entity_pairs: Sequence[InstanceEntityPair] = (),
    blank_pairs: Sequence[BlankNodePair] = (),
    created_blanks: Sequence[BNode] = (),
    removed_blanks: Sequence[BNode] = (),
    append_only: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> InstanceReleaseComparison:
    """Compare instance data independently of ontology/context/model/shape changes.

    The exact dataset patch retains the complete source selection, graph identities,
    empty graphs and lexical terms. Entity/path policies cover only the explicit
    physical record selection. Append-only protects existing root outgoing facts
    and reachable blank/list support in that graph; named child descriptions need
    their own selected record or path. No labels, minimal edits, candidate union,
    inferred OWL identity or source mutations establish historical correspondence.
    """
    if (
        not isinstance(before, InstanceRelease)
        or not isinstance(after, InstanceRelease)
        or type(append_only) is not bool
    ):
        raise InputError(
            "Expected typed instance releases and an explicit append-only switch"
        )
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    patch: DatasetPatch | None = None
    added: tuple[InstanceEntity, ...] = ()
    removed: tuple[InstanceEntity, ...] = ()
    changes: list[InstanceFieldChange] = []
    shared: tuple[InstanceSharedNodeChange, ...] = ()
    complete = False
    checked = 0
    try:
        policies = _policies(fields, operation)
        pairs = capture_items(blank_pairs, BlankNodePair, operation)
        created = capture_items(created_blanks, BNode, operation)
        deleted = capture_items(removed_blanks, BNode, operation)
        declarations = capture_items(entity_pairs, InstanceEntityPair, operation)
        left = capture_source(before.source, "base:", operation)
        right = capture_source(after.source, "new:", operation)
        old, new = (
            _entities(before, left, operation),
            _entities(after, right, operation),
        )
        if not old and not new:
            raise PatchUnavailable(
                "No actual instance entity selection was established"
            )
        patch = _plan_captured_patch(
            left,
            right,
            pairs=pairs,
            created=created,
            removed=deleted,
            operation=operation,
        )
        checked += 1
        mapping: dict[PatchBlank, PatchBlank] = {}
        for pair in pairs:
            _reserve(operation)
            mapping[right.reference(pair.after)] = left.reference(pair.before)
        _reserve(operation, len(deleted) + len(created))
        deleted_refs = {left.reference(node) for node in deleted}
        created_refs = {right.reference(node) for node in created}
        if not left.owned:
            _reserve(operation, len(right.owned))
            created_refs.update(ref for _, ref in right.owned.values())
        if not right.owned:
            _reserve(operation, len(left.owned))
            deleted_refs.update(ref for _, ref in left.owned.values())
        matched: dict[EntityKey, EntityKey] = {}
        used: set[EntityKey] = set()
        for declaration in declarations:
            old_key = select_entity(
                before.source, left, declaration.before, operation
            ).key
            new_key = select_entity(
                after.source, right, declaration.after, operation
            ).key
            if (
                old_key not in old
                or new_key not in new
                or old_key in matched
                or new_key in used
            ):
                raise InputError(
                    "Entity pairs must be unique and belong to both selections"
                )
            if (
                isinstance(old_key[0], PatchBlank) or isinstance(new_key[0], PatchBlank)
            ) and (
                not isinstance(old_key[0], PatchBlank)
                or not isinstance(new_key[0], PatchBlank)
                or mapping.get(new_key[0]) != old_key[0]
            ):
                raise PatchUnavailable(
                    "Blank entities require established dataset patch correspondence"
                )
            if (
                isinstance(old_key[1], PatchBlank) or isinstance(new_key[1], PatchBlank)
            ) and (
                not isinstance(old_key[1], PatchBlank)
                or not isinstance(new_key[1], PatchBlank)
                or mapping.get(new_key[1]) != old_key[1]
            ):
                raise PatchUnavailable(
                    "Blank graph identity requires dataset patch correspondence"
                )
            _reserve(operation, 2)
            matched[old_key] = new_key
            used.add(new_key)
        for key in old:
            operation.consume("work")
            if key in matched:
                continue
            candidates: list[EntityKey] = []
            for candidate in new:
                operation.consume("work")
                _reserve(operation, 2)
                normalized_key = (
                    mapping.get(candidate[0], candidate[0])
                    if isinstance(candidate[0], PatchBlank)
                    else candidate[0],
                    mapping.get(candidate[1], candidate[1])
                    if isinstance(candidate[1], PatchBlank)
                    else candidate[1],
                )
                if candidate not in used and normalized_key == key:
                    _reserve(operation)
                    candidates.append(candidate)
            if len(candidates) == 1:
                _reserve(operation, 2)
                matched[key] = candidates[0]
                used.add(candidates[0])
            elif any(
                isinstance(part, PatchBlank) and part not in deleted_refs
                for part in key
            ):
                raise PatchUnavailable(
                    "Selected blank identity needs correspondence or removal evidence"
                )
        _reserve(operation, len(old) + len(new))
        removed = tuple(
            entity.original for key, entity in old.items() if key not in matched
        )
        added = tuple(entity.original for key, entity in new.items() if key not in used)
        if append_only and removed:
            issues.append(
                Issue(
                    "instance.entity_removal_forbidden",
                    "Append-only release removes a selected existing record",
                    "instances.entities",
                )
            )
        # A completed ground-only plan proves anonymous content unchanged without
        # assigning historical identity to symmetric anonymous resources.
        anonymous_edits = any(
            isinstance(term, PatchBlank)
            for quad in chain(patch.removals, patch.additions)
            for term in (quad.subject, quad.value, quad.graph)
        )
        normalized_right = replace_witness(right.witness, mapping, operation)
        if append_only:
            _reserve(operation, len(normalized_right.quads))
            available = set(normalized_right.quads)
            for key in matched:
                selected = record_support(left, key, operation)
                missing = False
                for quad in selected:
                    operation.consume("work")
                    if (
                        not anonymous_edits
                        and not pairs
                        and any(
                            isinstance(term, PatchBlank)
                            for term in (quad.subject, quad.value, quad.graph)
                        )
                    ):
                        continue
                    missing |= quad not in available
                if missing:
                    issues.append(
                        Issue(
                            "instance.record_change_forbidden",
                            "Append-only release alters a protected record or support",
                            old[key].original.subject,
                        )
                    )
                checked += 1
        _reserve(operation, len(old) + len(new))
        rows: list[tuple[SelectedEntity | None, SelectedEntity | None]] = [
            (entity, new[matched[key]] if key in matched else None)
            for key, entity in old.items()
        ]
        rows.extend((None, entity) for key, entity in new.items() if key not in used)
        for a, b in rows:
            for policy, path in policies:
                proofs: list[LiteralValueComparison] = []
                previous: tuple[PatchValue, ...] = ()
                current: tuple[PatchValue, ...] = ()
                support_before: tuple[PatchQuad, ...] = ()
                support_after: tuple[PatchQuad, ...] = ()
                exact: bool | None = None
                accepted: bool | None = None
                anchor = a if a is not None else b
                assert anchor is not None
                operation.consume(
                    "bytes", 4 * (len(policy.name) + len(anchor.original.subject) + 1)
                )
                where = policy.name + ":" + str(anchor.original.subject)
                bound_text(where, operation)
                try:
                    if a is not None:
                        previous, support_before = observe(
                            a, left, path, policy.collection, operation
                        )
                    if b is not None:
                        current, support_after = observe(
                            b, right, path, policy.collection, operation
                        )
                    _reserve(operation, len(previous) + len(mapping))
                    unresolved = (
                        {
                            value.token
                            for value in previous
                            if isinstance(value, PatchBlank)
                        }
                        - {value.token for value in mapping.values()}
                        - {value.token for value in deleted_refs}
                    )
                    if unresolved and not anonymous_edits:
                        try:
                            _, established = compare_witnesses(
                                anonymous(left.witness, operation),
                                anonymous(right.witness, operation),
                                operation,
                                required=tuple(unresolved),
                                fixed=tuple(
                                    (v.token, k.token) for k, v in mapping.items()
                                ),
                            )
                            for old_token, new_token in established.items():
                                _reserve(operation)
                                mapping[PatchBlank(new_token)] = PatchBlank(old_token)
                        except PatchUnavailable:
                            pass
                    normalized = _normalize(current, mapping, operation)
                    unknown_blank = any(
                        isinstance(value, PatchBlank)
                        and value not in mapping.values()
                        and value not in deleted_refs
                        for value in previous
                    ) or any(
                        isinstance(value, PatchBlank)
                        and value not in mapping
                        and value not in created_refs
                        for value in current
                    )
                    if unknown_blank:
                        raise PatchUnavailable(
                            "Selected anonymous endpoint identity is not established"
                        )
                    operation.consume("work", len(previous) + len(normalized))
                    _reserve(operation, len(previous) + len(normalized))
                    exact = (
                        previous == normalized
                        if policy.collection == "list"
                        else set(previous) == set(normalized)
                    )
                    accepted = (
                        True
                        if a is None
                        else _accepted(
                            previous, normalized, policy, operation, issues, proofs
                        )
                    )
                except LimitExceeded:
                    raise
                except (PatchUnavailable, ResourceError, InputError) as error:
                    issues.append(
                        Issue(
                            "instance.path_unverified",
                            str(error),
                            where,
                            kind="unverified",
                        )
                    )
                if accepted is False:
                    issues.append(
                        Issue(
                            "instance.field_change_forbidden",
                            "Field change violates the selected release policy",
                            where,
                        )
                    )
                elif accepted is None:
                    issues.append(
                        Issue(
                            "instance.change_unverified",
                            "The selected field change policy was not established",
                            where,
                            kind="unverified",
                        )
                    )
                _reserve(operation)
                changes.append(
                    InstanceFieldChange(
                        a.original if a else None,
                        b.original if b else None,
                        policy,
                        path,
                        previous,
                        current,
                        support_before,
                        support_after,
                        exact,
                        accepted,
                        tuple(proofs),
                    )
                )
                checked += 1
        shared = _shared(patch, operation)
        operation.consume("work", 0)
        complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="instances")
    except (PatchUnavailable, ResourceError) as error:
        try:
            issues.append(
                Issue(
                    "instance.release_unverified",
                    str(error),
                    "instances",
                    kind="unverified",
                )
            )
        except LimitExceeded as limit:
            issues.exhausted(str(limit), location="instances")
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "instance.release_input_error",
                    str(error),
                    "instances",
                    kind="input_error",
                )
            )
        except LimitExceeded as limit:
            issues.exhausted(str(limit), location="instances")
    report = Report(
        "instance-release",
        checked,
        tuple(issues),
        requested=max(1, checked),
        complete=complete
        and not any(
            issue.kind in ("unverified", "exhausted", "input_error") for issue in issues
        ),
        regime="asserted-instance-release",
        families=("entity", "path", "dataset-patch"),
    )
    return InstanceReleaseComparison(
        before, after, patch, added, removed, tuple(changes), shared, report
    )
