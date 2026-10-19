"""Finite datatype relations and lexical checks on actual RDF literal samples."""

import re
from calendar import monthrange

from rdflib import Literal
from rdflib.namespace import RDF, XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import bound_text

from ..limits import Budget, IssueCollector
from .datatype_evaluators.registry import DatatypeRegistry
from .datatype_evaluators.types import DatatypeRelation as DatatypeRelation
from .datatype_evaluators.types import Regime as Regime
from .identifiers import absolute_iri
from .terms import bound_node
from .xsd_lexicals import XSD_LEXICAL_TYPES, xsd_lexical

_PARENT = {
    "integer": "decimal",
    "long": "integer",
    "int": "long",
    "short": "int",
    "byte": "short",
    "nonNegativeInteger": "integer",
    "positiveInteger": "nonNegativeInteger",
    "unsignedLong": "nonNegativeInteger",
    "unsignedInt": "unsignedLong",
    "unsignedShort": "unsignedInt",
    "unsignedByte": "unsignedShort",
    "nonPositiveInteger": "integer",
    "negativeInteger": "nonPositiveInteger",
    "normalizedString": "string",
    "token": "normalizedString",
    "language": "token",
    "Name": "token",
    "NCName": "Name",
    "NMTOKEN": "token",
    "dateTimeStamp": "dateTime",
}
_INTEGER_BOUNDS: dict[str, tuple[int | None, int | None]] = {
    "integer": (None, None),
    "long": (-(2**63), 2**63 - 1),
    "int": (-(2**31), 2**31 - 1),
    "short": (-(2**15), 2**15 - 1),
    "byte": (-128, 127),
    "nonNegativeInteger": (0, None),
    "positiveInteger": (1, None),
    "nonPositiveInteger": (None, 0),
    "negativeInteger": (None, -1),
    "unsignedLong": (0, 2**64 - 1),
    "unsignedInt": (0, 2**32 - 1),
    "unsignedShort": (0, 65535),
    "unsignedByte": (0, 255),
}
_ROOTS = {"string", "boolean", "decimal", "float", "double", "date", "dateTime", "time"}
_NUMERIC = {"decimal", "float", "double"}
_DATE = re.compile(r"(?P<year>-?[0-9]{4,})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})")
_TIME = re.compile(
    r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})(?P<fraction>\.[0-9]+)?"
)
_ZONE = re.compile(r"(?:Z|[+-](?:0[0-9]|1[0-3]):[0-5][0-9]|[+-]14:00)?")


def integer_interval(datatype: str) -> tuple[int | None, int | None] | None:
    """Read one exact builtin integer value interval, retaining unknown profiles.

    None means no supported integer datatype; a pair of None endpoints instead
    denotes the unbounded xsd:integer interval. The immutable bounds are shared
    with datatype relations and original lexical validation.
    """
    if not datatype.startswith(str(XSD)):
        return None
    return _INTEGER_BOUNDS.get(datatype.removeprefix(str(XSD)))


def _chain(iri: str) -> tuple[str, ...]:
    if not iri.startswith(str(XSD)):
        return (iri,)
    chain = [iri.removeprefix(str(XSD))]
    while chain[-1] in _PARENT:
        chain.append(_PARENT[chain[-1]])
    return tuple(chain)


def datatype_relation(
    actual: str,
    required: str,
    *,
    regime: Regime = "rdf",
    datatype_registry: DatatypeRegistry | None = None,
    budget: Budget | None = None,
) -> DatatypeRelation:
    """Compare supported value spaces, never equating unknown custom datatypes.

    Integer intervals and named XSD derivations have exact subset rules. Floating
    categories are deliberately unknown across RDF numeric roots; OWL 2's
    datatype map instead treats decimal, float and double as disjoint.
    """
    absolute_iri(actual)
    absolute_iri(required)
    if regime not in ("application", "rdf", "owl-dl"):
        raise InputError("Unknown semantic regime")
    if datatype_registry is not None and not isinstance(
        datatype_registry, DatatypeRegistry
    ):
        raise InputError("Expected an explicit DatatypeRegistry")
    left, right = _chain(actual), _chain(required)
    if (
        actual == required
        and actual.startswith(str(XSD))
        and left[0] in XSD_LEXICAL_TYPES
    ):
        return "equal"
    supported = _ROOTS | {str(RDF.langString)}
    if left[-1] not in supported or right[-1] not in supported:
        return (
            datatype_registry.relation(actual, required, regime=regime, budget=budget)
            if datatype_registry is not None
            else "unknown"
        )
    if actual == required:
        return "equal"
    if right[0] in left[1:]:
        return "subset"
    if left[0] in _INTEGER_BOUNDS and right[0] in _INTEGER_BOUNDS:
        lo, hi = _INTEGER_BOUNDS[left[0]]
        rlo, rhi = _INTEGER_BOUNDS[right[0]]
        if (lo is not None and rhi is not None and lo > rhi) or (
            hi is not None and rlo is not None and hi < rlo
        ):
            return "disjoint"
        if (rlo is None or lo is not None and lo >= rlo) and (
            rhi is None or hi is not None and hi <= rhi
        ):
            return "subset"
        return "overlap"
    if left[-1] == right[-1]:
        return "overlap"
    if left[-1] in _NUMERIC and right[-1] in _NUMERIC:
        return "disjoint" if regime == "owl-dl" else "unknown"
    return "disjoint"


def _calendar_lexical(text: str, datatype: str) -> bool:
    body = text
    zone = ""
    if text.endswith("Z"):
        body, zone = text[:-1], "Z"
    elif len(text) >= 6 and text[-6] in "+-" and text[-3] == ":":
        body, zone = text[:-6], text[-6:]
    if not _ZONE.fullmatch(zone) or datatype == "dateTimeStamp" and not zone:
        return False
    date_text, time_text = "", ""
    if datatype == "date":
        date_text = body
    elif datatype == "time":
        time_text = body
    elif body.count("T") == 1:
        date_text, time_text = body.split("T")
    else:
        return False
    if date_text:
        match = _DATE.fullmatch(date_text)
        if match is None:
            return False
        if len(match["year"].lstrip("-")) > 4 and match["year"].lstrip("-").startswith(
            "0"
        ):
            return False
        year, month, day = (int(match[key]) for key in ("year", "month", "day"))
        if not 1 <= month <= 12 or not 1 <= day <= monthrange(year, month)[1]:
            return False
    if time_text:
        match = _TIME.fullmatch(time_text)
        if match is None:
            return False
        hour, minute, second = (int(match[key]) for key in ("hour", "minute", "second"))
        if minute > 59 or second > 59 or hour > 24:
            return False
        if hour == 24 and (
            minute or second or any(c != "0" for c in (match["fraction"] or "")[1:])
        ):
            return False
    return bool(date_text or time_text)


def _lexical(text: str, datatype: str) -> bool | None:
    local = datatype.removeprefix(str(XSD))
    if local in _INTEGER_BOUNDS:
        if not re.fullmatch(r"[+-]?[0-9]+", text):
            return False
        value = int(text)
        low, high = _INTEGER_BOUNDS[local]
        return (low is None or value >= low) and (high is None or value <= high)
    if local == "decimal":
        return bool(re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", text))
    if local == "boolean":
        return text in ("true", "false", "0", "1")
    if local in ("float", "double"):
        return bool(
            re.fullmatch(
                r"(?:[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[+-]?INF|NaN)",
                text,
            )
        )
    if local in ("date", "dateTime", "dateTimeStamp", "time"):
        return _calendar_lexical(text, local)
    if local == "string":
        return True
    if local == "normalizedString":
        return not any(c in text for c in "\r\n\t")
    if local == "token":
        return (
            not any(c in text for c in "\r\n\t")
            and "  " not in text
            and not text.startswith(" ")
            and not text.endswith(" ")
        )
    if local == "language":
        # XSD's lexical grammar is broader than well-formed RDF language tags.
        return bool(re.fullmatch(r"[a-zA-Z]{1,8}(?:-[a-zA-Z0-9]{1,8})*", text))
    return xsd_lexical(text, local)


def check_literal(
    value: Literal,
    *,
    location: str = "",
    datatype_registry: DatatypeRegistry | None = None,
    budget: Budget | None = None,
) -> Report:
    """Validate one supplied lexical form, without generalising to other values.

    Unsupported datatypes stay unverified. Inputs are not normalised, rewritten
    or converted to Python values, preserving lexical and language identity.
    """
    if not isinstance(value, Literal):
        raise InputError("Expected an RDF literal", location=location)
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if datatype_registry is not None and not isinstance(
        datatype_registry, DatatypeRegistry
    ):
        raise InputError("Expected an explicit DatatypeRegistry")
    if datatype_registry is not None:
        datatype = str(
            value.datatype or (RDF.langString if value.language else XSD.string)
        )
        if datatype_registry.lookup(datatype, budget=budget) is not None:
            return datatype_registry.check(value, location=location, budget=budget)
    operation = budget if budget is not None else Budget()
    issues = IssueCollector(operation)
    checked = 0
    complete = True
    anchor = ""
    try:
        bound_text(location, operation)
        anchor = location
        try:
            bound_node(value, operation)
        except LimitExceeded:
            raise
        except InputError as error:
            raise InputError(
                str(error), location=anchor, failure=error.failure
            ) from error
        operation.consume("states")
        operation.reserve_storage(128 + 4 * (len(value) + len(value.datatype or "")))
        datatype = str(
            value.datatype or (RDF.langString if value.language else XSD.string)
        )
        valid: bool | None
        unavailable: str | None = None
        checked = 1
        if value.language:
            valid = datatype == str(RDF.langString)
        elif datatype == str(RDF.langString):
            valid = False
        elif len(value) > 4096:
            issues.append(
                Issue(
                    "literal.lexical_unverified",
                    "Literal exceeds supported lexical comparison size",
                    anchor,
                    kind="unverified",
                )
            )
            return Report("literal", checked, tuple(issues))
        else:
            try:
                valid = _lexical(str(value), datatype)
            except ResourceError as error:
                valid = None
                unavailable = str(error)
        if valid is not True:
            issues.append(
                Issue(
                    "literal.lexical_invalid"
                    if valid is False
                    else "literal.datatype_unverified",
                    "Lexical form is outside the datatype value space"
                    if valid is False
                    else unavailable or "Datatype lexical validation is unsupported",
                    anchor,
                    kind="finding" if valid is False else "unverified",
                    actual=datatype,
                )
            )
    except LimitExceeded as stopped:
        complete = False
        issues.exhausted(str(stopped), location=anchor)
    return Report("literal", checked, tuple(issues), requested=1, complete=complete)
