"""OLS controller captures preserve exact identity and scoped offline completion."""

import gzip
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlencode, urlsplit

import pytest
from rdflib import URIRef
from rdflib.namespace import OWL, RDFS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.workflows.projects.acquisition.types import AcquisitionPolicy
from bijux_ontology_adapters.ols import (
    OlsPage,
    OlsQuery,
    OlsSnapshot,
    acquire_ols_snapshot,
    lookup_ols,
    prepare_ols_snapshot,
)

BASE = "https://www.ebi.ac.uk/ols4/api/v2"
TERM = URIRef("http://purl.obolibrary.org/obo/GO_0008150")
DEFINITION = URIRef("http://purl.obolibrary.org/obo/IAO_0000115")
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ols"


def _json(value: object) -> bytes:
    """Serialize bounded test fixture objects, outside the product budget."""
    return json.dumps(value).encode()


def _query(
    *,
    ontology: str = "go",
    iri: URIRef | None = TERM,
    hierarchical_ancestor: URIRef | None = None,
    annotation_predicates: tuple[URIRef, ...] = (RDFS.label,),
    include_imported: bool = True,
    page_size: int = 2,
) -> OlsQuery:
    """Use the actual native query represented by captured deployed responses."""
    return OlsQuery(
        ontology,
        ("class",),
        iri=iri,
        page_size=page_size,
        hierarchical_ancestor=hierarchical_ancestor,
        annotation_predicates=annotation_predicates,
        include_imported=include_imported,
    )


def _url(query: OlsQuery, page: int = 0, base: str = BASE) -> str:
    """Describe request identity independently of response field ordering."""
    params = {
        "iri": str(query.iri),
        "type": ",".join(query.roles),
        "size": str(query.page_size),
        "page": str(page),
        "includeObsoleteEntities": "true",
        "lang": query.language,
    }
    if query.iri is None:
        del params["iri"]
    if query.hierarchical_ancestor is not None:
        params["hierarchicalAncestor"] = str(query.hierarchical_ancestor)
    return base + "/ontologies/" + query.ontology + "/entities?" + urlencode(params)


def _page(query: OlsQuery | None = None) -> OlsPage:
    """Unmodified actual responses captured from the documented deployed route."""
    selected = query or _query()
    prefix = "actual-" + ("efo-" if selected.ontology == "efo" else "")
    content = gzip.decompress((FIXTURES / (prefix + "entities.json.gz")).read_bytes())
    metadata = gzip.decompress((FIXTURES / (prefix + "ontology.json.gz")).read_bytes())
    return OlsPage(_url(selected), content, metadata, metadata)


def _snapshot(
    page: OlsPage | None = None, query: OlsQuery | None = None
) -> OlsSnapshot:
    """Prepare ordinary immutable caller bytes with no service connection."""
    selected = query or _query()
    return prepare_ols_snapshot(
        (page or _page(selected),), query=selected, base_url=BASE
    )


def test_actual_captures_keep_ontology_roles_and_qualified_annotations() -> None:
    """The same IRI stays ontology-local and raw qualifiers keep their attachment."""
    query = _query(annotation_predicates=(RDFS.label, DEFINITION))
    result = _snapshot(query=query)
    assert result.report.verified and result.complete and result.vocabulary is not None
    assert result.data_version and "/releases/" in result.data_version
    assert result.loaded and result.pages[0].software_release is None
    entity = result.entities[0]
    assert entity.iri == TERM and entity.ontology == "go" and "class" in entity.roles
    assert entity.defining and not entity.imported
    definition = next(a for a in entity.annotations if a.predicate == DEFINITION)
    assert "GOC:pdt" in definition.qualified_json.decode()
    assert json.loads(definition.qualified_json)["value"] == definition.lexical
    assert result.vocabulary.lookup(str(TERM)).status == "declared"
    assert result.vocabulary.annotations(str(TERM), predicates=(str(RDFS.label),)).items
    assert not result.vocabulary.statements(
        TERM, predicates=(str(RDFS.subClassOf),)
    ).items
    assert entity.hierarchical_ancestors
    efo = _snapshot(query=_query(ontology="efo"))
    assert efo.report.verified and efo.entities[0].iri == TERM
    assert efo.entities[0].ontology == "efo" and not efo.entities[0].defining
    assert not efo.entities[0].imported
    defining_query = replace(_query(ontology="efo"), defining_only=True)
    excluded = _snapshot(query=defining_query)
    assert not excluded.entities and excluded.vocabulary is None
    assert lookup_ols(result, TERM, role="class").status == "present"
    assert lookup_ols(result, TERM, role="individual").status == "unknown"
    assert not lookup_ols(result, URIRef("urn:unknown"), role="class").report.verified
    with pytest.raises(InputError):
        OlsSnapshot()


def test_missing_drifting_unversioned_or_duplicate_pages_cannot_close_scope() -> None:
    """Software release identity never substitutes for actual ontology data identity."""
    original = _page()
    data = cast(dict[str, object], json.loads(original.content))
    data.update(totalElements=3, totalPages=2)
    missing = _snapshot(replace(original, content=_json(data)))
    assert not missing.complete and not missing.report.verified
    assert missing.report.requested == 2 and missing.report.checked == 1
    data.update(totalElements=1, totalPages=1, numElements=0)
    assert (
        _snapshot(replace(original, content=_json(data))).report.outcome
        == "input_error"
    )
    metadata = cast(dict[str, object], json.loads(original.ontology_after))
    metadata["loaded"] = "changed"
    drift = _snapshot(replace(original, ontology_after=_json(metadata)))
    assert not drift.complete and any(
        i.code == "ols.data_changed" for i in drift.report
    )
    del metadata[str(OWL.versionIRI)]
    unversioned = _snapshot(
        replace(
            original,
            ontology_before=_json(metadata),
            ontology_after=_json(metadata),
            software_release="4.28.1",
        )
    )
    assert not unversioned.complete and unversioned.data_version is None
    assert not lookup_ols(
        unversioned, URIRef("urn:missing"), role="class"
    ).report.verified
    query = _query(iri=None, page_size=1)
    pages = []
    for page in (0, 1):
        duplicate = cast(dict[str, object], json.loads(original.content))
        duplicate.update(page=page, totalPages=2, totalElements=2)
        pages.append(replace(original, url=_url(query, page), content=_json(duplicate)))
    repeated = prepare_ols_snapshot(
        pages,
        query=query,
        base_url=BASE,
        limits=Limits(max_total_bytes=150_000_000, max_states=500_000),
    )
    assert not repeated.complete and any(
        i.code == "ols.page_incomplete" for i in repeated.report
    )


def test_original_status_exact_iri_and_empty_scopes_are_not_invented() -> None:
    """An obsolete/imported policy and lowercased lookalike do not create a term."""
    page = _page()
    for field, query in (
        ("imported", _query(include_imported=False)),
        ("isObsolete", _query()),
    ):
        data = json.loads(page.content)
        data["elements"][0][field] = True
        result = _snapshot(replace(page, content=_json(data)), query)
        assert (
            not result.entities
            and result.vocabulary is None
            and not result.report.verified
        )
    data = json.loads(page.content)
    data["elements"][0]["iri"] = str(TERM).lower()
    result = _snapshot(replace(page, content=_json(data)))
    assert not result.entities and result.vocabulary is None
    data.update(elements=[], numElements=0, totalElements=0, totalPages=0)
    empty = _snapshot(replace(page, content=_json(data)))
    assert empty.vocabulary is None and not empty.report.verified
    assert not lookup_ols(empty, TERM, role="class").report.verified
    selected = _query(hierarchical_ancestor=URIRef("urn:not-the-native-ancestor"))
    hierarchy = _snapshot(replace(page, url=_url(selected)), selected)
    assert not hierarchy.report.verified and not hierarchy.entities


@contextmanager
def _server(*, drift: bool = False) -> Iterator[tuple[str, list[str]]]:
    """Run real HTTP requests; only local controller-shaped test data is modified."""
    original = _page()
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Keep observable request evidence in the caller-owned list."""

        def do_GET(self) -> None:
            """Serve ordinary responses; metadata can genuinely change per request."""
            seen.append(self.path)
            metadata = json.loads(original.ontology_before)
            if drift:
                metadata["loaded"] = str(len(seen))
            content = (
                original.content
                if urlsplit(self.path).path.endswith("/entities")
                else _json(metadata)
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(http.server_port), seen
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def test_real_acquisition_and_offline_reuse_preserve_fresh_metadata() -> None:
    """Three actual requests bracket a page; subsequent preparation stays offline."""
    limits = Limits(max_total_bytes=100_000_000, max_states=500_000, max_work=2_000_000)
    with _server() as (origin, seen):
        budget = Budget(limits)
        result = acquire_ols_snapshot(
            origin + "/api/v2",
            query=_query(),
            policy=AcquisitionPolicy((origin,), max_requests=3),
            budget=budget,
        )
        assert result.report.verified and len(seen) == 3
        assert budget.remaining("resources") < budget.limits.max_resources - 2
    offline = prepare_ols_snapshot(
        result.pages, query=_query(), base_url=result.base_url
    )
    assert offline.report.verified and offline.entities == result.entities
    with _server(drift=True) as (origin, seen):
        result = acquire_ols_snapshot(
            origin + "/api/v2",
            query=_query(),
            policy=AcquisitionPolicy((origin,), max_requests=3),
            limits=limits,
        )
        assert len(seen) == 3 and not result.complete and not result.report.verified
        assert any(i.code == "ols.data_changed" for i in result.report)


def test_shared_limits_cancel_and_raw_prefix_survive_exhaustion() -> None:
    """Tiny grants stop before decode; successful preparation owns its raw bytes."""
    page = _page()
    tiny = _snapshot(page)
    assert tiny.report.verified
    for limits in (
        Limits(max_work=1),
        Limits(max_states=1),
        Limits(max_total_bytes=64),
    ):
        result = prepare_ols_snapshot(
            (page,), query=_query(), base_url=BASE, limits=limits
        )
        assert result.report.outcome == "exhausted" and not result.complete
    cancel = threading.Event()
    cancel.set()
    stopped = prepare_ols_snapshot(
        (page,), query=_query(), base_url=BASE, cancel=cancel
    )
    assert stopped.report.outcome == "exhausted"
    # A valid first raw page remains reachable if the next malformed/large input
    # exhausts; no partial result can certify a complete membership catalogue.
    query = _query(iri=None, page_size=1)
    data = json.loads(page.content)
    data.update(totalElements=2, totalPages=2)
    first = replace(page, url=_url(query), content=_json(data))
    second = replace(page, url=_url(query, 1), content=b" " * 5_000_001)
    result = prepare_ols_snapshot(
        (first, second),
        query=query,
        base_url=BASE,
        limits=Limits(max_total_bytes=100_000_000, max_states=500_000),
    )
    assert result.report.outcome == "exhausted" and result.pages[0] == first
    assert result.entities and not result.complete and result.vocabulary is None
