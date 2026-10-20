"""Original native formats, maintained preservation and honest interchange loss."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest
from bijux_ontology_reasoning.backend import (
    OwlProfile,
    OwlProfileBackend,
    prepare_profile_backend,
)
from bijux_ontology_reasoning.documents import (
    OwlSyntax,
    convert_ontology,
    prepare_owl_document,
)

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.limits import Budget, Limits

_FUNCTIONAL = (
    "Ontology(<urn:native> <urn:version> "
    "Declaration(Class(<urn:C>)) Declaration(Class(<urn:D>)) "
    "SubClassOf(<urn:C> <urn:D>))"
)
_XML = (
    '<Ontology xmlns="http://www.w3.org/2002/07/owl#" '
    'ontologyIRI="urn:native" versionIRI="urn:version">'
    '<Declaration><Class IRI="urn:C"/></Declaration>'
    '<Declaration><Class IRI="urn:D"/></Declaration>'
    '<SubClassOf><Class IRI="urn:C"/><Class IRI="urn:D"/></SubClassOf>'
    "</Ontology>"
)
_MANCHESTER = (
    "Ontology: <urn:native> <urn:version>\n"
    "Class: <urn:D>\nClass: <urn:C>\n SubClassOf: <urn:D>\n"
)
_OBO = (
    "format-version: 1.2\nontology: sample\n\n"
    "[Term]\nid: EX:1\nname: first\n\n"
    "[Term]\nid: EX:2\nname: second\nis_a: EX:1\n"
)
_PROFILES: tuple[OwlProfile, ...] = ("owl2-dl", "owl2-el", "owl2-ql", "owl2-rl")


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Use an explicitly pinned build, without discovering or downloading a JVM."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and bundle and pin):
        pytest.skip("Explicit pinned native OWL bundle and Java are not configured")
    return prepare_profile_backend(
        Path(java), Path(bundle), manifest_sha256=pin, artifacts=tmp_path / "jobs"
    )


def test_original_capture_and_core_import(tmp_path: Path) -> None:
    """Original bytes and explicit identities are owned without starting Java."""
    path = tmp_path / "ontology.owl"
    raw = _XML.encode("utf-16")
    path.write_bytes(raw)
    document = prepare_owl_document(
        path,
        format="owlxml",
        document_iri="urn:document",
        effective_url="https://example.org/effective",
    )
    path.write_text("changed")
    assert document.content == raw and document.effective_url.endswith("effective")
    with pytest.raises(LimitExceeded):
        prepare_owl_document(
            "x" * 1000,
            format="functional",
            document_iri="urn:document",
            limits=Limits(max_bytes=100),
        )
    with pytest.raises(InputError):
        prepare_owl_document(
            "", format="functional", document_iri="urn:document", effective_url=""
        )
    with pytest.raises(InputError):
        prepare_owl_document(b"\xff", format="obo", document_iri="urn:document")
    with pytest.raises(ResourceError):
        prepare_owl_document(
            tmp_path / "missing", format="functional", document_iri="urn:document"
        )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", "import bijux_ontology_reasoning.documents"],
        env={**os.environ, "PATH": ""},
        check=False,
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0


def test_equivalent_native_syntax_and_actual_profiles(
    backend: OwlProfileBackend,
) -> None:
    """Three real parsers/writers preserve selected identity, axioms and profiles."""
    for syntax, raw in (
        ("functional", _FUNCTIONAL),
        ("manchester", _MANCHESTER),
        ("owlxml", _XML),
    ):
        document = prepare_owl_document(
            raw,
            format=cast(OwlSyntax, syntax),
            document_iri="https://example.org/source",
        )
        converted = convert_ontology(
            backend, document, format="functional", profiles=_PROFILES
        )
        assert converted.report.verified and converted.lossless is True
        assert converted.content and document.content == raw.encode()
        assert converted.documents[0].ontology_iri == "urn:native"
        assert converted.documents[0].version_iri == "urn:version"
        assert converted.documents[0].axiom_count == 3
        assert tuple(x.in_profile for x in converted.before_profiles) == (True,) * 4
        assert converted.before_profiles == converted.after_profiles
    unnamed = prepare_owl_document(
        "Ontology(Declaration(Class(<urn:C>)))",
        format="functional",
        document_iri="urn:unnamed:document",
    )
    assert convert_ontology(backend, unnamed, format="owlxml").report.verified
    rich = prepare_owl_document(
        "Ontology(<urn:rich> Declaration(Class(<urn:C>)) "
        "Declaration(Class(<urn:D>)) Declaration(ObjectProperty(<urn:p>)) "
        "EquivalentClasses(<urn:C> ObjectUnionOf(<urn:D> "
        "ObjectSomeValuesFrom(<urn:p> <urn:C>))))",
        format="functional",
        document_iri="urn:rich:document",
    )
    result = convert_ontology(backend, rich, format="owlxml", profiles=_PROFILES)
    assert result.report.verified and result.before_profiles == result.after_profiles
    assert not result.before_profiles[1].in_profile


def test_offline_import_alias_and_effective_native_base(
    backend: OwlProfileBackend,
) -> None:
    """Offline closure retains aliases, effective bases and an actual cycle."""
    contacts: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            contacts.append(self.path)
            self.send_error(500)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        requested = f"http://127.0.0.1:{server.server_port}/import"
        root = prepare_owl_document(
            '<Ontology xmlns="http://www.w3.org/2002/07/owl#" ontologyIRI="urn:root">'
            f'<Import>{requested}</Import><Declaration><Class IRI="#Root"/>'
            "</Declaration></Ontology>",
            format="owlxml",
            document_iri="urn:root:document",
            effective_url="https://effective.example/root",
        )
        imported = prepare_owl_document(
            '<Ontology xmlns="http://www.w3.org/2002/07/owl#" ontologyIRI="urn:import">'
            '<Import>urn:root:document</Import><Declaration><Class IRI="#Child"/>'
            "</Declaration></Ontology>",
            format="owlxml",
            document_iri="urn:import:document",
            effective_url="https://effective.example/child",
        )
        unused = prepare_owl_document(
            "not an ontology", format="functional", document_iri="urn:unused"
        )
        result = convert_ontology(
            backend,
            root,
            format="functional",
            imports={requested: imported, "urn:unused": unused},
            profiles=_PROFILES,
        )
        assert result.report.verified and len(result.documents) == 2
        assert (
            result.content and b"https://effective.example/root#Root" in result.content
        )
        assert {x.source.effective_url for x in result.documents} == {
            "https://effective.example/root",
            "https://effective.example/child",
        }
        child = prepare_owl_document(
            "Ontology(<urn:child> Declaration(Class(<urn:Child>)))",
            format="functional",
            document_iri="urn:child:document",
        )
        for syntax, raw in (
            (
                "functional",
                f"Ontology(<urn:mixed> Import(<{requested}>) "
                "Declaration(Class(<urn:Root>)))",
            ),
            (
                "manchester",
                f"Ontology: <urn:mixed>\nImport: <{requested}>\nClass: <urn:Root>\n",
            ),
            (
                "obo",
                f"format-version: 1.2\nontology: rootsample\nimport: {requested}\n"
                "\n[Term]\nid: EX:1\nname: Root\n",
            ),
        ):
            mixed = convert_ontology(
                backend,
                prepare_owl_document(
                    raw,
                    format=cast(OwlSyntax, syntax),
                    document_iri="urn:mixed:document",
                ),
                format="functional",
                imports={requested: child},
            )
            assert mixed.report.verified and len(mixed.documents) == 2
            assert any(requested in item.imports for item in mixed.documents)
        # Unmapped requests cannot acquire another explicitly captured address.
        unresolved = prepare_owl_document(
            f"Ontology(<urn:missing> Import(<{requested}>) "
            "Declaration(Class(<urn:Root>)))",
            format="functional",
            document_iri="urn:missing:document",
        )
        decoy = prepare_owl_document(
            "Ontology(<urn:decoy> Declaration(Class(<urn:Wrong>)))",
            format="functional",
            document_iri="urn:decoy:document",
            effective_url="urn:bijux:unmapped-import",
        )
        denied = convert_ontology(
            backend, unresolved, format="functional", imports={"urn:unused": decoy}
        )
        assert not denied.report.verified and denied.content is None
        self_loop = prepare_owl_document(
            unresolved.content,
            format="functional",
            document_iri="urn:self:document",
            effective_url="urn:bijux:unmapped-import",
        )
        assert not convert_ontology(
            backend, self_loop, format="functional"
        ).report.verified
        explicit = convert_ontology(
            backend, unresolved, format="functional", imports={requested: decoy}
        )
        assert explicit.report.verified and len(explicit.documents) == 2
        missing = convert_ontology(backend, root, format="functional")
        assert not missing.report.verified and missing.content is None
        external_xml = prepare_owl_document(
            f'<!DOCTYPE Ontology SYSTEM "{requested}">' + _XML,
            format="owlxml",
            document_iri="urn:external",
        )
        assert not convert_ontology(
            backend, external_xml, format="functional"
        ).report.verified
        assert contacts == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_obo_interchange_and_explicit_loss_policy(backend: OwlProfileBackend) -> None:
    """OBO roundtrips retain actual axioms and expose unsupported constructs."""
    obo = prepare_owl_document(_OBO, format="obo", document_iri="urn:obo:document")
    result = convert_ontology(backend, obo, format="functional", profiles=_PROFILES)
    assert result.report.verified and result.documents[0].axiom_count == 12
    assert convert_ontology(backend, obo, format="obo").report.verified
    union = prepare_owl_document(
        "Ontology(<urn:union> Declaration(Class(<urn:A>)) "
        "Declaration(Class(<urn:B>)) Declaration(Class(<urn:C>)) "
        "SubClassOf(<urn:A> ObjectUnionOf(<urn:B> <urn:C>)))",
        format="functional",
        document_iri="urn:union:document",
    )
    rejected = convert_ontology(backend, union, format="obo")
    assert rejected.report.outcome == "disagreement"
    assert rejected.lossless is False and rejected.content is None
    assert any(item.kind == "untranslatable_axiom" for item in rejected.changes)
    prefix = convert_ontology(backend, union, format="obo", limits=Limits(max_issues=1))
    assert prefix.report.outcome == "exhausted" and prefix.content is None
    assert prefix.changes and prefix.report.issues[0].code == "owl.conversion_loss"
    reported = convert_ontology(backend, union, format="obo", loss="report")
    assert reported.content and reported.lossless is False
    assert not reported.report.verified and reported.changes


def test_parser_completeness_and_execution_bounds(backend: OwlProfileBackend) -> None:
    """Parser omissions and incomplete work cannot qualify native output."""
    for syntax, raw in (
        ("owlxml", _XML.replace("</Ontology>", "<UnknownConstruct/></Ontology>")),
        (
            "owlxml",
            '<Ontology xmlns="http://www.w3.org/2002/07/owl#" '
            'ontologyIRI="urn:both">'
            '<Prefix name="ex" IRI="urn:example:"/><Declaration>'
            '<Class IRI="urn:First" abbreviatedIRI="ex:Second"/>'
            "</Declaration></Ontology>",
        ),
        ("functional", _FUNCTIONAL + " Declaration(Class(<urn:lost>))"),
        ("manchester", _MANCHESTER + "UnknownConstruct: <urn:lost>\n"),
        ("owlxml", _XML.replace('IRI="urn:C"', 'IRI="Child"', 1)),
        (
            "owlxml",
            '<Ontology xmlns="http://www.w3.org/2002/07/owl#" '
            'ontologyIRI="urn:relative"><Annotation>'
            '<AnnotationProperty IRI="urn:p"/><IRI>Child</IRI>'
            "</Annotation></Ontology>",
        ),
        (
            "owlxml",
            '<Ontology xmlns="http://www.w3.org/2002/07/owl#">'
            '<Prefix name="ex" IRI="relative/"/>'
            '<Declaration><Class abbreviatedIRI="ex:C"/></Declaration></Ontology>',
        ),
        ("functional", "Ontology(<urn:relative> Declaration(Class(<Child>)))"),
        (
            "functional",
            "Ontology(<urn:relative> Annotation(<urn:p> <Child>) "
            "Declaration(AnnotationProperty(<urn:p>)))",
        ),
        (
            "owlxml",
            _XML.replace(
                'ontologyIRI="urn:native"',
                'xml:base="https://example.org/base" ontologyIRI="urn:native"',
            ).replace('IRI="urn:C"', 'IRI="#Child"', 1),
        ),
    ):
        document = prepare_owl_document(
            raw, format=cast(OwlSyntax, syntax), document_iri="urn:invalid"
        )
        result = convert_ontology(backend, document, format="functional")
        assert not result.report.verified and result.content is None
    absolute_base = prepare_owl_document(
        _XML.replace(
            'ontologyIRI="urn:native"',
            'xml:base="https://example.org/base" ontologyIRI="urn:native"',
        ),
        format="owlxml",
        document_iri="urn:base:document",
    )
    assert convert_ontology(backend, absolute_base, format="functional").report.verified
    literal = prepare_owl_document(
        "Ontology(<urn:literal> "
        'Annotation(<urn:p> "Child"^^<http://www.w3.org/2001/XMLSchema#anyURI>) '
        "Declaration(AnnotationProperty(<urn:p>)))",
        format="functional",
        document_iri="urn:literal:document",
    )
    assert convert_ontology(backend, literal, format="functional").report.verified
    prefixed = prepare_owl_document(
        '<Ontology xmlns="http://www.w3.org/2002/07/owl#" ontologyIRI="urn:prefixed">'
        '<Prefix name="ex" IRI="urn:native:"/>'
        '<Declaration><Class abbreviatedIRI="ex:C"/></Declaration></Ontology>',
        format="owlxml",
        document_iri="urn:prefix:document",
    )
    assert convert_ontology(backend, prefixed, format="functional").report.verified
    document = prepare_owl_document(
        _FUNCTIONAL, format="functional", document_iri="urn:source"
    )
    cancel = threading.Event()
    cancel.set()
    stopped = convert_ontology(backend, document, format="functional", cancel=cancel)
    assert stopped.report.outcome == "exhausted" and stopped.content is None
    operation = Budget(Limits(max_work=1))
    bounded = convert_ontology(backend, document, format="functional", budget=operation)
    assert bounded.report.outcome == "exhausted" and bounded.content is None
    assert convert_ontology(backend, document, format="owlxml").report.verified
    classpath_bytes = sum(
        len(str(backend.directory / name).encode()) + 1 for name in backend.jars
    )
    output_cap = max(10000, classpath_bytes + 1)
    expanded = prepare_owl_document(
        "Prefix(:=<urn:>) Ontology(<urn:bounded> Declaration(Class(:Root)) "
        + " ".join(
            f"Declaration(Class(:C{i})) SubClassOf(:C{i} :Root)"
            for i in range(max(80, output_cap // 80))
        )
        + ")",
        format="functional",
        document_iri="urn:bounded",
    )
    assert len(expanded.content) < output_cap
    ordinary_output = convert_ontology(backend, expanded, format="owlxml")
    assert ordinary_output.report.verified and ordinary_output.content is not None
    assert len(ordinary_output.content) > output_cap
    limited_output = convert_ontology(
        backend,
        expanded,
        format="owlxml",
        limits=Limits(max_bytes=output_cap),
    )
    assert limited_output.report.outcome == "exhausted"
    assert limited_output.content is None
    assert any(
        "Native conversion did not finish" in i.message for i in limited_output.report
    )
