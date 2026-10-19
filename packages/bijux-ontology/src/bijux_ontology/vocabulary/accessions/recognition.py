"""Bounded registry association and exact maintained alternate recognition."""

from collections.abc import Sequence

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.vocabulary.registry.types import (
    IdentifierRegistry,
    NamespaceContribution,
)

from .types import AccessionRule, _text


def indexed(value: object, budget: Budget) -> int:
    """Reserve a finite indexed inventory once, without mutable length rechecks."""
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise InputError("Accession inputs require finite typed sequences")
    count = len(value)
    budget.consume("states", count)
    budget.consume("bytes", 64 * count)
    return count


def prepare_rules(
    registry: IdentifierRegistry, rules: Sequence[AccessionRule], budget: Budget
) -> tuple[IdentifierRegistry, dict[str, AccessionRule]]:
    """Requalify owned registry/rule metadata before any new dictionaries or keys."""
    if not isinstance(registry, IdentifierRegistry):
        raise InputError("Accessions require an IdentifierRegistry")
    count = indexed(registry.contributions, budget)
    contributions = []
    largest = 1
    for index in range(count):
        budget.consume("work")
        item = registry.contributions[index]
        namespace = _text(item.namespace, budget)
        prefix = _text(item.canonical_prefix, budget)
        owner = _text(item.owner, budget)
        aliases = []
        for offset in range(indexed(item.aliases, budget)):
            budget.consume("work")
            aliases.append(_text(item.aliases[offset], budget))
        budget.consume(
            "work", (len(aliases) + 1) ** 2 * (1 + max(map(len, aliases), default=0))
        )
        largest = max(
            largest, len(namespace) + len(prefix) + len(owner) + sum(map(len, aliases))
        )
        contributions.append(
            NamespaceContribution(namespace, prefix, owner, tuple(aliases))
        )
    choices = {}
    for index in range(indexed(registry.canonical_prefixes, budget)):
        namespace, prefix = registry.canonical_prefixes[index]
        namespace = _text(namespace, budget)
        prefix = _text(prefix, budget)
        choices[namespace] = prefix
    # Reuse the existing conflict/alias/namespace policy, with accepted string
    # subclasses captured as text before its ordinary dictionary operations.
    budget.consume("work", 4 * count * (count.bit_length() + 1) * largest)
    owned = IdentifierRegistry(tuple(contributions), canonical_prefixes=choices)
    prepared = {}
    for index in range(indexed(rules, budget)):
        budget.consume("work")
        rule = rules[index]
        if not isinstance(rule, AccessionRule):
            raise InputError("Expected typed AccessionRule")
        namespace = _text(rule.namespace, budget)
        bound_text(rule.pattern, budget)
        bound_text(rule.case_policy, budget)
        bound_text(rule.checksum_separators, budget)
        if rule.check_digit is not None:
            bound_text(rule.check_digit, budget)
        for offset in range(indexed(rule.alternates, budget)):
            alternate = rule.alternates[offset]
            bound_text(alternate.template, budget)
            for key_index in range(indexed(alternate.keys, budget)):
                key = alternate.keys[key_index]
                bound_text(key.name, budget)
                bound_text(key.kind, budget)
        if namespace not in choices or namespace in prepared:
            raise InputError("Rules require distinct contributed namespaces")
        prepared[namespace] = rule
    return owned, prepared


def resolve(
    value: str,
    mode: str,
    registry: IdentifierRegistry,
    rules: dict[str, AccessionRule],
    budget: Budget,
) -> tuple[AccessionRule | None, str | None, bool, bool | None]:
    """Recognize all applicable representations without guessing between intents.

    A failed candidate match is ordinary nonmembership, not a discarded finding.
    Unsupported alternate qualification remains required unknown even beside a
    supported match. Distinct recognized accession records are ambiguous.
    """
    value = _text(value, budget)
    if mode == "curie":
        budget.consume("work", len(value) + len(registry.bindings))
        if ":" not in value:
            raise InputError("A CURIE requires an explicit prefix")
        budget.consume("bytes", len(value.encode("utf-8")))
        prefix, local = value.split(":", 1)
        bindings = dict(registry.bindings)
        namespace = bindings.get(prefix)
        if namespace is None:
            return None, None, True, False
        expanded = len(namespace.encode("utf-8")) + len(local.encode("utf-8"))
        if expanded > budget.limits.max_bytes:
            from bijux_ontology.diagnostics.errors import LimitExceeded

            raise LimitExceeded("Expanded accession exceeds max_bytes")
        budget.consume("bytes", expanded)
        budget.consume("work", len(namespace) + len(local) + 1)
        budget.consume(
            "work", len(value) + sum(len(p) + len(n) for p, n in registry.bindings)
        )
        registry.expand_curie(value)
        return rules.get(namespace), local, namespace in rules, False
    absolute_iri(value)
    best: str | None = None
    for namespace, _prefix in registry.canonical_prefixes:
        budget.consume("work", len(value) + len(namespace) + 1)
        if value.startswith(namespace) and (best is None or len(namespace) > len(best)):
            best = namespace
    candidates: dict[tuple[str, str], AccessionRule] = {}
    qualified = best is None or best in rules
    if best is not None and best in rules:
        budget.consume("bytes", len(value.encode("utf-8")))
        budget.consume("states")
        candidates[(best, value[len(best) :])] = rules[best]
    for rule in rules.values():
        budget.consume("work")
        for alternate in rule.alternates:
            budget.consume("work")
            try:
                record = alternate.match(value, budget=budget)
            except ResourceError:
                qualified = False
                continue
            if record is not None:
                binding = record.bindings[0][1]
                if not isinstance(binding, str):
                    raise InputError("Alternate recognition did not return text")
                local = binding
                budget.consume("work", len(rule.namespace) + len(local) + 1)
                budget.consume("states")
                candidates[(rule.namespace, local)] = rule
    if len(candidates) != 1:
        ambiguous = True if len(candidates) > 1 else False if qualified else None
        return None, None, qualified and not candidates, ambiguous
    (namespace, local), rule = next(iter(candidates.items()))
    return rule, local, qualified, False if qualified else None
