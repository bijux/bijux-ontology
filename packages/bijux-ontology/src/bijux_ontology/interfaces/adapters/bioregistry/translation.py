"""Conservative source-field interpretation and explicit local policy composition."""

from collections.abc import Mapping

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue, Location
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.vocabulary.accessions.recognition import prepare_rules
from bijux_ontology.vocabulary.accessions.types import AccessionRule
from bijux_ontology.vocabulary.registry.types import (
    IdentifierRegistry,
    NamespaceContribution,
    _prefix,
)

from .types import BioregistryOverride, _text

_INTERPRETED = frozenset(("preferred_prefix", "synonyms", "uri_format"))
_RULE_FIELDS = (
    "pattern",
    "banana",
    "banana_peel",
    "namespace_in_lui",
    "provides_for",
    "has_canonical",
)


def qualify_override(value: BioregistryOverride, budget: Budget) -> BioregistryOverride:
    """Capture local choices and all embedded rule metadata before retaining them."""
    prefix = _prefix(_text(value.prefix, budget))
    namespace = None if value.namespace is None else _text(value.namespace, budget)
    if namespace is not None:
        absolute_iri(namespace)
    canonical = (
        None
        if value.canonical_prefix is None
        else _prefix(_text(value.canonical_prefix, budget))
    )
    budget.consume("states", len(value.aliases))
    aliases = tuple(_prefix(_text(alias, budget)) for alias in value.aliases)
    rule = value.rule
    if rule is not None:
        for text in (
            rule.namespace,
            rule.pattern,
            rule.case_policy,
            rule.checksum_separators,
        ):
            _text(text, budget)
        if rule.check_digit is not None:
            _text(rule.check_digit, budget)
        budget.consume("states", len(rule.alternates))
        for alternate in rule.alternates:
            _text(alternate.template, budget)
            budget.consume("states", len(alternate.keys))
            for key in alternate.keys:
                _text(key.name, budget)
                _text(key.kind, budget)
    return BioregistryOverride(prefix, namespace, canonical, aliases, rule)


def translate(
    records: Mapping[str, dict[str, object]],
    overrides: Mapping[str, BioregistryOverride],
    budget: Budget,
    issues: IssueCollector,
    unsupported: list[tuple[str, str]],
    uninterpreted: list[tuple[str, str]],
) -> tuple[IdentifierRegistry | None, tuple[AccessionRule, ...]]:
    """Build only exact namespace policy; source regexes are never reinterpreted."""
    contributions = []
    rules = []
    largest = 1

    def unknown(prefix: str, field: str, message: str) -> None:
        """Retain exact field attribution beside required unsupported semantics."""
        budget.consume("states")
        unsupported.append((prefix, field))
        issues.append(
            Issue(
                "adapter.bioregistry_unverified",
                message,
                kind="unverified",
                details=Location(path=(prefix, field)),
            )
        )

    for prefix, record in records.items():
        budget.consume("work")
        override = overrides.get(prefix)
        source_canonical = _prefix(
            _text(record.get("preferred_prefix", prefix), budget)
        )
        canonical: object = source_canonical
        if override is not None and override.canonical_prefix is not None:
            canonical = override.canonical_prefix
        canonical = _prefix(_text(canonical, budget))
        aliases = [prefix, source_canonical]
        supplied = record.get("synonyms", [])
        if not isinstance(supplied, list):
            raise InputError("Bioregistry synonyms require an array")
        budget.consume("states", len(supplied))
        for alias in supplied:
            text = _text(alias, budget)
            try:
                aliases.append(_prefix(text))
            except InputError:
                unknown(
                    prefix,
                    "synonyms",
                    "A source alias is outside the supported prefix-label profile",
                )
        if override is not None:
            aliases.extend(override.aliases)
        namespace = override.namespace if override is not None else None
        if namespace is None:
            formatter = record.get("uri_format")
            if formatter is not None:
                formatter = _text(formatter, budget)
                budget.consume("work", 2 * len(formatter) + 1)
                if formatter.endswith("$1") and formatter.count("$") == 1:
                    namespace = _text(formatter[:-2], budget)
                    absolute_iri(namespace)
        if namespace is None:
            unknown(
                prefix,
                "uri_format",
                "A literal namespace followed by one terminal $1 "
                "or an explicit local namespace is required",
            )
        for field in record:
            budget.consume("work")
            _text(field, budget)
            if field not in _INTERPRETED:
                budget.consume("states")
                uninterpreted.append((prefix, field))
        rule = override.rule if override is not None else None
        for field in _RULE_FIELDS:
            budget.consume("work")
            if (
                field != "pattern"
                and field in record
                and record[field] is not None
                and record[field] is not False
                and rule is None
            ):
                unknown(
                    prefix,
                    field,
                    "Source identifier semantics require an explicit local "
                    "AccessionRule replacement",
                )
        if rule is None:
            unknown(
                prefix,
                "pattern",
                "Source patterns are not executable local validation rules; "
                "an explicit AccessionRule replacement is required",
            )
        elif namespace is not None:
            if rule.namespace != namespace:
                raise InputError(
                    "A local rule must name the selected effective namespace"
                )
            rules.append(rule)
        if namespace is not None:
            budget.consume(
                "work",
                (len(aliases) + 1) ** 2 * (1 + max(map(len, aliases), default=0)),
            )
            largest = max(
                largest, len(namespace) + len(canonical) + sum(map(len, aliases))
            )
            contributions.append(
                NamespaceContribution(namespace, canonical, prefix, tuple(aliases))
            )
    if not contributions or len(contributions) != len(records):
        return None, ()
    count = len(contributions)
    budget.consume("work", 4 * count * (count.bit_length() + 1) * largest)
    registry = IdentifierRegistry(tuple(contributions))
    owned, captured = prepare_rules(registry, tuple(rules), budget)
    return owned, tuple(captured.values())
