"""Resolve the optional JSON-LD processor only at an operation boundary."""

from importlib.metadata import PackageNotFoundError, version
from types import ModuleType
from typing import cast

from bijux_ontology.errors import ResourceError


def processor() -> ModuleType:
    """Require the supported PyLD runtime without affecting core-only imports."""
    guidance = (
        "JSON-LD requires bijux-ontology[jsonld] with PyLD>=3.3,<4; "
        "use a compatible runtime when the host requires another PyLD version"
    )
    try:
        release = version("PyLD")
        major, minor = (int(part) for part in release.split(".")[:2])
        if major != 3 or minor < 3:
            raise ResourceError(f"{guidance} (installed PyLD {release})")
        from pyld import jsonld  # type: ignore[import-untyped]
    except (PackageNotFoundError, ImportError, ValueError) as error:
        if isinstance(error, ResourceError):
            raise
        raise ResourceError(guidance) from error
    return cast(ModuleType, jsonld)
