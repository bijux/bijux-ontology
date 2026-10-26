"""Narrow qualified maintained-library releases and optional exact caller pins."""

import re

from bijux_ontology.diagnostics.errors import InputError, ResourceError

_RANGES = {
    "scipy": ((1, 17, 0), (1, 17, 1)),
    "pyarrow": ((25, 0, 1), (26, 0, 0)),
    "bmt": ((1, 5, 0), (1, 5, 0)),
    "linkml-runtime": ((1, 12, 0), (1, 12, 1)),
    "pyrudof": ((0, 3, 20), (0, 3, 21)),
}


def exact_requirement(value: str | None) -> None:
    """Accept a release identity, never an unbounded constraint or local version."""
    if value is not None and (
        type(value) is not str
        or len(value) > 32
        or re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)
        is None
    ):
        raise InputError("An exact native requirement must be a three-part release")


def qualify_release(package: str, actual: str, required: str | None = None) -> str:
    """Refuse unqualified APIs and a caller's exact mismatch before interpretation."""
    exact_requirement(required)
    if type(actual) is not str:
        raise ResourceError(f"Unsupported {package} native release identity")
    try:
        exact_requirement(actual)
    except InputError as error:
        raise ResourceError(f"Unsupported {package} native release {actual}") from error
    release = tuple(int(part) for part in actual.split("."))
    minimum, maximum = _RANGES[package]
    if not minimum <= release <= maximum:
        raise ResourceError(f"Unsupported {package} native release {actual}")
    if required is not None and required != actual:
        raise ResourceError(
            f"Native {package} {actual} differs from exact requirement {required}"
        )
    return actual
