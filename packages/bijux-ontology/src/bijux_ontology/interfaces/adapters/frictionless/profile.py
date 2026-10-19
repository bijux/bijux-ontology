"""An explicit offline v1 CSV descriptor profile, not a second Table Schema engine."""

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import cast

from bijux_ontology._json import _measure, own_json
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, bound_text


class UnsupportedPackage(ResourceError):
    """Required descriptor semantics are outside the qualified native profile."""


def object_value(value: object) -> dict[str, object]:
    """Reject descriptor/protocol container coercions."""
    if type(value) is not dict:
        raise InputError("Expected an inline package object")
    return cast(dict[str, object], value)


def array(value: object) -> list[object]:
    """Require a finite JSON array without executing caller iterators."""
    if type(value) is not list:
        raise InputError("Expected a package array")
    return value


def text(value: object) -> str:
    """Keep names and resource identities exact."""
    if type(value) is not str or not value:
        raise InputError("Expected nonempty package text")
    return value


def keys(value: dict[str, object], allowed: set[str]) -> None:
    """Keep native-ignored descriptor extensions required unknown."""
    if set(value) - allowed:
        raise UnsupportedPackage(
            "Descriptor contains fields outside the supported v1 CSV profile"
        )


def relative_path(value: object) -> str:
    """Require canonical relative names inside the exclusively owned tree."""
    path = text(value)
    if any(
        part in path for part in ("\\", "\x00", ":", "?", "#", "%")
    ) or path.startswith("/"):
        raise UnsupportedPackage(
            "Resources require unambiguous local package-relative paths"
        )
    parts = path.split("/")
    if (
        any(part in ("", ".", "..") for part in parts)
        or str(PurePosixPath(path)) != path
    ):
        raise UnsupportedPackage(
            "Resource paths cannot escape or alias the owned package tree"
        )
    return path


def _constraints(field: dict[str, object]) -> None:
    """Select native-qualified constraints without executing another checker."""
    kind = field.get("type", "string")
    if (
        kind not in ("string", "integer", "number", "boolean")
        or field.get("format", "default") != "default"
    ):
        raise UnsupportedPackage(
            "Only default string/integer/number/boolean fields are qualified"
        )
    constraints = object_value(field.get("constraints", {}))
    allowed = {"required", "unique", "enum"}
    allowed |= (
        {"minLength", "maxLength"}
        if kind == "string"
        else {"minimum", "maximum"}
        if kind in ("integer", "number")
        else set()
    )
    keys(constraints, allowed)
    for name, value in constraints.items():
        if name in ("required", "unique"):
            if type(value) is not bool:
                raise InputError("Required/unique constraints require booleans")
        elif name == "enum":
            values = array(value)
            expected = str if kind == "string" else bool if kind == "boolean" else int
            if any(type(item) is not expected for item in values):
                raise UnsupportedPackage(
                    "Enum values must use the supported native scalar type"
                )
        elif type(value) is not int or name in ("minLength", "maxLength") and value < 0:
            raise UnsupportedPackage("Supported bounds require integral JSON values")


def scalar_key(value: object) -> str:
    """Qualify scalar keys; native composite partial-null validation is unsupported."""
    if type(value) is list:
        names = array(value)
        if len(names) != 1:
            raise UnsupportedPackage(
                "Composite keys are outside the qualified native key profile"
            )
        value = names[0]
    return text(value)


def capture(
    description: object, resources: Mapping[str, bytes], budget: Budget
) -> tuple[dict[str, object], dict[str, bytes], tuple[dict[str, object], ...]]:
    """Own original JSON and supplied bytes before staging or invoking native code."""
    size = _measure(description, budget)
    budget.consume("bytes", 64 * size + 1024)
    descriptor = object_value(
        own_json(
            description,
            max_bytes=budget.limits.max_bytes,
            max_depth=budget.limits.max_depth,
        )
    )
    keys(descriptor, {"name", "profile", "resources"})
    if descriptor.get("profile", "tabular-data-package") != "tabular-data-package":
        raise UnsupportedPackage(
            "Only the built-in v1 tabular-data-package profile is qualified"
        )
    if "name" in descriptor:
        text(descriptor["name"])
    entries = array(descriptor.get("resources"))
    if not entries:
        raise InputError("A package must contain supplied resources")
    budget.consume("resources", len(entries))
    paths: list[str] = []
    names: dict[str, dict[str, object]] = {}
    owned_entries = []
    for raw in entries:
        item = object_value(raw)
        keys(
            item, {"name", "profile", "path", "format", "encoding", "schema", "dialect"}
        )
        name = text(item.get("name"))
        if name in names:
            raise InputError("Package resource names must be unique")
        names[name] = item
        path = relative_path(item.get("path"))
        for existing in paths:
            budget.consume("work")
            left, right = path.casefold(), existing.casefold()
            if (
                left == right
                or left.startswith(right + "/")
                or right.startswith(left + "/")
            ):
                raise InputError(
                    "Package paths are ambiguous or collide as files/directories"
                )
        paths.append(path)
        if (
            item.get("profile", "tabular-data-resource") != "tabular-data-resource"
            or item.get("format", "csv") != "csv"
            or text(item.get("encoding", "utf-8")).lower() != "utf-8"
        ):
            raise UnsupportedPackage(
                "Only inline UTF-8 CSV table resources are qualified"
            )
        dialect = object_value(item.get("dialect", {}))
        keys(dialect, {"delimiter", "quoteChar", "doubleQuote", "header"})
        if (
            dialect.get("header", True) is not True
            or dialect.get("doubleQuote", True) is not True
        ):
            raise UnsupportedPackage(
                "Only native default header/double-quote behavior is qualified"
            )
        for key in ("delimiter", "quoteChar"):
            if key in dialect and (
                type(dialect[key]) is not str
                or len(cast(str, dialect[key])) != 1
                or dialect[key] in ("\r", "\n", "\x00")
            ):
                raise UnsupportedPackage(
                    "CSV delimiter/quoteChar must be one supported character"
                )
        if dialect.get("delimiter", ",") == dialect.get("quoteChar", '"'):
            raise InputError("CSV delimiter and quoteChar must differ")
        schema = object_value(item.get("schema"))
        keys(schema, {"fields", "missingValues", "primaryKey", "foreignKeys"})
        fields = array(schema.get("fields"))
        if not fields:
            raise InputError("Each table requires explicit native fields")
        field_names: set[str] = set()
        for raw_field in fields:
            field = object_value(raw_field)
            keys(field, {"name", "type", "format", "constraints"})
            field_name = text(field.get("name"))
            if field_name in field_names:
                raise InputError("Native field names must be unique")
            field_names.add(field_name)
            _constraints(field)
        if any(
            type(value) is not str for value in array(schema.get("missingValues", [""]))
        ):
            raise InputError("Missing values require native text markers")
        if "primaryKey" in schema:
            key = scalar_key(schema["primaryKey"])
            primary_field = next(
                (
                    object_value(value)
                    for value in fields
                    if object_value(value)["name"] == key
                ),
                None,
            )
            if primary_field is None:
                raise InputError("Primary key field is not declared")
            if primary_field.get("type", "string") not in ("string", "integer"):
                raise UnsupportedPackage(
                    "Primary keys require qualified string/integer fields"
                )
        owned_entries.append(item)
    for item in owned_entries:
        schema = object_value(item["schema"])
        field_types = {
            text(object_value(field)["name"]): object_value(field).get("type", "string")
            for field in array(schema["fields"])
        }
        for raw_foreign in array(schema.get("foreignKeys", [])):
            foreign = object_value(raw_foreign)
            keys(foreign, {"fields", "reference"})
            source = scalar_key(foreign.get("fields"))
            reference = object_value(foreign.get("reference"))
            keys(reference, {"resource", "fields"})
            target_name = text(reference.get("resource"))
            if target_name not in names:
                raise UnsupportedPackage("Foreign key target resource was not supplied")
            target_schema = object_value(names[target_name]["schema"])
            target = scalar_key(reference.get("fields"))
            if (
                "primaryKey" not in target_schema
                or scalar_key(target_schema["primaryKey"]) != target
            ):
                raise UnsupportedPackage(
                    "Foreign keys require a declared scalar target primary key"
                )
            target_types = {
                text(object_value(field)["name"]): object_value(field).get(
                    "type", "string"
                )
                for field in array(target_schema["fields"])
            }
            if (
                source not in field_types
                or target not in target_types
                or field_types[source] not in ("string", "integer")
                or field_types[source] != target_types[target]
            ):
                raise UnsupportedPackage(
                    "Foreign key endpoints require aligned string/integer types"
                )
    if not isinstance(resources, Mapping):
        raise InputError("Supplied package resources require a path-to-bytes mapping")
    budget.consume("states", len(resources))
    supplied: dict[str, bytes] = {}
    for raw_path, content in resources.items():
        path = relative_path(raw_path)
        bound_text(path, budget)
        if type(content) is not bytes:
            raise InputError("Supplied package resources require immutable bytes")
        if len(content) > budget.limits.max_bytes:
            raise LimitExceeded("Package resource exceeds max_bytes")
        budget.consume("bytes", 2 * len(content))
        budget.consume("work", len(content) + 1)
        supplied[path] = content
    if set(paths) != set(supplied):
        raise UnsupportedPackage(
            "Supply exactly the descriptor's package-relative resources"
        )
    return descriptor, supplied, tuple(owned_entries)
