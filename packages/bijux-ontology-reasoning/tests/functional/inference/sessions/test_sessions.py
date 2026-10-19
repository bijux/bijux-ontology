"""Actual persistent HermiT queries, fixed revisions and finite RDF projections."""

import os
import shutil
import sys
import time
from pathlib import Path
from threading import Event, Thread
from types import FrameType
from typing import cast

import pytest
from bijux_ontology_reasoning.axioms.types import (
    NamedEntity,
    ObjectProperty,
    ObjectQuantifier,
    SubClassOf,
)
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.backend.types import ProfileDocument
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document
from bijux_ontology_reasoning.entailment import EntailmentCheck, check_entailments
from bijux_ontology_reasoning.inference.relationships.types import InferredContracts
from bijux_ontology_reasoning.relationships import InferredQuery
from bijux_ontology_reasoning.sessions import (
    InferenceSelection,
    PreparedReasoningTheory,
    ReasoningSession,
    prepare_reasoning_theory,
)
from rdflib import BNode, Dataset, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.restrictions import project_restrictions

_SOURCE = """Ontology(<urn:session>
 Declaration(Class(<urn:A>)) Declaration(Class(<urn:B>))
 Declaration(Class(<urn:C>)) Declaration(Class(<urn:D>))
 Declaration(ObjectProperty(<urn:p>)) Declaration(ObjectProperty(<urn:q>))
 SubClassOf(<urn:A> <urn:B>)
 SubClassOf(<urn:B> ObjectSomeValuesFrom(<urn:p> <urn:C>))
 SubObjectPropertyOf(<urn:p> <urn:q>))"""


def _class(name: str) -> NamedEntity:
    """Use explicit native class roles in every requested obligation."""
    return NamedEntity("class", "urn:" + name)


def _some(filler: str = "C") -> SubClassOf:
    """Preserve the existential rather than asking for an instance assertion."""
    return SubClassOf(
        _class("A"),
        ObjectQuantifier("some", ObjectProperty("urn:p"), _class(filler)),
        (),
    )


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Require the normally installed caller-pinned real maintained backend."""
    java = os.environ.get("BIJUX_OWL_JAVA")
    directory = os.environ.get("BIJUX_OWL_BUNDLE")
    pin = os.environ.get("BIJUX_OWL_MANIFEST_SHA256")
    if not (java and directory and pin):
        pytest.skip("An explicit pinned OWL backend is not configured")
    return prepare_profile_backend(
        Path(java), Path(directory), manifest_sha256=pin, artifacts=tmp_path / "native"
    )


def test_one_native_preparation_repeated_obligations_and_batches(
    backend: OwlProfileBackend,
) -> None:
    """Changed obligations get new answers while one classification stays live."""
    original = prepare_owl_document(
        _SOURCE, format="functional", document_iri="urn:source"
    )
    theory = prepare_reasoning_theory(backend, original)
    with ReasoningSession(theory) as session:
        assert session.report.verified
        first = session.check_entailments((_some(),))
        changed = session.check_entailments((_some("D"),))
        repeated = session.check_entailments((_some(), _some("D")))
        independent = check_entailments(backend, original, (_some(), _some("D")))
        assert first.report.verified
        assert changed.answers[0].outcome == "not_entailed"
        assert (
            tuple(a.outcome for a in repeated.answers)
            == tuple(a.outcome for a in independent.answers)
            == ("entailed", "not_entailed")
        )
        assert repeated.report.complete and not repeated.report.verified
        assert session.launches == session.classifications == 1
        bounded = session.query_inferred_contracts(
            (InferredQuery(_class("A"), "super", False),), max_results=1
        )
        assert not bounded.report.complete
        assert session.check_entailments((_some(),)).report.verified
        assert session.launches == session.classifications == 2
        assert (
            session.preparation.loads == 1
            and session.preparation.process_id is not None
        )
        assert session.bind(theory).verified and session.launches == 2
        assert original.content == _SOURCE.encode()
    assert not session.check_entailments((_some(),)).report.verified


def test_named_relationships_reuse_actual_reasoner_and_original_scope(
    backend: OwlProfileBackend,
) -> None:
    """Existing native taxonomy/property helpers run without a second load."""
    theory = prepare_reasoning_theory(
        backend,
        prepare_owl_document(_SOURCE, format="functional", document_iri="urn:source"),
    )
    with ReasoningSession(theory) as session:
        result = session.query_inferred_contracts(
            (
                InferredQuery(_class("A"), "super", True),
                InferredQuery(ObjectProperty("urn:p"), "super", False),
            )
        )
        assert result.report.verified and result.consistent is True
        assert _class("B") in {
            m.value for group in result.answers[0].groups for m in group.members
        }
        assert ObjectProperty("urn:q") in {
            m.value for group in result.answers[1].groups for m in group.members
        }
        assert session.launches == session.classifications == 1


def test_selected_physical_rdf_graph_does_not_union_other_assertions(
    backend: OwlProfileBackend,
) -> None:
    """Actual RDF loader receipts retain the selected graph and literal profile."""
    dataset = Dataset()
    selected = dataset.graph(URIRef("urn:selected"))
    selected.add((URIRef("urn:ontology"), RDF.type, OWL.Ontology))
    for name in ("A", "B", "C"):
        selected.add((URIRef("urn:" + name), RDF.type, OWL.Class))
    selected.add((URIRef("urn:p"), RDF.type, OWL.ObjectProperty))
    selected.add((URIRef("urn:A"), RDFS.subClassOf, URIRef("urn:B")))
    restriction = BNode()
    selected.add((URIRef("urn:B"), RDFS.subClassOf, restriction))
    selected.add((restriction, RDF.type, OWL.Restriction))
    selected.add((restriction, OWL.onProperty, URIRef("urn:p")))
    selected.add((restriction, OWL.someValuesFrom, URIRef("urn:C")))
    dataset.graph(URIRef("urn:excluded")).add(
        (URIRef("urn:A"), RDFS.subClassOf, OWL.Nothing)
    )
    original = prepare_rdf(
        RdfSource(dataset, graph_name="urn:selected", document_iri="urn:source")
    )
    theory = prepare_reasoning_theory(backend, original)
    with ReasoningSession(theory) as session:
        checked = session.check_entailments((_some(),))
        assert checked.report.verified
        assert checked.loaders and all(item.qualified for item in checked.loaders)
        assert isinstance(checked.documents[0], ProfileDocument)
        assert checked.documents[0].graph_name == URIRef("urn:selected")
        assert theory.rdf_sources and theory.rdf_sources[0] is original
        assert len(dataset.graph(URIRef("urn:excluded"))) == 1


def test_changed_backend_seal_invalidates_before_another_query(
    backend: OwlProfileBackend,
    tmp_path: Path,
) -> None:
    """A changed native bundle cannot silently retain an accepted theory receipt."""
    copied = tmp_path / "bundle"
    shutil.copytree(backend.directory, copied)
    local = prepare_profile_backend(
        backend.java,
        copied,
        manifest_sha256=backend.manifest_sha256,
        artifacts=tmp_path / "sealed-native",
    )
    original = prepare_owl_document(
        _SOURCE, format="functional", document_iri="urn:source"
    )
    theory = prepare_reasoning_theory(local, original)
    with ReasoningSession(theory) as session:
        assert session.check_entailments((_some(),)).report.verified
        jar = copied / "owl-profile-worker-0.1.0.jar"
        before = jar.stat()
        os.utime(jar, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        stale = session.check_entailments((_some(),))
        assert not stale.report.verified and stale.answers[0].outcome == "unknown"
        assert session.launches == 1
        current = prepare_profile_backend(
            local.java,
            copied,
            manifest_sha256=local.manifest_sha256,
            artifacts=tmp_path / "renewed-native",
        )
        refreshed = prepare_reasoning_theory(current, original)
        assert session.bind(refreshed).verified
        assert session.check_entailments((_some(),)).report.verified
        assert session.launches == session.classifications == 2


def test_explicit_import_revision_binding_refuses_stale_expected_theory(
    backend: OwlProfileBackend,
) -> None:
    """Changed imported axioms invalidate preparation, not merely an answer key."""
    source = prepare_owl_document(
        "Ontology(<urn:root> Import(<urn:import>) Declaration(Class(<urn:A>)) "
        "SubClassOf(<urn:A> <urn:B>))",
        format="functional",
        document_iri="urn:root",
    )

    def imported(target: str) -> OwlDocument:
        """Capture a replacement at the same actual import identity."""
        return prepare_owl_document(
            "Ontology(<urn:import> Declaration(Class(<urn:B>)) "
            "Declaration(Class(<urn:C>)) Declaration(Class(<urn:D>)) "
            f"SubClassOf(<urn:B> <urn:{target}>))",
            format="functional",
            document_iri="urn:import",
        )

    old = prepare_reasoning_theory(
        backend, source, imports={"urn:import": imported("C")}
    )
    new = prepare_reasoning_theory(
        backend, source, imports={"urn:import": imported("D")}
    )
    query = SubClassOf(_class("A"), _class("C"), ())
    assert old.identity != new.identity
    with ReasoningSession(old) as session:
        assert session.check_entailments((query,)).report.verified
        assert not session.check_entailments((query,), theory=new).report.verified
        assert session.bind(new).verified
        changed = session.check_entailments((query,), theory=new)
        assert changed.answers[0].outcome == "not_entailed"
        assert session.launches == session.classifications == 2


def test_finite_inherited_existential_projection_is_ordinary_rdf(
    backend: OwlProfileBackend,
) -> None:
    """Known and absent selected pairs coexist without inventing instance facts."""
    original = prepare_owl_document(
        _SOURCE, format="functional", document_iri="urn:source"
    )
    theory = prepare_reasoning_theory(backend, original)
    with ReasoningSession(theory) as session:
        selected = InferenceSelection(
            (_class("A"),),
            properties=(ObjectProperty("urn:p"),),
            fillers=(_class("C"), _class("D")),
            families=("existential",),
        )
        projected = session.project(selected)
        assert projected.report.verified and projected.source is not None
        assert projected.answers is not None and not projected.answers.report.verified
        assert len(projected.edges) == 1 and projected.edges[0].quantifier == "some"
        view = VocabularyView(load_vocabulary(projected.source))
        restrictions = project_restrictions(view, "urn:A")
        assert restrictions.report.verified
        assert any(
            r.property == URIRef("urn:p")
            and r.filler == URIRef("urn:C")
            and r.quantifier == OWL.someValuesFrom
            for r in restrictions.items
        )
        graph = projected.source.graph()
        assert (URIRef("urn:A"), URIRef("urn:p"), URIRef("urn:C")) not in graph
        assert original.content == _SOURCE.encode()
        unknown = session.project(
            InferenceSelection(
                (_class("A"),),
                properties=(ObjectProperty("urn:p"),),
                fillers=(_class("missing"),),
                families=("existential",),
            )
        )
        assert not unknown.report.complete and unknown.source is None
        assert (
            unknown.answers is not None
            and unknown.answers.answers[0].outcome == "unknown"
        )
        assert session.classifications == 1


def test_unsupported_query_recovers_and_inconsistent_theory_cannot_prove(
    backend: OwlProfileBackend,
) -> None:
    """Unknown signature and inconsistent theory stay separate from non-entailment."""
    theory = prepare_reasoning_theory(
        backend,
        prepare_owl_document(_SOURCE, format="functional", document_iri="urn:source"),
    )
    with ReasoningSession(theory) as session:
        unknown = session.check_entailments((_some("missing"),))
        assert unknown.answers[0].outcome == "unknown" and not unknown.report.complete
        assert session.check_entailments((_some(),)).report.verified
        assert session.launches == 1
    inconsistent = prepare_reasoning_theory(
        backend,
        prepare_owl_document(
            "Ontology(<urn:bad> Declaration(Class(<urn:A>)) "
            "Declaration(NamedIndividual(<urn:x>)) SubClassOf(<urn:A> owl:Nothing) "
            "ClassAssertion(<urn:A> <urn:x>))",
            format="functional",
            document_iri="urn:bad",
        ),
    )
    with ReasoningSession(inconsistent) as session:
        result = session.check_entailments((SubClassOf(_class("A"), _class("A"), ()),))
        assert result.consistent is False and not result.report.verified
        assert any(i.code == "owl.inconsistent" for i in result.report)
        assert session.classifications == 0


def test_request_counters_cancellation_and_paid_negative_prefix(
    backend: OwlProfileBackend,
) -> None:
    """Repeated host work is cumulative and an actual paid negative survives stop."""
    theory = prepare_reasoning_theory(
        backend,
        prepare_owl_document(_SOURCE, format="functional", document_iri="urn:source"),
    )
    operation = Budget(Limits(max_work=2_000_000, max_total_bytes=100_000_000))
    with ReasoningSession(theory, budget=operation) as session:
        before = dict(operation._counts)
        assert session.check_entailments((_some(),)).report.verified

        after = dict(operation._counts)
        assert all(after.get(k, 0) >= v for k, v in before.items())
        assert after["work"] > before["work"] and after["bytes"] > before["bytes"]
    with ReasoningSession(theory) as session:
        stopped = session.check_entailments(
            (_some("D"), _some("D")), budget=Budget(Limits(max_issues=1))
        )
        assert any(i.code == "owl.not_entailed" for i in stopped.report)
        assert any(i.kind == "exhausted" for i in stopped.report)
        event = Event()
        event.set()
        assert not session.check_entailments((_some(),), cancel=event).report.verified
        assert session.check_entailments((_some(),)).report.verified


def test_waiting_requests_keep_their_deadlines_without_resetting_native_owner(
    backend: OwlProfileBackend,
) -> None:
    """Queued stops cannot wait indefinitely or disrupt the active native batch."""
    limits = Limits(
        max_work=50_000_000, max_states=5_000_000, max_total_bytes=1_000_000_000
    )
    theory = prepare_reasoning_theory(
        backend,
        prepare_owl_document(_SOURCE, format="functional", document_iri="urn:source"),
    )
    with ReasoningSession(theory, limits=limits) as session:
        entered = Event()
        values: list[EntailmentCheck] = []
        failures: list[BaseException] = []

        def observe(frame: FrameType, event: str, arg: object) -> None:
            """Observe actual query transport entry without replacing execution."""
            if event == "call" and frame.f_code.co_name == "_exchange":
                payload = frame.f_locals.get("payload", b"")
                if b'"operation":"entailment"' in payload[:100]:
                    entered.set()

        def run() -> None:
            """Keep a real maintained query batch active behind the public API."""
            sys.setprofile(observe)
            try:
                values.append(session.check_entailments((_some(),) * 10000, timeout=20))
            except BaseException as error:
                failures.append(error)
            finally:
                sys.setprofile(None)

        owner = Thread(target=run)
        owner.start()
        assert entered.wait(15)
        started = time.monotonic()
        stopped = session.check_entailments((_some(),), timeout=0.02)
        assert time.monotonic() - started < 0.5
        assert any(i.kind == "exhausted" for i in stopped.report)
        assert any(i.kind == "exhausted" for i in session.bind(theory, timeout=0.02))
        inferred = session.query_inferred_contracts(
            (InferredQuery(_class("A"), "super", True),), timeout=0.02
        )
        assert any(i.kind == "exhausted" for i in inferred.report)
        projection = session.project(
            InferenceSelection(
                (_class("A"),),
                properties=(ObjectProperty("urn:p"),),
                fillers=(_class("C"),),
                families=("existential",),
            ),
            timeout=0.02,
        )
        assert not projection.report.complete and projection.source is None
        cancelled = Event()
        cancelled.set()
        assert not session.check_entailments(
            (_some(),), cancel=cancelled
        ).report.verified
        owner.join(20)
        assert not owner.is_alive() and not failures and values[0].report.verified
        assert session.launches == session.classifications == 1
        assert session.report.verified


def test_binding_handoffs_keep_cleanup_and_answer_provenance_with_the_owner(
    backend: OwlProfileBackend,
) -> None:
    """A later binding cannot be reset or attached to an earlier native answer."""

    def capture(target: str) -> PreparedReasoningTheory:
        """Capture two different revisions at the same source ontology identity."""
        return prepare_reasoning_theory(
            backend,
            prepare_owl_document(
                "Ontology(<urn:scope> Declaration(Class(<urn:A>)) "
                "Declaration(Class(<urn:B>)) Declaration(Class(<urn:C>)) "
                f"SubClassOf(<urn:A> <urn:{target}>))",
                format="functional",
                document_iri="urn:scope",
            ),
        )

    old, new = capture("B"), capture("C")
    query = SubClassOf(_class("A"), _class("B"), ())

    def check_handoff(kind: str, caller: str) -> None:
        """Keep each pair of native owners in one independent closure scope."""
        with ReasoningSession(old) as session:
            paused, release = Event(), Event()
            values: list[Report | EntailmentCheck | InferredContracts] = []
            failures: list[BaseException] = []

            def observe(frame: FrameType, event: str, arg: object) -> None:
                """Schedule a real binding immediately after ownership is released."""
                if event != "return" or frame.f_code.co_name != "__exit__":
                    return
                if frame.f_back is None or frame.f_back.f_code.co_name != caller:
                    return
                context = frame.f_locals.get("self")
                code = getattr(getattr(context, "gen", None), "gi_code", None)
                if (
                    code is not None
                    and code.co_name == "_locked"
                    and not paused.is_set()
                ):
                    assert not session._lock.locked()
                    paused.set()
                    assert release.wait(15)

            def run() -> None:
                """Observe scheduling while running the original native operation."""
                sys.setprofile(observe)
                try:
                    if kind == "bind":
                        values.append(session.bind(cast(PreparedReasoningTheory, None)))
                    elif kind == "entailment":
                        values.append(session.check_entailments((query,)))
                    else:
                        values.append(
                            session.query_inferred_contracts(
                                (InferredQuery(_class("A"), "super", False),)
                            )
                        )
                except BaseException as error:
                    failures.append(error)
                finally:
                    sys.setprofile(None)

            owner = Thread(target=run)
            owner.start()
            assert paused.wait(15)
            try:
                assert session.bind(new).verified
                assert session.check_entailments(
                    (SubClassOf(_class("A"), _class("C"), ()),)
                ).report.verified
            finally:
                release.set()
            owner.join(15)
            assert not owner.is_alive() and not failures
            result = values[0]
            if isinstance(result, Report):
                assert result.outcome == "input_error"
            else:
                assert result.report.verified and result.documents == old.documents
                if isinstance(result, InferredContracts):
                    assert all(
                        document in old.documents
                        for answer in result.answers
                        for group in answer.groups
                        for member in group.members
                        for document in member.asserted_in
                    )
            assert session.theory is new and session.report.verified
            assert session.check_entailments(
                (SubClassOf(_class("A"), _class("C"), ()),)
            ).report.verified

    for kind, caller in (
        ("bind", "bind"),
        ("entailment", "check_entailments"),
        ("relationships", "query_inferred_contracts"),
    ):
        check_handoff(kind, caller)
