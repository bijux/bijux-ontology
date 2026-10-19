"""Maintained scalar URI-template expansion and conservative exact-image recognition."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, cast
from urllib.parse import unquote_to_bytes

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.reports import Issue, Report

from .types import (
    IdentifierBatch,
    IdentifierRecognition,
    IdentifierRecord,
    TemplateKey,
)

if TYPE_CHECKING:
    from uri_template import URITemplate

_RESERVED = frozenset(":/?#[]@!$&'()*+,;=")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CAPTURE = re.compile(r"(?:[A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})*\Z")
_INTEGER = re.compile(r"(?:0|-?[1-9][0-9]*)\Z")


def _text(value: str, budget: Budget) -> str:
    """Bound text before encoding and charge both inspection and retained bytes."""
    if not isinstance(value, str):
        raise InputError("Expected a string")
    if len(value) > budget.limits.max_bytes:
        raise LimitExceeded("Text exceeds max_bytes")
    budget.consume("work", len(value) + 1)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as error:
        raise InputError("Expected valid Unicode text") from error
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Text exceeds max_bytes")
    budget.consume("bytes", size)
    return value


def _processor(template: str) -> URITemplate:
    """Import the optional maintained implementation only at an operation boundary."""
    guidance = (
        "URI templates require bijux-ontology[templates] with uri-template>=1.3,<2"
    )
    try:
        release = version("uri-template")
        major, minor = (int(part) for part in release.split(".")[:2])
        if major != 1 or minor < 3:
            raise ResourceError(f"{guidance} (installed {release})")
        from uri_template import (
            ExpansionInvalidError,
            ExpansionReservedError,
            URITemplate,
            VariableInvalidError,
        )
    except (PackageNotFoundError, ImportError, ValueError) as error:
        if isinstance(error, ResourceError):
            raise
        raise ResourceError(guidance) from error
    try:
        return URITemplate(template)
    except (
        ValueError,
        ExpansionInvalidError,
        ExpansionReservedError,
        VariableInvalidError,
    ) as error:
        raise InputError("Invalid URI template") from error


def _self_alias(keys: tuple[TemplateKey, ...], budget: Budget) -> str:
    """Avoid the maintained bound method's parameter without changing caller keys."""
    budget.consume("work", len(keys))
    declared = {key.name for key in keys}
    alias = "_bijux_self"
    suffix = 0
    while alias in declared:
        budget.consume("work")
        suffix += 1
        alias = f"_bijux_self_{suffix}"
    return _text(alias, budget)


def _engine(
    template: str, keys: tuple[TemplateKey, ...], budget: Budget
) -> URITemplate:
    """Charge metadata and guard brace structure before maintained template parsing."""
    _text(template, budget)
    opened = False
    for character in template:
        if character == "{":
            if opened:
                raise InputError("Nested template expressions are invalid")
            opened = True
        elif character == "}":
            if not opened:
                raise InputError("Unmatched template brace")
            opened = False
    if opened:
        raise InputError("Unmatched template brace")
    for key in keys:
        budget.consume("states")
        _text(key.name, budget)
    engine = _processor(template)
    if set(engine.variable_names) != {key.name for key in keys}:
        raise InputError("Template variables must match exactly the declared keys")
    if any(key.name == "self" for key in keys):
        alias = _self_alias(keys, budget)
        budget.consume("work", 2 * len(template))
        count = template.count("{self}") + template.count("{+self}")
        size = len(template.encode("utf-8")) + count * (len(alias) - len("self"))
        if size > budget.limits.max_bytes:
            raise LimitExceeded("Internal template binding exceeds max_bytes")
        rewritten = template.replace("{self}", "{" + alias + "}").replace(
            "{+self}", "{+" + alias + "}"
        )
        if rewritten != template:
            engine = _processor(_text(rewritten, budget))
    return engine


def _literal(text: str, budget: Budget) -> str:
    """Bound maintained literal encoding before allocating its expanded form."""
    upper = 3 * len(text.encode("utf-8"))
    budget.consume("work", upper + 1)
    if upper > budget.limits.max_bytes:
        raise LimitExceeded("Template literal may exceed max_bytes")
    return _text(_processor(text).expand(), budget)


def _parts(
    template: str, keys: tuple[TemplateKey, ...], budget: Budget
) -> tuple[tuple[str, ...], tuple[tuple[str, bool], ...]] | None:
    """Qualify only exact scalar tokens; the maintained parser validates syntax."""
    allowed = {
        token: (key.name, reserved)
        for key in keys
        for token, reserved in (
            ("{" + key.name + "}", False),
            ("{+" + key.name + "}", True),
        )
    }
    literals: list[str] = []
    slots: list[tuple[str, bool]] = []
    start = 0
    while (opening := template.find("{", start)) >= 0:
        closing = template.index("}", opening)
        budget.consume("work", closing - start + 1)
        token = template[opening : closing + 1]
        if token not in allowed:
            return None
        budget.consume("states")
        literals.append(_literal(template[start:opening], budget))
        slots.append(allowed[token])
        start = closing + 1
    literals.append(_literal(template[start:], budget))
    if len(slots) != len(keys) or len({name for name, _ in slots}) != len(keys):
        return None
    return tuple(literals), tuple(slots)


def _bindings(
    keys: tuple[TemplateKey, ...], values: Mapping[str, str | int], budget: Budget
) -> tuple[tuple[tuple[str, str | int], ...], dict[str, str]]:
    """Capture only schema-selected scalar values; never iterate arbitrary mappings."""
    if not isinstance(values, Mapping) or len(values) != len(keys):
        raise InputError("Bindings must contain exactly the declared keys")
    bindings: list[tuple[str, str | int]] = []
    lexical: dict[str, str] = {}
    for key in keys:
        budget.consume("work", 2)
        if key.name not in values:
            raise InputError("Missing template key", location=key.name)
        try:
            value = values[key.name]
        except KeyError as error:
            raise InputError("Missing template key", location=key.name) from error
        if key.kind == "integer":
            if type(value) is not int:
                raise InputError(
                    "Expected an integer, excluding bool", location=key.name
                )
            # Bound decimal conversion before allocating an arbitrarily large string.
            budget.consume("work", value.bit_length() + 1)
            try:
                text = str(value)
            except ValueError as error:
                raise LimitExceeded(
                    "Integer exceeds decimal conversion bound"
                ) from error
        else:
            if not isinstance(value, str):
                raise InputError("Expected a string", location=key.name)
            text = value
        lexical[key.name] = _text(text, budget)
        bindings.append((key.name, value))
    return tuple(bindings), lexical


def _expand(
    engine: URITemplate,
    keys: tuple[TemplateKey, ...],
    values: Mapping[str, str | int],
    budget: Budget,
    literal_bytes: int,
) -> IdentifierRecord:
    """Reserve an encoding upper bound before maintained scalar expansion."""
    bindings, lexical = _bindings(keys, values, budget)
    upper = literal_bytes + 3 * sum(
        len(text.encode("utf-8")) for text in lexical.values()
    )
    budget.consume("work", upper + 1)
    if upper > budget.limits.max_bytes:
        raise LimitExceeded("Expanded identifier may exceed max_bytes")
    if "self" in lexical:
        lexical[_self_alias(keys, budget)] = lexical.pop("self")
    iri = _text(engine.expand(**lexical), budget)
    return IdentifierRecord(absolute_iri(iri), bindings)


@dataclass(frozen=True, init=False)
class IdentifierTemplate:
    """Finite scalar forward profile and an explicitly narrower inverse profile.

    Forward expansion supports each declared ASCII key exactly once as ``{key}``
    or ``{+key}``. Other maintained-valid templates remain recognizable as an
    unsupported scope, and cannot construct identifiers. Recognition supports
    only ``{key}`` slots separated by literals containing a reserved character.
    It decodes once and requires exact maintained re-expansion. Integer keys use
    canonical decimal spelling; string keys retain their exact lexical value.
    """

    template: str
    keys: tuple[TemplateKey, ...]

    def __init__(
        self,
        template: str,
        keys: Sequence[TemplateKey],
        *,
        limits: Limits | None = None,
    ) -> None:
        budget = Budget(limits)
        if isinstance(cast(object, keys), (str, bytes)) or not isinstance(
            keys, Sequence
        ):
            raise InputError("Expected a finite sequence of typed template keys")
        count = len(keys)
        if count > budget.limits.max_states:
            raise LimitExceeded("Template key count exceeds max_states")
        captured: list[TemplateKey] = []
        for index in range(count):
            budget.consume("work")
            budget.consume("states")
            key = keys[index]
            if not isinstance(key, TemplateKey):
                raise InputError("Expected typed template keys")
            _text(key.name, budget)
            if not _NAME.fullmatch(key.name):
                raise InputError(
                    "Template key names require ASCII letters, digits or underscore"
                )
            captured.append(key)
        owned = tuple(captured)
        if not owned or len({key.name for key in owned}) != len(owned):
            raise InputError("Template keys must be nonempty and unique")
        _engine(template, owned, budget)
        object.__setattr__(self, "template", template)
        object.__setattr__(self, "keys", owned)

    def construct(
        self, values: Mapping[str, str | int], *, limits: Limits | None = None
    ) -> IdentifierRecord:
        """Construct one exact absolute identifier within the finite forward profile."""
        budget = Budget(limits)
        engine = _engine(self.template, self.keys, budget)
        parts = _parts(self.template, self.keys, budget)
        if parts is None:
            raise InputError(
                "Unsupported forward template profile: use one {key} or {+key} per key"
            )
        return _expand(
            engine,
            self.keys,
            values,
            budget,
            sum(len(part.encode("utf-8")) for part in parts[0]),
        )

    def recognize(
        self, iri: str, *, limits: Limits | None = None
    ) -> IdentifierRecognition:
        """Retain original spelling and recognize only the exact expansion image."""
        if not isinstance(iri, str):
            raise InputError("Expected an IRI string")
        budget = Budget(limits)
        issues = IssueCollector(budget)
        record: IdentifierRecord | None = None
        checked = 0
        try:
            _text(iri, budget)
            absolute_iri(iri)
            engine = _engine(self.template, self.keys, budget)
            parts = _parts(self.template, self.keys, budget)
            if (
                parts is None
                or any(reserved for _, reserved in parts[1])
                or any(not _RESERVED.intersection(part) for part in parts[0][1:-1])
            ):
                issues.append(
                    Issue(
                        "vocabulary.identifier_template_unverified",
                        "Inverse requires {key} slots with reserved literal separators",
                        kind="unverified",
                    )
                )
            else:
                record = self._recognize(iri, engine, parts, budget)
                checked = 1
                if record is None:
                    issues.append(
                        Issue(
                            "vocabulary.identifier_template_mismatch",
                            "IRI is outside the template's exact typed expansion image",
                        )
                    )
        except LimitExceeded as error:
            issues.exhausted(str(error))
        except InputError as error:
            try:
                issues.append(
                    Issue(
                        "vocabulary.identifier_input_error",
                        str(error),
                        kind="input_error",
                    )
                )
            except LimitExceeded as exhausted:
                issues.exhausted(str(exhausted))
        report = Report(
            "vocabulary.identifier_recognition", checked, tuple(issues), requested=1
        )
        return IdentifierRecognition(iri, record if report.verified else None, report)

    def _recognize(
        self,
        iri: str,
        engine: URITemplate,
        parts: tuple[tuple[str, ...], tuple[tuple[str, bool], ...]],
        budget: Budget,
    ) -> IdentifierRecord | None:
        """Split disjoint encoded captures, decode once and re-expand exactly."""
        literals, slots = parts
        if not iri.startswith(literals[0]) or not iri.endswith(literals[-1]):
            return None
        end = len(iri) - len(literals[-1]) if literals[-1] else len(iri)
        position = len(literals[0])
        values: dict[str, str | int] = {}
        kinds = {key.name: key.kind for key in self.keys}
        for index, (name, _) in enumerate(slots):
            separator = literals[index + 1]
            budget.consume("work", len(iri) - position + 1)
            stop = iri.find(separator, position, end) if index + 1 < len(slots) else end
            if stop < position:
                return None
            capture = iri[position:stop]
            if not _CAPTURE.fullmatch(capture):
                return None
            try:
                text = unquote_to_bytes(capture).decode("utf-8", errors="strict")
            except UnicodeError:
                return None
            _text(text, budget)
            if kinds[name] == "integer":
                if not _INTEGER.fullmatch(text):
                    return None
                try:
                    values[name] = int(text)
                except ValueError as error:
                    raise LimitExceeded(
                        "Integer exceeds decimal conversion bound"
                    ) from error
            else:
                values[name] = text
            position = stop + len(separator)
        result = _expand(
            engine,
            self.keys,
            values,
            budget,
            sum(len(part.encode("utf-8")) for part in literals),
        )
        return result if result.iri == iri else None

    def construct_many(
        self,
        rows: Sequence[Mapping[str, str | int]],
        *,
        limits: Limits | None = None,
    ) -> IdentifierBatch:
        """Construct atomically with cumulative bounds and IRI collision checks."""
        budget = Budget(limits)
        issues = IssueCollector(budget)
        records: list[IdentifierRecord] = []
        checked = 0
        requested = 1
        try:
            if isinstance(cast(object, rows), (str, bytes)) or not isinstance(
                rows, Sequence
            ):
                raise InputError("Expected a finite sequence of binding mappings")
            requested = len(rows)
            if requested > budget.limits.max_states:
                raise LimitExceeded("Batch record count exceeds max_states")
            engine = _engine(self.template, self.keys, budget)
            parts = _parts(self.template, self.keys, budget)
            if parts is None:
                raise InputError("Unsupported forward template profile")
            literal_bytes = sum(len(part.encode("utf-8")) for part in parts[0])
            identities: dict[str, int] = {}
            for index in range(requested):
                budget.consume("states")
                budget.consume("work")
                record = _expand(engine, self.keys, rows[index], budget, literal_bytes)
                if record.iri in identities:
                    issues.append(
                        Issue(
                            "vocabulary.identifier_collision",
                            "Different batch positions construct the same exact IRI",
                            str(index),
                            actual=record.iri,
                            evidence_json=json.dumps(
                                {"first": identities[record.iri], "second": index}
                            ),
                        )
                    )
                else:
                    identities[record.iri] = index
                records.append(record)
                checked += 1
        except LimitExceeded as error:
            issues.exhausted(str(error))
        except InputError as error:
            try:
                issues.append(
                    Issue(
                        "vocabulary.identifier_input_error",
                        str(error),
                        kind="input_error",
                    )
                )
            except LimitExceeded as exhausted:
                issues.exhausted(str(exhausted))
        report = Report(
            "vocabulary.identifier_batch",
            checked,
            tuple(issues),
            requested=requested,
            allow_empty=requested == 0,
        )
        return IdentifierBatch(
            tuple(records)
            if report.verified or report.outcome == "not_applicable"
            else None,
            report,
        )
