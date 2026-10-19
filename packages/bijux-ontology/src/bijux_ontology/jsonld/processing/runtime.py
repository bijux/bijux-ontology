"""Resolve the optional JSON-LD processor only at an operation boundary."""

from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from types import ModuleType
from typing import Any, cast

from bijux_ontology.diagnostics.errors import ResourceError


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


def document_processor(
    *, on_property_dropped: Callable[[str | None], None] | None = None
) -> Any:
    """Keep an absent document base absent through maintained context resets.

    PyLD's empty document base otherwise introduces its example base IRI. A
    real initial null base preserves unresolved identifiers while allowing an
    explicit local context base to override it. Every expansion, context reset,
    framing and compaction algorithm remains owned by the maintained processor.
    """
    processor()
    from pyld.jsonld import JsonLdProcessor  # type: ignore[import-untyped]

    class DocumentProcessor(JsonLdProcessor):  # type: ignore[misc]
        """Adapt only the supported native initial-context boundary."""

        def _get_initial_context(self, options: dict[str, Any]) -> Any:
            """Establish the absent-base default without mutating cached contexts."""
            initial = super()._get_initial_context(options)
            if options.get("base", "") == "":
                return self._process_context(initial, {"@base": None}, options)
            return initial

    return DocumentProcessor(on_property_dropped=on_property_dropped)
