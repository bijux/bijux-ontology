"""Language-ranked written forms reuse the bounded RFC 4647 primitives."""

from collections.abc import Sequence
from typing import Literal, cast

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.languages.matching import _lookup, _range, _tag

from .reading import reserve
from .types import ONTOLEX as O
from .types import Lexicon, WrittenMatch, WrittenSelection
from .writing import _budget

Fallback = Literal["untagged", "other"]


def lookup_written(
    lexicon: Lexicon,
    *,
    ranges: Sequence[str],
    fallback: Sequence[Fallback] = (),
    roles: Sequence[str] = ("canonical", "other", "lexical"),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> WrittenSelection:
    """Choose the winning language and form-role tier, retaining all original ties.

    Ranges use RFC 4647 lookup truncation. Fallback tiers are caller-selected;
    omitted roles cannot reappear through fallback. Every match retains its entry,
    form resource, authored role and separate lexical senses. This selects labels,
    without deciding synonymy or linguistic completeness.
    """
    operation = _budget(limits, budget)
    issues = IssueCollector(operation)
    children: tuple[Report, ...] = ()
    matches: tuple[WrittenMatch, ...] = ()
    checked = 0
    complete = False
    try:
        if type(lexicon) is not Lexicon:
            raise InputError("Written lookup requires an owned Lexicon")
        children = (lexicon.report,)
        retain_report(lexicon.report, operation, issues)
        inventories = (ranges, fallback, roles)
        if any(
            not isinstance(v, Sequence) or isinstance(cast(object, v), str | bytes)
            for v in inventories
        ):
            raise InputError("Language preferences require finite sequences")
        reserve(operation, len(ranges) + len(fallback) + len(roles))
        compiled = tuple(
            _range(ranges[i], False, operation) for i in range(len(ranges))
        )
        tiers = tuple(fallback[i] for i in range(len(fallback)))
        selected_roles = tuple(roles[i] for i in range(len(roles)))
        for value in (*tiers, *selected_roles):
            if type(value) is not str:
                raise InputError("Language and role preferences need ordinary text")
            bound_text(value, operation)
        if any(t not in ("untagged", "other") for t in tiers) or len(set(tiers)) != len(
            tiers
        ):
            raise InputError("Select distinct untagged/other fallback tiers")
        if (
            not selected_roles
            or any(r not in ("canonical", "other", "lexical") for r in selected_roles)
            or len(set(selected_roles)) != len(selected_roles)
        ):
            raise InputError("Select distinct canonical/other/lexical role priorities")
        predicates = {
            "canonical": O.canonicalForm,
            "other": O.otherForm,
            "lexical": O.lexicalForm,
        }
        priority = {predicates[role]: i for i, role in enumerate(selected_roles)}
        candidates = []
        for entry in lexicon.entries:
            for link in entry.forms:
                operation.consume("work")
                if link.predicate not in priority:
                    continue
                for written in link.form.written:
                    bound_node(written, operation)
                    tag = _tag(written.language, operation)
                    reserve(operation, 2)
                    candidates.append(
                        (
                            WrittenMatch(entry, link, written),
                            tag,
                            priority[link.predicate],
                        )
                    )
        checked = len(candidates)
        for wanted in compiled:
            for tag in _lookup(wanted, operation):
                winners = []
                for match, actual, rank in candidates:
                    operation.consume("work", len(tag) + 1)
                    if actual == tag:
                        reserve(operation)
                        winners.append((match, rank))
                if winners:
                    best = min(rank for _, rank in winners)
                    matches = tuple(match for match, rank in winners if rank == best)
                    break
            if matches:
                break
        if not matches:
            for tier in tiers:
                winners = []
                for match, tag, rank in candidates:
                    operation.consume("work")
                    if (tag is None) == (tier == "untagged"):
                        reserve(operation)
                        winners.append((match, rank))
                if winners:
                    best = min(rank for _, rank in winners)
                    matches = tuple(match for match, rank in winners if rank == best)
                    break
        complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(Issue("ontolex.input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return WrittenSelection(
        matches,
        Report(
            "adapter.ontolex.written_selection",
            checked,
            tuple(issues),
            requested=checked if complete else checked + 1,
            allow_empty=True,
            complete=complete,
            children=children,
            regime="rfc4647-lookup-and-form-roles",
        ),
    )
