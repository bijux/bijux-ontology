"""Strict offline OASIS XML Catalog URI entries and explicit version choices."""

import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn
from urllib.parse import unquote, urljoin, urlsplit
from xml.parsers import expat

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.sources.loading import prepare_input
from bijux_ontology.rdf.sources.types import RdfSource

_NAMESPACE = "urn:oasis:names:tc:entity:xmlns:xml:catalog"
_BASE = "http://www.w3.org/XML/1998/namespace}base"


@dataclass(frozen=True)
class OntologyVersion:
    """Explicitly bind an ontology import IRI to one catalogued version IRI.

    The selected version's IRI remains its RDF base. An existing ontology entry
    must point to the same physical resource; divergent entries are errors.
    """

    ontology_iri: str
    version_iri: str

    def __post_init__(self) -> None:
        absolute_iri(self.ontology_iri, location="ontology_iri")
        absolute_iri(self.version_iri, location="version_iri")
        if self.ontology_iri == self.version_iri:
            raise InputError("Ontology and selected version IRIs must differ")


def _regular_file(path: Path) -> None:
    """Reject missing and special resources before opening bounded local input."""
    try:
        mode = path.stat().st_mode
    except OSError as error:
        raise ResourceError("Missing catalog resource", location=str(path)) from error
    if not stat.S_ISREG(mode):
        raise ResourceError(
            "Catalog resources must be regular files", location=str(path)
        )


def _local_path(reference: str) -> Path:
    """Decode a local file URI without dereferencing any other URI scheme."""
    try:
        parsed = urlsplit(reference)
    except ValueError as error:
        raise InputError("Invalid catalog target URI", location=reference) from error
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise InputError("Catalog targets must be local file URIs", location=reference)
    try:
        return Path(unquote(parsed.path, encoding="utf-8", errors="strict")).resolve()
    except (UnicodeError, ValueError, OSError) as error:
        raise InputError("Invalid local catalog target", location=reference) from error


def _entries(payload: bytes, base: str, budget: Budget) -> dict[str, Path]:
    """Parse only catalog/group/uri, rejecting unsupported resolution behavior."""
    entries: dict[str, Path] = {}
    stack: list[tuple[str, str]] = []
    parser = expat.ParserCreate(namespace_separator="}")

    def forbidden(*args: object) -> NoReturn:
        """Reject DTD and entity constructs before any external input is read."""
        raise InputError("Catalog DTDs and entity declarations are unsupported")

    def start(tag: str, attributes: dict[str, str]) -> None:
        """Validate a scoped entry before retaining its exact logical binding."""
        budget.consume("work")
        budget.consume("states")
        if len(stack) >= budget.limits.max_depth:
            raise LimitExceeded("Catalog exceeds max_depth")
        prefix = _NAMESPACE + "}"
        name = tag.removeprefix(prefix)
        if not tag.startswith(prefix) or name not in {"catalog", "group", "uri"}:
            raise InputError("Unsupported catalog directive", location=tag)
        if (not stack and name != "catalog") or (
            stack and (name == "catalog" or stack[-1][0] == "uri")
        ):
            raise InputError("Invalid catalog element nesting", location=name)
        allowed = {"id", _BASE}
        if name == "uri":
            allowed |= {"name", "uri"}
        else:
            allowed.add("prefer")
        if attributes.keys() - allowed:
            raise InputError("Unsupported catalog attribute", location=name)
        if "prefer" in attributes and attributes["prefer"] not in {"public", "system"}:
            raise InputError("Invalid catalog prefer attribute", location=name)
        parent = stack[-1][1] if stack else base
        current = urljoin(parent, attributes.get(_BASE, ""))
        absolute_iri(current, location="xml:base")
        stack.append((name, current))
        if name == "uri":
            if not attributes.get("uri") or "name" not in attributes:
                raise InputError("Catalog uri requires name and uri attributes")
            logical = absolute_iri(attributes["name"], location="name")
            if logical in entries:
                raise InputError("Duplicate catalog URI name", location=logical)
            entries[logical] = _local_path(urljoin(current, attributes["uri"]))

    def end(tag: str) -> None:
        """Release one inherited base scope after the parser validates matching."""
        budget.consume("work")
        stack.pop()

    def text(value: str) -> None:
        """Permit formatting whitespace, never ignored semantic text content."""
        budget.consume("work")
        if value.strip():
            raise InputError("Catalog elements cannot contain text")

    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    parser.SkippedEntityHandler = forbidden
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    try:
        parser.Parse(payload, True)
    except expat.ExpatError as error:
        raise InputError("Malformed XML catalog", location=str(error.lineno)) from error
    return entries


def load_catalog(
    path: Path,
    *,
    versions: Sequence[OntologyVersion] = (),
    formats: Mapping[str, str] | None = None,
    limits: Limits | None = None,
) -> Mapping[str, RdfSource]:
    """Own exact offline catalog bindings usable as ``load_vocabulary(imports=)``.

    Supported OASIS namespace elements are catalog, nested group, and exact uri
    entries with absolute name IRIs and local file targets. xml:base is inherited
    and resolves relative targets, never logical names. id and prefer are allowed;
    prefer cannot affect URI matching. Rewrites, delegates, nextCatalog, public,
    system, DTDs and entities are rejected, with no fallback resolution. Matching
    preserves exact absolute IRI spelling without URI equivalence normalization.

    Each bounded file is snapshotted as immutable bytes; syntax comes from its
    extension through the ordinary RDF loader, or an explicit formats mapping
    keyed by catalog name (for ambiguous .owl or extensionless targets). Logical
    names supply RDF bases independent of physical paths. Versions add explicit
    ontology aliases;
    catalog entries alone never assert ontology/version relationships. Returned
    bindings acquire import/lookup roles only when passed to the normal loader.
    """
    if not isinstance(path, Path):
        raise InputError("Catalog input must be an explicit Path")
    budget = Budget(limits)
    budget.consume("resources")
    _regular_file(path)
    try:
        with path.open("rb") as handle:
            payload = handle.read(budget.limits.max_bytes + 1)
    except OSError as error:
        raise ResourceError("Cannot read XML catalog", location=str(path)) from error
    if len(payload) > budget.limits.max_bytes:
        raise LimitExceeded("Catalog exceeds max_bytes")
    budget.consume("bytes", len(payload))
    entries = _entries(payload, path.resolve().as_uri(), budget)
    syntaxes: dict[str, str] = {}
    if formats is not None:
        for logical, syntax in formats.items():
            budget.consume("work")
            budget.consume("states")
            if logical not in entries:
                raise InputError("Format key has no catalog entry", location=logical)
            if not isinstance(syntax, str) or not syntax.strip():
                raise InputError("Catalog formats must be nonempty syntax strings")
            syntaxes[logical] = syntax
    choices: dict[str, str] = {}
    selected_versions: set[str] = set()
    for binding in versions:
        budget.consume("work")
        budget.consume("states")
        if not isinstance(binding, OntologyVersion):
            raise InputError("Version bindings must be OntologyVersion values")
        ontology, version = binding.ontology_iri, binding.version_iri
        if ontology in choices or version in selected_versions:
            raise InputError("Conflicting or duplicate ontology/version binding")
        if version not in entries:
            raise ResourceError(
                "Selected version has no catalog entry", location=version
            )
        if ontology in entries and entries[ontology] != entries[version]:
            raise InputError(
                "Ontology entry conflicts with selected version", location=ontology
            )
        choices[ontology] = version
        selected_versions.add(version)
    if choices.keys() & selected_versions:
        raise InputError("Ontology/version bindings cannot form alias chains")
    snapshots: dict[tuple[Path, str | None], tuple[bytes, str | None]] = {}
    sources: dict[str, RdfSource] = {}
    for logical, target in sorted(entries.items()):
        budget.consume("work")
        key = target, syntaxes.get(logical)
        if key not in snapshots:
            _regular_file(target)
            preparation = prepare_input(RdfSource(target, format=key[1]), budget=budget)
            snapshots[key] = preparation.read(), preparation.syntax
        content, snapshot_syntax = snapshots[key]
        sources[logical] = RdfSource(
            content, format=snapshot_syntax, document_iri=logical
        )
    for ontology, version in choices.items():
        if ontology in sources and sources[ontology].format != sources[version].format:
            raise InputError(
                "Ontology entry syntax conflicts with selected version",
                location=ontology,
            )
        sources[ontology] = sources[version]
    return MappingProxyType(sources)
