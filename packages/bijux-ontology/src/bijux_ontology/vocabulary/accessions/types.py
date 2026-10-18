"""Explicit accession policies and exact caller-owned source occurrences."""

from dataclasses import KW_ONLY, dataclass
from typing import Literal

from rdflib import BNode
from rdflib import Literal as RDFLiteral

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Location, Report
from bijux_ontology.vocabulary.templates.expansion import IdentifierTemplate

CasePolicy = Literal["preserve", "ascii-upper", "ascii-lower"]
Checksum = Literal["iso7064-mod11-2", "luhn"]


def _text(value: object, budget: Budget) -> str:
    """Bound metadata before capturing plain text or hashing accepted subclasses."""
    if not isinstance(value, str) or isinstance(value, BNode | RDFLiteral):
        raise InputError(
            "Accession metadata requires text, not RDF blank/literal terms"
        )
    bound_text(value, budget)
    return str(value)


@dataclass(frozen=True)
class AccessionRule:
    """Rules for one contributed namespace, with no resolver or existence claim.

    Grammar is RE2 fullmatch over the exact local identifier. ASCII case policies
    require canonical case; a corrected spelling is only a suggestion. Declared
    punctuation is removed solely from checksum input after grammar succeeds.
    Alternate templates use the existing exact inverse with one string local key.
    """

    namespace: str
    pattern: str
    case_policy: CasePolicy = "preserve"
    check_digit: Checksum | None = None
    checksum_separators: str = ""
    alternates: tuple[IdentifierTemplate, ...] = ()

    def __post_init__(self) -> None:
        """Own bounded scalar metadata without importing optional engines."""
        budget = Budget()
        namespace = _text(self.namespace, budget)
        absolute_iri(namespace)
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "pattern", _text(self.pattern, budget))
        if self.case_policy not in ("preserve", "ascii-upper", "ascii-lower"):
            raise InputError("Unknown accession case policy")
        if self.check_digit not in (None, "iso7064-mod11-2", "luhn"):
            raise InputError("Unsupported accession checksum algorithm")
        separators = _text(self.checksum_separators, budget)
        if any(
            not 33 <= ord(char) <= 126 or char.isalnum() for char in separators
        ) or len(set(separators)) != len(separators):
            raise InputError("Checksum separators require distinct ASCII punctuation")
        if separators and self.check_digit is None:
            raise InputError("Checksum separators require a checksum algorithm")
        object.__setattr__(self, "checksum_separators", separators)
        if type(self.alternates) is not tuple:
            raise InputError(
                "Alternate identifiers require an immutable template tuple"
            )
        budget.consume("states", len(self.alternates))
        for alternate in self.alternates:
            if not isinstance(alternate, IdentifierTemplate):
                raise InputError("Alternate identifiers require IdentifierTemplate")
            bound_text(alternate.template, budget)
            if (
                len(alternate.keys) != 1
                or alternate.keys[0].name != "local"
                or alternate.keys[0].kind != "string"
            ):
                raise InputError("Alternate templates require one string local key")


@dataclass(frozen=True)
class AccessionValue:
    """One exact supplied spelling, explicit interpretation and actual location."""

    value: str
    mode: Literal["curie", "iri"]
    location: Location = Location()

    def __post_init__(self) -> None:
        """Reject kind erasure and guessed IRI-versus-CURIE interpretation."""
        _text(self.value, Budget())
        if self.mode not in ("curie", "iri") or not isinstance(self.location, Location):
            raise InputError("Accessions require explicit intent and a typed Location")


@dataclass(frozen=True)
class AccessionObservation:
    """Retain original identity separately from qualified recognition evidence.

    True ambiguity witnesses two distinct actual namespace/local records, even
    beside unavailable alternatives. False means completed nonambiguous matching;
    None leaves uniqueness unresolved. Rule validity remains separate.
    """

    original: AccessionValue
    status: Literal["valid", "invalid", "unknown", "excluded"]
    namespace: str | None = None
    local: str | None = None
    canonical_curie: str | None = None
    suggested_curie: str | None = None
    _: KW_ONLY
    ambiguous: bool | None = None


@dataclass(frozen=True)
class AccessionCheck:
    """Completed bounded occurrences and the required finite rule-check report."""

    observations: tuple[AccessionObservation, ...]
    report: Report
