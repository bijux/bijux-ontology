"""Actual immutable preparation, atomic activation and in-flight revision leases."""

import os
import sys
from pathlib import Path
from threading import Barrier, Event, Thread
from types import FrameType

import pytest
from rdflib import URIRef

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.execution.processes.sessions import FramedSession
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.shacl.execution.preparation import prepare_session
from bijux_ontology.shacl.execution.session import ShaclSession
from bijux_ontology.shacl.execution.transport import SessionBackend
from bijux_ontology.workflows.revisions import (
    PreparedRevision,
    RevisionLease,
    RevisionManager,
    RevisionResult,
    ShapePreparationProbe,
    prepare_revision,
)

E = "https://example.org/revisions/"
X = "http://www.w3.org/2001/XMLSchema#string"


def ontology(name: str) -> RdfSource:
    """Give revisions genuinely different class, property and range ownership."""
    return RdfSource(
        "@prefix ex: <" + E + "> . "
        "@prefix owl: <http://www.w3.org/2002/07/owl#> . "
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> . "
        "ex:" + name + " a owl:Class . ex:" + name + "label a owl:DatatypeProperty ; "
        "rdfs:domain ex:" + name + " ; rdfs:range <" + X + "> .",
        format="turtle",
    )


def models(name: str) -> tuple[ModelDeclaration, ...]:
    """Compile one real declaration rather than accepting a labelled callback."""
    return (
        ModelDeclaration(
            "node",
            (E + name,),
            (
                FieldDeclaration(
                    "label",
                    E + name + "label",
                    (ValueDeclaration("literal", datatype=X),),
                    emission="guaranteed",
                ),
            ),
        ),
    )


def data(name: str, *, invalid: bool = False) -> RdfSource:
    """Use exact revision RDF identity with an optional missing required field."""
    return RdfSource(
        "<"
        + E
        + "root> a <"
        + E
        + name
        + "> . "
        + ("" if invalid else "<" + E + "root> <" + E + name + 'label> "valid" .'),
        format="turtle",
    )


def shapes(name: str) -> RdfSource:
    """A changed actual shape path detects mixed revisions independently of models."""
    return RdfSource(
        "@prefix ex: <" + E + "> . @prefix sh: <http://www.w3.org/ns/shacl#> . "
        "ex:Shape a sh:NodeShape ; sh:property [ sh:path ex:"
        + name
        + "label ; sh:minCount 1 ; sh:datatype <"
        + X
        + "> ] .",
        format="turtle",
    )


def prepared(name: str, *, native_shapes: bool = False) -> PreparedRevision:
    """Require existing actual compiled model preparation and optional native probes."""
    if native_shapes:
        pytest.importorskip("pyld")
        pytest.importorskip("pyshacl")
        result = prepare_revision(
            models(name),
            ontology(name),
            context={"Node": E + name, "label": E + name + "label"},
            shapes=shapes(name),
            shape_roots=(E + "Shape",),
            shape_probes=(ShapePreparationProbe(data(name), (E + "root",)),),
        )
    else:
        result = prepare_revision(models(name), ontology(name))
    assert result.report.verified, result.report
    assert result.prepared is not None
    return result.prepared


def check(lease: RevisionLease, name: str, *, invalid: bool = False) -> RevisionResult:
    """Use the ordinary public RDF checker for every supported minimal-core run."""
    return lease.check_rdf(
        prepare_source(data(name, invalid=invalid)),
        (RdfModelUsage("node", URIRef(E + "root")),),
    )


def test_running_request_retains_coherent_resources_after_activation() -> None:
    """A suspended application request resumes on A after B becomes active."""
    first, second = prepared("A", native_shapes=True), prepared("B", native_shapes=True)
    manager = RevisionManager()
    manager.activate(first)
    entered, resume = Event(), Event()
    results: list[RevisionResult] = []
    errors: list[BaseException] = []
    document = {"@id": E + "root", "@type": "Node", "label": "valid"}

    def request() -> None:
        """Hold a real leased application request across atomic publication."""
        try:
            with manager.lease() as lease:
                entered.set()
                assert resume.wait(10)
                results.append(lease.check_emission(document, (ModelUsage("node"),)))
                results.append(
                    lease.validate_shapes(
                        prepare_source(data("A")), focus_nodes=(E + "root",)
                    )
                )
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=request)
    thread.start()
    assert entered.wait(10)
    manager.activate(second, expected_active=first.revision)
    with manager.lease() as lease:
        assert lease.revision == second.revision
        current = lease.check_emission(document, (ModelUsage("node"),))
        native = lease.validate_shapes(
            prepare_source(data("B")), focus_nodes=(E + "root",)
        )
        crossed = lease.validate_shapes(
            prepare_source(data("A")), focus_nodes=(E + "root",)
        )
    resume.set()
    thread.join(10)
    assert not thread.is_alive() and not errors
    assert all(
        result.revision == first.revision and result.report.verified
        for result in results
    )
    assert current.revision == native.revision == second.revision
    assert current.report.verified and native.report.verified
    assert crossed.shacl and crossed.shacl.conforms is False
    assert not crossed.report.verified
    assert manager.close().closed


def test_failed_context_import_probe_or_mutable_path_never_replaces_active(
    tmp_path: Path,
) -> None:
    """Every required failure remains actual evidence while the active revision runs."""
    pytest.importorskip("pyld")
    pytest.importorskip("pyshacl")
    first = prepared("A")
    manager = RevisionManager()
    manager.activate(first)
    original = ontology("A").content
    assert isinstance(original, str)
    missing_import = RdfSource(
        original
        + " <urn:ontology> <http://www.w3.org/2002/07/owl#imports> <urn:missing> .",
        format="turtle",
    )
    path = tmp_path / "ontology.ttl"
    path.write_text(str(ontology("A").content))
    failed = (
        prepare_revision(models("A"), ontology("A"), context=E + "missing"),
        prepare_revision(models("A"), missing_import),
        prepare_revision(models("A"), RdfSource(path, format="turtle")),
        prepare_revision(
            models("A"),
            ontology("A"),
            shapes=shapes("A"),
            shape_roots=(E + "Shape",),
            shape_probes=(
                ShapePreparationProbe(data("A", invalid=True), (E + "root",)),
            ),
        ),
        prepare_revision(
            models("A"),
            ontology("A"),
            shapes=RdfSource(
                str(shapes("A").content)
                + " <"
                + E
                + 'Shape> <http://www.w3.org/ns/shacl#js> "unknown" .',
                format="turtle",
            ),
            shape_roots=(E + "Shape",),
            shape_probes=(ShapePreparationProbe(data("A"), (E + "root",)),),
        ),
    )
    for result in failed:
        assert result.prepared is None and not result.report.verified
        assert manager.status().active == first.revision
        with manager.lease() as lease:
            assert check(lease, "A").report.verified
    assert any(
        issue.kind == "finding" and issue.code.startswith("shacl.")
        for issue in failed[3].report
    )
    manager.close()


def test_retirement_limits_rollback_and_close_preserve_live_leases() -> None:
    """Retention is bounded without evicting a leased or rollback revision."""
    first, second, third = prepared("A"), prepared("B"), prepared("C")
    manager = RevisionManager(max_retained=2)
    manager.activate(first)
    old = manager.lease()
    manager.activate(second, expected_active=first.revision)
    current = manager.lease()
    with pytest.raises(LimitExceeded):
        manager.activate(third, expected_active=second.revision)
    assert manager.status().active == second.revision
    with pytest.raises(ResourceError):
        manager.retire(first.revision)
    manager.rollback(first.revision, expected_active=second.revision)
    assert check(current, "B").report.verified and check(old, "A").report.verified
    current.close()
    manager.retire(second.revision)
    manager.activate(third, expected_active=first.revision)
    status = manager.close()
    assert status.closing and not status.closed and status.leases == 1
    with pytest.raises(ResourceError):
        manager.lease()
    assert check(old, "A").report.verified
    old.close()
    assert manager.status().closed and manager.status().retained_bytes == 0
    with pytest.raises(InputError):
        check(old, "A")


def test_preparation_byte_cap_single_transfer_and_stale_base_do_not_mutate_active() -> (
    None
):
    """Failed publication cannot steal the candidate or disturb the current owner."""
    first, second = prepared("A"), prepared("B")
    manager = RevisionManager(max_retained_bytes=first.retained_bytes)
    manager.activate(first)
    with pytest.raises(LimitExceeded):
        manager.activate(second, expected_active=first.revision)
    with manager.lease() as lease:
        assert check(lease, "A").report.verified
        assert not check(lease, "A", invalid=True).report.verified
    manager.close()
    another = RevisionManager()
    with pytest.raises(InputError):
        another.activate(first)
    another.activate(second)
    third = prepared("C")
    with pytest.raises(ResourceError):
        another.activate(third, expected_active=first.revision)
    assert another.status().active == second.revision
    third.close()
    another.close()


def test_parallel_publication_has_one_exact_base_winner() -> None:
    """Concurrent proposals cannot last-write-win or combine resource revisions."""
    first, second, third = prepared("A"), prepared("B"), prepared("C")
    manager = RevisionManager(max_retained=3)
    manager.activate(first)
    barrier = Barrier(3)
    accepted: list[str] = []
    rejected: list[str] = []

    def activate(candidate: PreparedRevision) -> None:
        """Compete only at the actual public compare-and-swap boundary."""
        barrier.wait()
        try:
            manager.activate(candidate, expected_active=first.revision)
            accepted.append(candidate.revision)
        except ResourceError:
            rejected.append(candidate.revision)
            candidate.close()

    threads = [
        Thread(target=activate, args=(candidate,)) for candidate in (second, third)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert len(accepted) == len(rejected) == 1
    assert manager.status().active == accepted[0]
    with manager.lease() as lease:
        name = "B" if accepted[0] == second.revision else "C"
        assert check(lease, name).report.verified
    manager.close()


def test_owned_json_and_exhausted_or_cancelled_preparation() -> None:
    """Later caller JSON edits cannot change a sealed prepared semantic revision."""
    pytest.importorskip("pyld")
    context = {"Node": E + "A", "label": E + "Alabel"}
    resources = {E + "context": {"@context": context}}
    result = prepare_revision(
        models("A"), ontology("A"), context=E + "context", resources=resources
    )
    assert result.report.verified and result.prepared
    context["label"] = E + "foreign"
    manager = RevisionManager()
    manager.activate(result.prepared)
    with manager.lease() as lease:
        assert lease.check_emission(
            {"@id": E + "root", "@type": "Node", "label": "valid"},
            (ModelUsage("node"),),
        ).report.verified
    manager.close()
    tiny = prepare_revision(
        models("A"), ontology("A"), budget=Budget(Limits(max_work=1))
    )
    assert tiny.prepared is None and tiny.report.outcome == "exhausted"
    event = Event()
    event.set()
    cancelled = prepare_revision(models("A"), ontology("A"), cancel=event)
    assert cancelled.prepared is None and cancelled.report.outcome == "exhausted"


def test_semantic_identity_binds_closed_inputs_independently_of_activation_uuid() -> (
    None
):
    """Equal inputs can be resumed; changed shape or probe bytes cannot share a key."""
    first, equal, changed = prepared("A"), prepared("A"), prepared("B")
    assert first.revision != equal.revision and first.identity == equal.identity
    assert first.identity.inputs_sha256 != changed.identity.inputs_sha256
    assert first.identity.engine_sha256 == changed.identity.engine_sha256
    resumed = prepare_revision(models("A"), ontology("A"), expected=first.identity)
    assert resumed.prepared and resumed.report.verified
    assert resumed.prepared.identity == first.identity
    resumed.prepared.close()
    mismatched = prepare_revision(models("B"), ontology("B"), expected=first.identity)
    assert mismatched.prepared is None and not mismatched.report.verified
    manager = RevisionManager()
    manager.activate(first)
    with manager.lease() as lease:
        assert lease.identity == first.identity and check(lease, "A").report.verified
    manager.close()
    equal.close()
    changed.close()
    pytest.importorskip("pyshacl")
    shape = shapes("A").content
    probe = data("A").content
    assert isinstance(shape, str) and isinstance(probe, str)
    variants = []
    for selected_shape, selected_probe in (
        (shape, probe),
        (
            shape
            + "<"
            + E
            + 'Shape> <http://www.w3.org/2000/01/rdf-schema#comment> "changed" .',
            probe,
        ),
        (shape, probe.replace('"valid"', '"different"')),
    ):
        result = prepare_revision(
            models("A"),
            ontology("A"),
            shapes=RdfSource(selected_shape, format="turtle"),
            shape_roots=(E + "Shape",),
            shape_probes=(
                ShapePreparationProbe(
                    RdfSource(selected_probe, format="turtle"), (E + "root",)
                ),
            ),
        )
        assert result.report.verified and result.prepared
        variants.append(result.prepared.identity)
        result.prepared.close()
        resumed_shape = prepare_revision(
            models("A"),
            ontology("A"),
            shapes=RdfSource(selected_shape, format="turtle"),
            shape_roots=(E + "Shape",),
            shape_probes=(
                ShapePreparationProbe(
                    RdfSource(selected_probe, format="turtle"), (E + "root",)
                ),
            ),
            expected=variants[0],
        )
        if len(variants) == 1:
            assert resumed_shape.report.verified and resumed_shape.prepared
            resumed_shape.prepared.close()
        else:
            assert not resumed_shape.report.verified and resumed_shape.prepared is None
    assert len({identity.inputs_sha256 for identity in variants}) == 3
    assert len({identity.engine_sha256 for identity in variants}) == 1
    pytest.importorskip("pyld")
    contextual = prepare_revision(
        models("A"), ontology("A"), context={"label": E + "Alabel"}
    )
    assert contextual.prepared and contextual.report.verified
    changed_context = prepare_revision(
        models("A"),
        ontology("A"),
        context={"label": E + "Alabel", "extra": E + "extra"},
        expected=contextual.prepared.identity,
    )
    assert changed_context.prepared is None and not changed_context.report.verified
    contextual.prepared.close()


def test_revision_workers_reuse_exact_preparation_and_drain_after_native_requests() -> (
    None
):
    """Fresh focus, recovery and queued cancellation stay within each retained owner."""
    first, second = prepared("A", native_shapes=True), prepared("B", native_shapes=True)
    manager = RevisionManager(max_retained=2)
    manager.activate(first)
    lease = manager.lease()
    source = prepare_source(data("A"))
    positive = lease.validate_shapes(source, focus_nodes=(E + "root",))
    session = lease._record.session
    assert session is not None and session.worker_launches == 1
    native = session._backend.generation
    assert native is not None
    negative = lease.validate_shapes(source, focus_nodes=(E + "other",))
    assert (
        positive.report.verified and negative.shacl and negative.shacl.conforms is False
    )
    assert negative.shacl.coverage and negative.shacl.coverage.report.verified
    assert {str(target.focus_node) for target in negative.shacl.coverage.targets} == {
        E + "other"
    }
    assert session._backend.generation is native and session.worker_launches == 1
    preparation = lease._record.state
    assert preparation is not None and preparation.shacl_preparation is not None
    assert preparation.shacl_preparation.plan.budget is None
    with pytest.raises(InputError, match="matching limits"):
        ShaclSession._from_preparation(
            preparation.shacl_preparation, storage=Budget(Limits(max_work=2_000_000))
        )
    tight = Budget(preparation.shacl_preparation.plan.limits)
    tight.consume("bytes", tight.limits.max_total_bytes - 8191)
    with pytest.raises(LimitExceeded, match="max_total_bytes"):
        ShaclSession._from_preparation(preparation.shacl_preparation, storage=tight)
    assert tight._counts.get("states", 0) == 0
    assert session._backend.generation is native

    def deplete_preparation(frame: FrameType, event: str, value: object) -> None:
        """Stop the actual implicit construction allowance between owned stages."""
        if event == "return" and frame.f_code is prepare_session.__code__:
            storage = frame.f_locals["storage"]
            storage.consume("bytes", storage.remaining("bytes") - 8191)

    previous = sys.getprofile()
    sys.setprofile(deplete_preparation)
    try:
        with pytest.raises(LimitExceeded, match="max_total_bytes"):
            ShaclSession(preparation.shacl_preparation.plan.shapes)
    finally:
        sys.setprofile(previous)
    running = native
    entered, resume, queued = Event(), Event(), Event()
    cancel = Event()
    results: list[RevisionResult] = []
    errors: list[BaseException] = []

    def request() -> None:
        """Pause only after the real native reply while the borrowed owner is live."""

        def observe(frame: FrameType, event: str, value: object) -> None:
            if (
                event == "return"
                and frame.f_code is SessionBackend.execute.__code__
                and frame.f_locals.get("self") is session._backend
            ):
                entered.set()
                assert resume.wait(20)

        sys.setprofile(observe)
        try:
            results.append(lease.validate_shapes(source, focus_nodes=(E + "root",)))
        except BaseException as error:
            errors.append(error)
        finally:
            sys.setprofile(None)

    thread = Thread(target=request)
    thread.start()
    assert entered.wait(10)

    def waiter() -> None:
        """Cancel at the real queue checkpoint without touching the active worker."""

        def observe(frame: FrameType, event: str, value: object) -> None:
            if (
                event == "call"
                and frame.f_code is FramedSession.checkpoint.__code__
                and frame.f_locals.get("cancel") is cancel
            ):
                queued.set()
                cancel.set()

        sys.setprofile(observe)
        try:
            results.append(
                lease.validate_shapes(source, focus_nodes=(E + "root",), cancel=cancel)
            )
        except BaseException as error:
            errors.append(error)
        finally:
            sys.setprofile(None)

    waiting = Thread(target=waiter)
    waiting.start()
    waiting.join(10)
    assert not waiting.is_alive() and queued.is_set() and not errors
    assert results[0].report.outcome == "exhausted"
    assert session._backend.generation is running
    cancel_lease = manager.lease()
    manager.activate(second, expected_active=first.revision)
    current_lease = manager.lease()
    current = current_lease.validate_shapes(
        prepare_source(data("B")), focus_nodes=(E + "root",)
    )
    current_session = current_lease._record.session
    assert current.report.verified and current_session is not None
    current_worker = current_session._backend.generation
    assert current_worker is not None and current_worker is not running
    lease.close()
    with pytest.raises(InputError, match="closed"):
        lease.validate_shapes(source, focus_nodes=(E + "root",))
    with pytest.raises(ResourceError, match="in-use"):
        manager.retire(first.revision)
    assert manager.status().operations == 1
    resume.set()
    thread.join(10)
    assert not thread.is_alive() and not errors
    assert results[-1].report.verified and results[-1].revision == first.revision
    active_cancel = Event()

    def cancel_completed(frame: FrameType, event: str, value: object) -> None:
        """Cancel at the actual old worker reply before its host publication."""
        if (
            event == "return"
            and frame.f_code is SessionBackend.execute.__code__
            and frame.f_locals.get("self") is session._backend
        ):
            active_cancel.set()

    previous = sys.getprofile()
    sys.setprofile(cancel_completed)
    try:
        stopped = cancel_lease.validate_shapes(
            source, focus_nodes=(E + "root",), cancel=active_cancel
        )
    finally:
        sys.setprofile(previous)
    assert stopped.revision == first.revision and stopped.report.outcome == "exhausted"
    assert session._backend.generation is not running and session.worker_launches == 1
    with pytest.raises(ProcessLookupError):
        os.kill(running.pid, 0)
    assert current_session._backend.generation is current_worker
    recovered = cancel_lease.validate_shapes(source, focus_nodes=(E + "root",))
    assert recovered.report.verified and recovered.revision == first.revision
    assert session.worker_launches == 2
    running = session._backend.generation
    assert running is not None and running is not native
    cancel_lease.close()
    reaping, reap = Event(), Event()

    def retire() -> None:
        """Observe cleanup before native reaping without holding the manager lock."""

        def observe(frame: FrameType, event: str, value: object) -> None:
            if (
                event == "call"
                and frame.f_code is ShaclSession.close.__code__
                and frame.f_locals.get("self") is session
            ):
                reaping.set()
                assert reap.wait(10)

        sys.setprofile(observe)
        try:
            manager.retire(first.revision)
        except BaseException as error:
            errors.append(error)
        finally:
            sys.setprofile(None)

    retiring = Thread(target=retire)
    retiring.start()
    assert reaping.wait(10)
    status = manager.status()
    assert first.revision in status.retained and len(status.retained) == 2
    assert status.retained_bytes == first.retained_bytes + second.retained_bytes
    with pytest.raises(ResourceError, match="still-retained"):
        manager.rollback(first.revision, expected_active=second.revision)
    reap.set()
    retiring.join(10)
    assert not retiring.is_alive() and not errors
    with pytest.raises(ProcessLookupError):
        os.kill(running.pid, 0)
    assert current_session._backend.generation is current_worker
    assert not manager.close().closed
    assert current_lease.validate_shapes(
        prepare_source(data("B")), focus_nodes=(E + "root",)
    ).report.verified
    assert current_session._backend.generation is current_worker
    current_lease.close()
    assert manager.status().closed
    with pytest.raises(ProcessLookupError):
        os.kill(current_worker.pid, 0)
