"""Opt-in scheme-local naming policies over unchanged plain SKOS label terms."""

import json
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal as Mode
from typing import TypeVar

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import SKOS, XSD
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.inspection import VocabularyView, inspect_vocabulary
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.results import Edge
from bijux_ontology.vocabulary.view import Vocabulary

from .structure import structure

Comparison = Mode["exact", "casefold", "nfc", "nfc-casefold"]
_ROLES = frozenset(map(str, (SKOS.prefLabel, SKOS.altLabel, SKOS.hiddenLabel)))
T = TypeVar("T")
Key = tuple[str, str | None, str]


@dataclass(frozen=True)
class LabelHomonym:
    """Permit one scheme/language/key for a finite set of named concept owners.

    The supplied label uses the operation's comparison mode. One allowance must
    cover every owner; overlapping allowances never authorize their union.
    """

    scheme: str
    label: Literal
    concepts: tuple[str, ...]

    def __post_init__(self) -> None:
        """Retain immutable declaration shapes before bounded operation checks."""
        if (
            not isinstance(self.scheme, str)
            or not isinstance(self.label, Literal)
            or not isinstance(self.concepts, tuple)
        ):
            raise InputError("A homonym requires a scheme, literal and owner tuple")


def _capture(values: Sequence[T], budget: Budget) -> tuple[T, ...]:
    """Capture indexed finite selections before any caller-controlled iteration."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Label policy selections must be finite sequences")
    length = len(values)
    if length > budget.limits.max_states:
        raise LimitExceeded("Label policy selection exceeds max_states")
    result: list[T] = []
    for index in range(length):
        budget.consume("work")
        budget.consume("states")
        result.append(values[index])
    return tuple(result)


def _text(value: str, budget: Budget) -> str:
    """Bound original and derived lexical keys before retaining their bytes."""
    if not isinstance(value, str):
        raise InputError("Expected text in a label policy")
    if len(value) > budget.limits.max_bytes:
        raise LimitExceeded("Label policy text exceeds max_bytes")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InputError("Label policy text requires valid Unicode") from error
    if len(encoded) > budget.limits.max_bytes:
        raise LimitExceeded("Label policy text exceeds max_bytes")
    budget.consume("bytes", len(encoded))
    return value


def _iri(value: str, budget: Budget) -> str:
    """Validate an exact named IRI without rewriting its spelling."""
    if isinstance(value, Literal | BNode):
        raise InputError("Expected a named IRI string")
    return str(absolute_iri(_text(value, budget)))


def _key(
    label: Literal, comparison: Comparison, budget: Budget
) -> tuple[str | None, str]:
    """Normalize comparison keys only; language tags stay separate namespaces."""
    value = _text(str(label), budget)
    language = label.language.lower() if label.language else None
    if language is not None:
        _text(language, budget)
    if comparison in ("nfc", "nfc-casefold"):
        value = _text(unicodedata.normalize("NFC", value), budget)
    if comparison in ("casefold", "nfc-casefold"):
        value = _text(value.casefold(), budget)
    if comparison == "nfc-casefold":
        value = _text(unicodedata.normalize("NFC", value), budget)
    return language, value


def _lexical(term: Node) -> bool:
    """Select string labels without interpreting arbitrary datatype lexicals."""
    return isinstance(term, Literal) and (
        term.language is not None or term.datatype in (None, XSD.string)
    )


def _evidence(key: Key, owners: dict[Node, list[Edge]], budget: Budget) -> str:
    """Retain exact original RDF terms and their supplying assertion scopes."""
    assertions: list[dict[str, str]] = []
    for edges in owners.values():
        for edge in edges:
            budget.consume("work")
            budget.consume("states")
            assertions.append(
                {
                    "concept": edge.subject.n3(),
                    "role": str(edge.predicate),
                    "label": edge.object.n3(),
                    "scope": edge.scope,
                }
            )
    return _text(
        json.dumps(
            {
                "scheme": key[0],
                "language": key[1],
                "key": key[2],
                "assertions": assertions,
            },
            sort_keys=True,
        ),
        budget,
    )


def check_label_collisions(
    vocabulary: Vocabulary | Graph | VocabularyView,
    *,
    schemes: Sequence[str],
    concepts: Sequence[str] = (),
    languages: Sequence[str | None] = (),
    roles: Sequence[str] = (str(SKOS.prefLabel),),
    comparison: Comparison = "exact",
    allowed_homonyms: Sequence[LabelHomonym] = (),
    scopes: Sequence[str] = ("local", "imports"),
    limits: Limits | None = None,
) -> Report:
    """Check supplied labels in explicitly selected nonempty named schemes.

    Selected roles share a naming namespace within each scheme and exact
    language tag (case insensitive); None selects untagged labels. Empty language
    selection includes all languages. Membership uses supplied inScheme and
    top-concept assertions. Owners need an established finite SKOS Concept role;
    inScheme or a label assertion alone does not establish that role. Missing
    labels are outside this uniqueness policy.

    Exact, Unicode casefold, NFC, and NFC-then-casefold-then-NFC affect keys only.
    These are finite consumer naming policies, not additional SKOS integrity
    conditions, linguistic collation, or a claim that homonyms denote one entity.
    """
    if comparison not in ("exact", "casefold", "nfc", "nfc-casefold"):
        raise InputError("Unknown label comparison mode")
    selected_limits = limits or (
        vocabulary.limits
        if isinstance(vocabulary, Vocabulary | VocabularyView)
        else None
    )
    budget = Budget(selected_limits)
    issues = IssueCollector(budget)
    checked = 0
    complete = True
    exclusions: list[str] = []
    try:
        selected_schemes = {_iri(value, budget) for value in _capture(schemes, budget)}
        if not selected_schemes:
            raise InputError("Label collision policies require selected schemes")
        selected_concepts = {
            _iri(value, budget) for value in _capture(concepts, budget)
        }
        selected_roles = {_iri(value, budget) for value in _capture(roles, budget)}
        if not selected_roles or not selected_roles <= _ROLES:
            raise InputError("Select plain SKOS preferred, alternate or hidden labels")
        selected_languages: set[str | None] = set()
        for language in _capture(languages, budget):
            selected_languages.add(
                language_tag(_text(language, budget)).lower()
                if language is not None
                else None
            )
        allowances: dict[Key, list[frozenset[Node]]] = {}
        for allowance in _capture(allowed_homonyms, budget):
            if not isinstance(allowance, LabelHomonym) or not _lexical(allowance.label):
                raise InputError("Homonyms require lexical label literals")
            scheme = _iri(allowance.scheme, budget)
            permitted_owners = frozenset(
                URIRef(_iri(value, budget))
                for value in _capture(allowance.concepts, budget)
            )
            if scheme not in selected_schemes or len(permitted_owners) < 2:
                raise InputError(
                    "A homonym needs a selected scheme and two distinct owners"
                )
            language, lexical = _key(allowance.label, comparison, budget)
            allowances.setdefault((scheme, language, lexical), []).append(
                permitted_owners
            )
        if isinstance(vocabulary, VocabularyView):
            view = vocabulary
        else:
            source = (
                load_vocabulary(vocabulary, budget=budget)
                if isinstance(vocabulary, Graph)
                else vocabulary
            )
            view = inspect_vocabulary(
                source, scopes=_capture(scopes, budget), budget=budget
            )
        facts = structure(view._read(), budget)
        memberships: dict[Node, tuple[str, ...]] = {}
        seen_schemes: set[str] = set()
        for owner, values in facts.schemes.items():
            budget.consume("work")
            if SKOS.Concept not in facts.roles.get(owner, frozenset()):
                continue
            if selected_concepts and str(owner) not in selected_concepts:
                continue
            budget.consume("work", len(values))
            matching = tuple(
                str(value) for value in values if str(value) in selected_schemes
            )
            if matching:
                budget.consume("states", len(matching) + 1)
                memberships[owner] = matching
                seen_schemes.update(matching)
        for missing in selected_schemes - seen_schemes:
            issues.append(
                Issue(
                    "skos.policy.label_collision_unverified",
                    "Selected scheme has no supplied selected concept membership",
                    missing,
                    kind="unverified",
                )
            )
        for missing in selected_concepts - {str(owner) for owner in memberships}:
            issues.append(
                Issue(
                    "skos.policy.label_collision_unverified",
                    "Selected concept has no supplied membership in selected schemes",
                    missing,
                    kind="unverified",
                )
            )
        buckets: dict[Key, dict[Node, list[Edge]]] = {}
        reported: set[Key] = set()
        for owner in sorted(memberships, key=lambda node: node.n3()):
            assert isinstance(owner, URIRef | BNode)
            statements = view.statements(
                owner, predicates=tuple(selected_roles), budget=budget
            )
            for edge in statements.items:
                budget.consume("work")
                checked += 1
                if (
                    isinstance(edge.object, Literal)
                    and selected_languages
                    and (edge.object.language.lower() if edge.object.language else None)
                    not in selected_languages
                ):
                    continue
                if not _lexical(edge.object):
                    issues.append(
                        Issue(
                            "skos.policy.label_collision_unverified",
                            "Selected label is not a supported lexical literal",
                            str(edge.subject),
                            kind="unverified",
                            actual=edge.object.n3(),
                        )
                    )
                    continue
                assert isinstance(edge.object, Literal)
                language, lexical = _key(edge.object, comparison, budget)
                if selected_languages and language not in selected_languages:
                    continue
                for scheme in memberships[edge.subject]:
                    budget.consume("work")
                    budget.consume("states")
                    key = (scheme, language, lexical)
                    owners = buckets.setdefault(key, {})
                    owners.setdefault(edge.subject, []).append(edge)
                    if len(owners) < 2 or key in reported:
                        continue
                    allowed = False
                    for permitted in allowances.get(key, ()):
                        budget.consume("work", len(owners))
                        if owners.keys() <= permitted:
                            allowed = True
                            break
                    if not allowed:
                        evidence = _evidence(key, owners, budget)
                        issues.append(
                            Issue(
                                "skos.policy.label_collision",
                                "Distinct concepts share a label key prohibited "
                                "by the selected naming policy",
                                scheme,
                                evidence_json=evidence,
                            )
                        )
                        reported.add(key)
            if not statements.report.complete:
                issues.extend(statements.report.issues)
                complete = False
                break
        for key, owners in buckets.items():
            budget.consume("work")
            if len(owners) > 1 and key not in reported:
                exclusions.append(_evidence(key, owners, budget))
        # Successful selection is meaningful even when no supplied labels exist;
        # label presence remains an independent consumer obligation.
        checked += len(memberships)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="skos.label_collisions")
        complete = False
    return Report(
        "skos.label_collisions",
        checked,
        tuple(issues),
        requested=checked if complete else checked + 1,
        complete=complete,
        regime=f"consumer-label-policy:{comparison}",
        families=(
            "skos.label_collisions",
            f"comparison:{comparison}",
            *(
                (f"unicode:{unicodedata.unidata_version}",)
                if comparison != "exact"
                else ()
            ),
        ),
        exclusions=tuple(exclusions),
    )
