"""Finite XSD 1.1 lexical validation without XML whitespace preprocessing.

Supported additions are durations and their two subtypes, binary forms, the five
partial Gregorian forms, ASCII Name/NCName/NMTOKEN, and anyURI's XML 1.0 character
space. QName, document-dependent types and non-ASCII names remain unsupported.
No decoded value or normalized spelling replaces a supplied RDF term.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, cast
from xml.parsers.expat import ExpatError, ParserCreate
from xml.sax.saxutils import escape

from bijux_ontology.diagnostics.errors import ResourceError

if TYPE_CHECKING:
    from elementpath.datatypes import AbstractDateTime, AnyAtomicType

XSD_LEXICAL_TYPES = frozenset(
    {
        "duration",
        "yearMonthDuration",
        "dayTimeDuration",
        "hexBinary",
        "base64Binary",
        "gYear",
        "gYearMonth",
        "gMonthDay",
        "gMonth",
        "gDay",
        "Name",
        "NCName",
        "NMTOKEN",
        "anyURI",
    }
)
_NAMES = {"Name", "NCName", "NMTOKEN"}
_GREGORIAN = {"gYear", "gYearMonth", "gMonthDay", "gMonth", "gDay"}


def _datatypes() -> dict[str, type[AnyAtomicType]]:
    """Resolve the optional maintained datatype classes at an operation boundary."""
    guidance = (
        "XSD lexical checks require bijux-ontology[xsd] with elementpath>=5.1.4,<6"
    )
    try:
        release = version("elementpath")
        numbers = tuple(int(part) for part in release.split(".")[:3])
        if len(numbers) != 3 or numbers < (5, 1, 4) or numbers[0] != 5:
            raise ResourceError(f"{guidance} (installed {release})")
        from elementpath import datatypes
    except (PackageNotFoundError, ImportError, ValueError) as error:
        if isinstance(error, ResourceError):
            raise
        raise ResourceError(guidance) from error
    return {
        "duration": datatypes.Duration,
        "yearMonthDuration": datatypes.YearMonthDuration,
        "dayTimeDuration": datatypes.DayTimeDuration,
        "hexBinary": datatypes.HexBinary,
        "base64Binary": datatypes.Base64Binary,
        "gYear": datatypes.GregorianYear,
        "gYearMonth": datatypes.GregorianYearMonth,
        "gMonthDay": datatypes.GregorianMonthDay,
        "gMonth": datatypes.GregorianMonth,
        "gDay": datatypes.GregorianDay,
        "Name": datatypes.Name,
        "NCName": datatypes.NCName,
        "NMTOKEN": datatypes.NMToken,
    }


def xsd_lexical(text: str, local: str) -> bool | None:
    """Validate an exact bounded lexical form; unsupported or domain limits are unknown.

    Maintained patterns are matched in full before constructors can strip XML
    whitespace. Duration subtype guards implement XSD 1.1 sections 3.4.26/27's
    lexical restrictions even for zero-valued forbidden fragments. Calendar
    parsing supplies real day/month/timezone constraints; its value is discarded.
    """
    if local not in XSD_LEXICAL_TYPES or len(text) > 4096:
        return None
    if local == "anyURI":
        # XSD 1.1 section 3.3.17 defines XML characters, not IRI syntax validity.
        # Escaped scalar content cannot introduce markup, DTDs or entity loaders.
        try:
            ParserCreate().Parse("<value>" + escape(text) + "</value>", True)
        except (ExpatError, UnicodeError):
            return False
        return True
    if local in _NAMES and not text.isascii():
        return None
    datatype = _datatypes()[local]
    if datatype.pattern.fullmatch(text) is None:
        return False
    if local == "yearMonthDuration" and ("D" in text or "T" in text):
        return False
    if local == "dayTimeDuration" and ("Y" in text or "M" in text.partition("T")[0]):
        return False
    if local in _GREGORIAN:
        try:
            cast("type[AbstractDateTime]", datatype).fromstring(text)
        except (OverflowError, ArithmeticError):
            return None
        except ValueError:
            return False
    return True
