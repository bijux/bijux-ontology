"""Opt-in lexical policies, original codepoint spans and exact literal repairs."""

from dataclasses import dataclass
from typing import Literal as Choice

from rdflib import URIRef

from bijux_ontology.absence import Absent
from bijux_ontology.errors import InputError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import language_tag
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.annotations.graphs.types import (
    AnnotationGraph,
    AnnotationPath,
    AnnotationStatement,
)

AnnotationRole = Choice["direct", "axiom", "annotation"]
NormalizationForm = Choice["NFC", "NFD", "NFKC", "NFKD"]
FindingKind = Choice["edge_whitespace", "forbidden_character", "normalization"]


@dataclass(frozen=True)
class UnicodeRequirement:
    """One explicitly named Unicode database version and normalization form."""

    form: NormalizationForm
    version: str

    def __post_init__(self) -> None:
        """Keep compatibility normalization an explicit caller decision."""
        bound_text(self.version, Budget())
        if self.form not in ("NFC", "NFD", "NFKC", "NFKD") or not self.version:
            raise InputError("Unicode requirements need a named form and version")


@dataclass(frozen=True)
class HygieneRule:
    """A selected predicate/role/language policy; every lexical rule is opt-in.

    Edge whitespace uses the running Python Unicode ``str.isspace`` dialect.
    Forbidden characters/categories apply only when explicitly supplied. Allowed
    characters and exact excluded statements are exceptions, not global rewrites.
    Language selection is exact, case-insensitive; Absent selects untagged values.
    """

    predicate: URIRef
    roles: tuple[AnnotationRole, ...] = ("direct", "axiom", "annotation")
    languages: tuple[str | Absent, ...] | None = None
    edge_whitespace: bool = False
    forbidden_characters: tuple[str, ...] = ()
    forbidden_categories: tuple[str, ...] = ()
    allowed_characters: tuple[str, ...] = ()
    excluded_statements: tuple[AnnotationStatement, ...] = ()
    normalization: UnicodeRequirement | None = None

    def __post_init__(self) -> None:
        """Own finite policy inventories before any caller can mutate them."""
        budget = Budget()
        if (
            not isinstance(self.predicate, URIRef)
            or type(self.edge_whitespace) is not bool
        ):
            raise InputError(
                "Hygiene rules require an RDF predicate and boolean switch"
            )
        bound_node(self.predicate, budget)
        roles = capture_items(self.roles, str, budget)
        if not roles or any(
            role not in ("direct", "axiom", "annotation") for role in roles
        ):
            raise InputError("Unknown annotation hygiene role")
        object.__setattr__(self, "roles", roles)
        if self.languages is not None:
            languages = capture_items(self.languages, object, budget)
            for language in languages:
                if not isinstance(language, str | Absent):
                    raise InputError(
                        "Language policies require language tags or ABSENT"
                    )
                if isinstance(language, str):
                    bound_text(language, budget)
                    language_tag(language)
            object.__setattr__(self, "languages", languages)
        for name in (
            "forbidden_characters",
            "allowed_characters",
            "forbidden_categories",
        ):
            values = capture_items(getattr(self, name), str, budget)
            for value in values:
                bound_text(value, budget)
                if name != "forbidden_categories" and len(value) != 1:
                    raise InputError(
                        "Character policies require individual Unicode scalars"
                    )
                if name == "forbidden_categories" and value not in (
                    "Lu",
                    "Ll",
                    "Lt",
                    "Lm",
                    "Lo",
                    "Mn",
                    "Mc",
                    "Me",
                    "Nd",  # codespell:ignore nd
                    "Nl",
                    "No",
                    "Pc",
                    "Pd",
                    "Ps",
                    "Pe",
                    "Pi",
                    "Pf",
                    "Po",
                    "Sm",
                    "Sc",
                    "Sk",
                    "So",
                    "Zs",
                    "Zl",
                    "Zp",
                    "Cc",
                    "Cf",
                    "Cs",
                    "Co",
                    "Cn",
                ):
                    raise InputError("Unknown Unicode general category")
            object.__setattr__(self, name, values)
        object.__setattr__(
            self,
            "excluded_statements",
            capture_items(self.excluded_statements, AnnotationStatement, budget),
        )
        if self.normalization is not None and not isinstance(
            self.normalization, UnicodeRequirement
        ):
            raise InputError("Normalization requires a typed Unicode requirement")


@dataclass(frozen=True)
class HygieneFinding:
    """One half-open span in the original literal, measured in Unicode codepoints."""

    path: AnnotationPath
    rule: int
    kind: FindingKind
    start: int
    end: int


@dataclass(frozen=True)
class HygieneCheck:
    """Requalified annotation witnesses and raw selected lexical policy truth."""

    graph: AnnotationGraph
    rules: tuple[HygieneRule, ...]
    findings: tuple[HygieneFinding, ...]
    unicode_version: str
    report: Report
