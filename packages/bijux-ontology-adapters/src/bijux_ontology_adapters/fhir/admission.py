"""Maintained FHIR R5 model admission and exact original scalar fidelity."""

from importlib import import_module
from typing import Any, Literal

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.execution.limits import Budget

from .json import decode_fhir_json, same_json

NativeKind = Literal[
    "CodeSystem", "ValueSet", "ConceptMap", "Coding", "CodeableConcept"
]


class NativeVersionUnavailable(Exception):
    """The installed maintained models implement a different FHIR release."""


class NativeFidelityUnavailable(Exception):
    """Native production changes an exact supported original value or scalar kind."""


class NativeOutputLimit(LimitExceeded):
    """Native output exceeds the selected resource profile's byte bound."""


def r5_models(*names: NativeKind) -> tuple[Any, ...]:
    """Load only selected actual optional models, without importing them at startup."""
    from fhir.resources import __fhir_version__

    if __fhir_version__ != "5.0.0":
        raise NativeVersionUnavailable("The installed maintained model is not FHIR R5")
    return tuple(
        getattr(import_module("fhir.resources." + name.lower()), name) for name in names
    )


def _qualify(produced: str, original: object, budget: Budget) -> str:
    """Bound and decode the actual native form before exact source comparison."""
    if len(produced) > budget.limits.max_bytes:
        raise NativeOutputLimit("Native FHIR output exceeds max_bytes")
    if not same_json(original, decode_fhir_json(produced, budget), budget):
        raise NativeFidelityUnavailable(
            "Native FHIR production changes original values"
        )
    return produced


def admit_r5_json(
    payload: bytes, original: object, resource: NativeKind, budget: Budget
) -> str:
    """Qualify actual strict native JSON admission; resource policy stays local."""
    model = r5_models(resource)[0]
    native = model.model_validate_json(payload, strict=True)
    return _qualify(native.model_dump_json(), original, budget)


def admit_r5_value(model: Any, original: object, budget: Budget) -> str:
    """Qualify the same actual native fidelity for selected Python/Coding values."""
    native = model.model_validate(original, strict=True)
    return _qualify(native.model_dump_json(), original, budget)
