"""Explicit cross-format preparation into owned RDF graph and dataset snapshots."""

from bijux_ontology._json import decode_json
from bijux_ontology.errors import LimitExceeded, ResourceError
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.jsonld.resources import DocumentLoader
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_input, prepare_native
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource


def prepare_source(
    source: RdfSource | Payload,
    *,
    format: str | None = None,
    base: str | None = None,
    max_bytes: int | None = None,
    max_triples: int | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    preserve_literals: bool = True,
    require_complete_graphs: bool = False,
    document_loader: DocumentLoader | None = None,
) -> PreparedSource:
    """Prepare supplied RDF without resource fetching or global parser changes.

    Parsed RDFLib graphs preserve their supplied terms, but cannot recover lexical
    forms already normalised before this call. File/text parsing preserves terms
    only when conversion can retain them; unsupported loss raises ``InputError``.
    TriG reports nonempty-only graph fidelity because the maintained parser does
    not expose empty named graphs; require_complete_graphs rejects that scope.
    """
    preparation = prepare_input(
        source,
        format=format,
        base=base,
        max_bytes=max_bytes,
        max_triples=max_triples,
        limits=limits,
        budget=budget,
        preserve_literals=preserve_literals,
        require_complete_graphs=require_complete_graphs,
    )
    if preparation.syntax != "json-ld":
        return prepare_native(preparation)
    document = preparation.document_iri
    effective = preparation.effective_url
    selected = preparation.limits
    operation = preparation.budget
    max_bytes = selected.max_bytes
    max_triples = selected.max_triples
    payload = preparation.read()
    converted = prepare_jsonld(
        decode_json(
            payload,
            source=document or "",
            max_bytes=max_bytes,
            max_depth=selected.max_depth,
        ),
        base=effective,
        document_iri=document,
        document_loader=document_loader,
        budget=operation,
    )
    if converted.source is None or not converted.report.verified:
        message = "; ".join(issue.message for issue in converted.report.issues)
        if converted.report.outcome == "exhausted":
            raise LimitExceeded(message, location=document or "")
        raise ResourceError(
            "JSON-LD source preservation was not verified: " + message,
            location=document or "",
        )
    return PreparedSource(
        converted.source.dataset(),
        is_dataset=converted.source.is_dataset,
        document_iri=document,
        effective_url=effective,
        literal_fidelity=converted.source.literal_fidelity,
        graph_fidelity=converted.source.graph_fidelity,
        max_triples=max_triples,
    )
