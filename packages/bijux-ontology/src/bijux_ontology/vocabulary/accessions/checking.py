"""Portable finite accession rules over exact explicit source occurrences."""

from collections.abc import Sequence
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    _issue_metadata,
    bound_text,
)
from bijux_ontology.rdf.patterns import matches_pattern
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry

from .checksums import valid_checksum
from .recognition import indexed, prepare_rules, resolve
from .types import AccessionCheck, AccessionObservation, AccessionRule, AccessionValue


def _where(value: AccessionValue, budget: Budget) -> str:
    """Qualify original occurrence metadata before formatting an escaped pointer."""
    _issue_metadata(
        Issue(
            "vocabulary.accession_unverified",
            "Identifier occurrence",
            "accession",
            actual=value.value,
            details=value.location,
        ),
        budget,
    )
    location = value.location
    budget.consume("work", len(location.path) + 1)
    upper = len(location.source.encode("utf-8"))
    for part in location.path:
        text = str(part)
        budget.consume("work", len(text) + 1)
        upper += 1 + 3 * len(text.encode("utf-8"))
    upper = max(
        [upper]
        + [
            len(text.encode("utf-8"))
            for text in (location.model, location.term, location.graph)
            if text
        ]
    )
    if upper > budget.limits.max_bytes:
        raise LimitExceeded("Accession source location exceeds max_bytes")
    budget.consume("bytes", upper)
    budget.consume("work", upper + 1)
    result = str(location)
    bound_text(result, budget)
    return result


def _curie(prefix: str, local: str, budget: Budget) -> str:
    """Reserve a complete suggested representation before joining its parts."""
    size = len(prefix.encode("utf-8")) + 1 + len(local.encode("utf-8"))
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Accession representation exceeds max_bytes")
    budget.consume("bytes", size)
    budget.consume("work", len(prefix) + len(local) + 1)
    return prefix + ":" + local


def _issue(
    issues: IssueCollector,
    value: AccessionValue,
    where: str,
    message: str,
    *,
    unknown: bool = False,
) -> None:
    """Retain actual input identity and caller-provided source attribution."""
    issues.append(
        Issue(
            "vocabulary.accession_unverified"
            if unknown
            else "vocabulary.accession_invalid",
            message,
            where,
            kind="unverified" if unknown else "finding",
            actual=value.value,
            details=value.location,
        )
    )


def check_accessions(
    values: Sequence[AccessionValue],
    registry: IdentifierRegistry,
    *,
    rules: Sequence[AccessionRule],
    unknown_namespaces: Literal["unverified", "exclude"] = "unverified",
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> AccessionCheck:
    """Check explicit registry rules without resolving or rewriting identifiers.

    RE2 grammar, exact template recognition and checksums use their maintained
    optional engines. Unavailable required engines or ambiguous representations
    remain unknown. ASCII canonical case mismatches retain a finding, with a
    suggestion only if its grammar and checksum qualify. Original terms and all
    caller-supplied locations stay untouched. No term existence or RDF/OWL
    equality follows from a valid local identifier or a representation hint.
    """
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Accession limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    observations: list[AccessionObservation] = []
    exclusions = []
    requested = 1
    checked = 0
    try:
        if unknown_namespaces not in ("unverified", "exclude"):
            raise InputError("Unknown namespace scope requires an explicit policy")
        requested = indexed(values, operation)
        owned, prepared = prepare_rules(registry, rules, operation)
        prefixes = dict(owned.canonical_prefixes)
        for index in range(requested):
            operation.consume("work")
            value = values[index]
            if not isinstance(value, AccessionValue):
                raise InputError("Expected typed AccessionValue")
            where = _where(value, operation)
            try:
                rule, local, qualified, ambiguous = resolve(
                    value.value, value.mode, owned, prepared, operation
                )
            except LimitExceeded:
                raise
            except InputError:
                _issue(
                    issues,
                    value,
                    where,
                    "Identifier fails its explicit CURIE or absolute IRI syntax",
                )
                observations.append(AccessionObservation(value, "invalid"))
                checked += 1
                continue
            if rule is None or local is None or not qualified:
                status: Literal["valid", "invalid", "unknown", "excluded"] = "unknown"
                if qualified and rule is None and unknown_namespaces == "exclude":
                    exclusion = f"accession-occurrence:{index}"
                    bound_text(exclusion, operation)
                    exclusions.append(exclusion)
                    status = "excluded"
                else:
                    _issue(
                        issues,
                        value,
                        where,
                        "Namespace, rule or unique alternate representation "
                        "is unavailable",
                        unknown=True,
                    )
                    checked += 1
                observations.append(
                    AccessionObservation(value, status, ambiguous=ambiguous)
                )
                continue
            bound_text(local, operation)
            operation.consume("work", len(local) + 1)
            operation.consume("bytes", len(local.encode("utf-8")))
            normalized = local
            if rule.case_policy != "preserve":
                # A finite ASCII policy cannot silently transform Unicode letters.
                if not local.isascii():
                    _issue(
                        issues,
                        value,
                        where,
                        "Canonical ASCII case policy cannot qualify Unicode local text",
                        unknown=True,
                    )
                    observations.append(
                        AccessionObservation(
                            value, "unknown", rule.namespace, local, ambiguous=False
                        )
                    )
                    checked += 1
                    continue
                normalized = (
                    local.upper()
                    if rule.case_policy == "ascii-upper"
                    else local.lower()
                )
            grammar = matches_pattern(rule.pattern, normalized, "fullmatch", operation)
            checksum: bool | None = True
            if grammar and rule.check_digit is not None:
                checksum = valid_checksum(
                    normalized, rule.check_digit, rule.checksum_separators, operation
                )
            canonical = suggestion = None
            if grammar is None or checksum is None:
                status = "unknown"
                _issue(
                    issues,
                    value,
                    where,
                    "Required grammar or checksum needs a supported "
                    "[patterns] or [checksums] backend",
                    unknown=True,
                )
            elif not grammar or not checksum:
                status = "invalid"
                _issue(
                    issues,
                    value,
                    where,
                    "Local identifier fails its declared grammar or checksum",
                )
            else:
                candidate = _curie(prefixes[rule.namespace], normalized, operation)
                if normalized != local:
                    status = "invalid"
                    suggestion = candidate
                    _issue(
                        issues,
                        value,
                        where,
                        "Local identifier does not use the declared "
                        "canonical ASCII case",
                    )
                else:
                    status = "valid"
                    canonical = candidate
                    if candidate != str(value.value):
                        suggestion = candidate
            observations.append(
                AccessionObservation(
                    value,
                    status,
                    rule.namespace,
                    local,
                    canonical,
                    suggestion,
                    ambiguous=False,
                )
            )
            checked += 1
        if checked == 0:
            issues.append(
                Issue(
                    "vocabulary.accession_unverified",
                    "No required accession occurrence was checked",
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, IndexError, KeyError):
        try:
            issues.append(
                Issue(
                    "vocabulary.accession_input_error",
                    "Accession policy or occurrence could not be qualified",
                    kind="input_error",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error))
    return AccessionCheck(
        tuple(observations),
        Report(
            "vocabulary.accessions",
            checked,
            tuple(issues),
            requested=max(checked, requested - len(exclusions)),
            complete=len(observations) == requested,
            exclusions=tuple(exclusions),
            regime="explicit-registry-accession-rules",
        ),
    )
