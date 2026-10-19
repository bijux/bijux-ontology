"""Whole-set native relocation with actual consumer and selected deployment checks."""

from collections.abc import Mapping, Sequence
from itertools import chain
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from rdflib import URIRef

from bijux_ontology._json import object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.workflows.ontology.modules.checking import check_module_wiring
from bijux_ontology.workflows.ontology.modules.types import (
    ModuleContract,
    ModuleProvider,
)

from ..axioms.changes.checking import _losses
from ..axioms.changes.preconditions import source_base
from ..axioms.changes.types import AxiomBase
from ..axioms.codec.decoding import ProjectionDecoder
from ..axioms.types import ProjectedAxiom
from ..backend.owlapi.bundles import qualify_backend
from ..backend.owlapi.execution import invoke_worker, qualify_capability, result_counter
from ..backend.types import OwlProfileBackend
from ..diagnosis.checking import _classes
from ..documents.sources.native import prepare_owl_document, stage_native_documents
from ..documents.types import OwlDocument
from ..entailment.checking import check_entailments
from ..theories.interfaces import ModuleInterfaceChange, ModuleSet, capture_interfaces
from ..theories.observations import logical_report, observe_theory
from ..theories.sources import assemble_theory, reserve
from ..theories.types import TheoryCombination, TheoryComponent, TheoryObservation
from .preparation import (
    CapturedModules,
    capture_consumers,
    capture_modules,
    capture_namespaces,
    encode_changes,
)
from .types import (
    AxiomMove,
    ChangedModule,
    ConsumerModuleCheck,
    ModuleConsumer,
    ModuleImportChange,
    ModuleInline,
    ModuleRefactorCandidate,
    NamespaceTransfer,
)


def _read(path: Path, size: object, operation: Operation) -> bytes:
    """Reserve bounded native output storage before the actual file allocation."""
    length = result_counter(size, operation, operation.limits.max_bytes)
    if path.is_symlink() or not path.is_file():
        raise ResourceError("A native module output is unavailable")
    operation.consume("bytes", length + 1)
    operation.consume("work", length + 1)
    with path.open("rb") as stream:
        raw = stream.read(length + 1)
    if len(raw) != length:
        raise ResourceError("A native module output differs from its size receipt")
    return raw


def _scopes(
    raw: object, inputs: tuple[OwlDocument, ...], operation: Operation
) -> dict[str, tuple[str, ...]]:
    """Use actual OWLAPI closure receipts; no second imports resolver is introduced."""
    decoder = ProjectionDecoder(operation)
    result: dict[str, tuple[str, ...]] = {}
    for value in decoder.records(raw):
        item = object_value(value, location="native module closure")
        index = result_counter(item.get("document"), operation, len(inputs) - 1)
        if index == 0 or inputs[index].effective_url in result:
            raise ResourceError("Native module closure ownership is ambiguous")
        reserve(operation)
        members = []
        seen = set()
        for encoded in decoder.records(item.get("closure")):
            member = result_counter(encoded, operation, len(inputs) - 1)
            if member == 0 or member in seen:
                raise ResourceError(
                    "A module closure includes synthetic or duplicate owners"
                )
            reserve(operation)
            seen.add(member)
            members.append(inputs[member].effective_url)
        if index not in seen:
            raise ResourceError("A native module closure omitted its original root")
        for text in decoder.records(item.get("imports")):
            decoder.iri(text)
        result[inputs[index].effective_url] = tuple(members)
    if len(result) != len(inputs) - 1:
        raise ResourceError("Native module closures omit an original source")
    return result


def _rdf_views(
    raw: object,
    directory: Path,
    inputs: tuple[OwlDocument, ...],
    operation: Operation,
    issues: IssueCollector,
    reports: list[Report],
) -> dict[str, PreparedSource]:
    """Adopt only checked maintained RDF views with exact real loader coverage."""
    item = object_value(raw, location="checked native RDF modules")
    decoder = ProjectionDecoder(operation)
    _losses(item.get("changes"), decoder, issues)
    observed = set()
    for encoded in decoder.records(item.get("loaders")):
        metadata = object_value(encoded, location="native RDF loader metadata")
        index = result_counter(metadata.get("document"), operation, len(inputs) - 1)
        if index in observed or metadata.get("present") is not True:
            raise ResourceError("Native RDF views lack exact loader coverage")
        observed.add(index)
        if result_counter(
            metadata.get("unparsed"), operation, 2**31 - 1
        ) or result_counter(metadata.get("guessed"), operation, 2**31 - 1):
            raise ResourceError("Native RDF views contain unparsed or guessed content")
        result_counter(metadata.get("triples"), operation, 2**31 - 1)
        decoder.text(metadata.get("header"))
    result: dict[str, PreparedSource] = {}
    modules = decoder.records(item.get("modules"))
    complete = item.get("qualified") is True and len(observed) == len(inputs)
    reserve(operation)
    report = Report(
        "owl.module_rdf_views",
        len(observed),
        requested=len(inputs),
        complete=complete,
        regime="owlapi-annotated-structure-roundtrip-and-real-rdf-loader",
    )
    reports.append(report)
    if not complete:
        raise ResourceError("The maintained RDF module views are unqualified")
    seen = set()
    for encoded in modules:
        module = object_value(encoded, location="native RDF module")
        index = result_counter(module.get("document"), operation, len(inputs) - 1)
        if index in seen:
            raise ResourceError("Native RDF modules repeat an original owner")
        seen.add(index)
        if index == 0:
            continue
        raw_bytes = _read(
            directory / f"module-{index}.ttl", module.get("bytes"), operation
        )
        original = inputs[index]
        result[original.effective_url] = prepare_rdf(
            RdfSource(
                raw_bytes,
                format="turtle",
                document_iri=original.document_iri,
                effective_url=original.effective_url,
            ),
            budget=operation,
        )
    if len(seen) != len(inputs):
        raise ResourceError("Native RDF modules omit a source owner")
    return result


def _aliases(
    captured: CapturedModules,
    documents: dict[str, OwlDocument],
    scope: tuple[str, ...],
    operation: Operation,
) -> dict[str, OwlDocument]:
    """Filter the original exact aliases by an actual native closure witness."""
    result = {}
    reserve(operation, len(captured.assembly.imports or {}))
    for alias, original in (captured.assembly.imports or {}).items():
        operation.consume("work", len(scope) + 1)
        if original.effective_url in scope:
            result[alias] = documents[original.effective_url]
    return result


def _consumer_checks(
    backend: OwlProfileBackend,
    captured: CapturedModules,
    documents: dict[str, OwlDocument],
    rdf: dict[str, PreparedSource],
    scopes: dict[str, tuple[str, ...]],
    interfaces: tuple[tuple[str, ModuleContract], ...],
    consumers: tuple[ModuleConsumer, ...],
    after: bool,
    operation: Operation,
    heap_mb: int,
    reports: list[Report],
    issues: IssueCollector,
    checked_consumers: list[ConsumerModuleCheck],
) -> None:
    """Require actual consumer visibility before the selected provider proof."""
    contracts = dict(interfaces)
    names: dict[str, str] = {}
    reserve(operation, len(interfaces))
    for module_identity, contract in interfaces:
        if contract.name in names:
            raise InputError("Module provider contract names must be unique")
        names[contract.name] = module_identity
    views: dict[str, VocabularyView] = {}
    for consumer in consumers:
        if consumer.document not in contracts:
            raise InputError("A selected consumer has no explicit module contract")
        contract = contracts[consumer.document]
        if not contract.requires:
            raise InputError("A selected consumer interface has no required provider")
        scope = scopes[consumer.document]
        aliases = _aliases(captured, documents, scope, operation)
        reserve(operation, 8)
        checked = check_entailments(
            backend,
            documents[consumer.document],
            consumer.entailments,
            imports=aliases,
            budget=operation,
            heap_mb=heap_mb,
        )
        reports.append(checked.report)
        checked_consumers.append(
            ConsumerModuleCheck(
                consumer,
                "after" if after else "before",
                checked,
                None,
            )
        )
        retain_report(checked.report, operation, issues)
        bindings = consumer.after_bindings if after else consumer.before_bindings
        providers = []
        selected = set()
        for binding in bindings:
            bound_text(binding.provider, operation)
            identity = names.get(binding.provider)
            if identity is None or identity in selected:
                continue
            reserve(operation)
            selected.add(identity)
            if identity not in scope:
                issues.append(
                    Issue(
                        "owl.module_provider_unreachable",
                        "The bound provider is outside the actual consumer "
                        "import closure",
                        consumer.document,
                        actual=identity,
                    )
                )
                continue
            if identity not in views:
                provider_aliases = _aliases(
                    captured, documents, scopes[identity], operation
                )
                reserve(operation, len(provider_aliases) + 1)
                imported = {
                    alias: RdfSource(
                        rdf[value.effective_url]._read(budget=operation),
                        document_iri=value.document_iri,
                        effective_url=value.effective_url,
                    )
                    for alias, value in provider_aliases.items()
                }
                vocabulary = load_vocabulary(
                    rdf[identity], imports=imported, budget=operation
                )
                views[identity] = VocabularyView(vocabulary, budget=operation)
            providers.append(ModuleProvider(contracts[identity], views[identity]))
        reserve(operation, 4)
        wiring = check_module_wiring(
            contract, tuple(providers), bindings, budget=operation
        )
        reports.append(wiring.report)
        checked_consumers[-1] = ConsumerModuleCheck(
            consumer,
            "after" if after else "before",
            checked,
            wiring,
        )
        retain_report(wiring.report, operation, issues)


def _deployment(
    deployment: TheoryCombination,
    captured: CapturedModules,
    documents: dict[str, OwlDocument],
    scopes: dict[str, tuple[str, ...]],
    consumers: tuple[ModuleConsumer, ...],
    classes: tuple[URIRef, ...],
    backend: OwlProfileBackend,
    operation: Operation,
    heap_mb: int,
    observations: list[TheoryObservation],
    reports: list[Report],
    issues: IssueCollector,
) -> None:
    """Check the declared deployment, excluding unrelated supplied module roots."""
    labels = dict(captured.labels)
    selected = (*deployment.components, *deployment.bridges)
    reserve(operation, len(selected) + 4)
    chosen = []
    closure: set[str] = set()
    for label in selected:
        if label not in labels:
            raise InputError("A deployment selects an unknown original component")
        identity = labels[label]
        closure.update(scopes[identity])
        chosen.append(
            TheoryComponent(
                label,
                documents[identity],
                _aliases(captured, documents, scopes[identity], operation),
            )
        )
    if any(consumer.document not in closure for consumer in consumers):
        raise ResourceError(
            "The selected deployment does not include every required consumer"
        )
    assembled = assemble_theory(deployment.label, tuple(chosen), operation)
    obligations = tuple(
        axiom for consumer in consumers for axiom in consumer.entailments
    )
    reserve(operation, len(obligations) + 8)
    observed = observe_theory(
        backend, assembled, classes, obligations, operation, heap_mb, observations
    )
    reports.append(observed.report)
    retain_report(observed.report, operation, issues)
    logical = logical_report(observed, operation)
    reports.append(logical)
    retain_report(logical, operation, issues)


def prepare_module_refactor(
    backend: OwlProfileBackend,
    components: Sequence[TheoryComponent],
    changes: Sequence[AxiomMove | ModuleInline],
    *,
    expected: Mapping[str, AxiomBase],
    owned: Sequence[str],
    deployment: TheoryCombination,
    interfaces: Mapping[str, ModuleContract],
    consumers: Sequence[ModuleConsumer],
    imports: Sequence[ModuleImportChange] = (),
    protected: Mapping[str, Sequence[ProjectedAxiom]] | None = None,
    interface_changes: Sequence[ModuleInterfaceChange] = (),
    namespace_transfers: Sequence[NamespaceTransfer] = (),
    named_classes: Sequence[URIRef] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> ModuleRefactorCandidate:
    """Prepare all owned module candidates together, without installing any output.

    The exact full source-set base, annotation-bearing ownership and approved
    import changes precede every edit. Actual consumer closures and unchanged
    finite module interfaces remain separate from the selected joint theory.
    Legal import cycles are interpreted by OWLAPI, never rejected as a policy.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    before: list[Report] = []
    after: list[Report] = []
    observations: list[TheoryObservation] = []
    checked_consumers: list[ConsumerModuleCheck] = []
    selected_deployment = None
    original_interfaces: tuple[tuple[str, ModuleContract], ...] = ()
    updated_interfaces: tuple[tuple[str, ModuleContract], ...] = ()
    transfers: tuple[NamespaceTransfer, ...] = ()
    modules: tuple[ChangedModule, ...] = ()
    candidate = None
    capability = None
    complete = False
    try:
        captured = capture_modules(components, expected, owned, operation)
        if not captured.bases_match:
            issues.append(
                Issue(
                    "owl.module_base_mismatch",
                    "The exact module source-set base differs",
                )
            )
        else:
            identities = {value.effective_url for value in captured.originals}
            selected_consumers = capture_consumers(consumers, identities, operation)
            original_interfaces, updated_interfaces = capture_interfaces(
                interfaces,
                interface_changes,
                identities,
                set(captured.owned),
                operation,
            )
            initial = dict(original_interfaces)
            revised = dict(updated_interfaces)
            for consumer in selected_consumers:
                if consumer.document not in initial or consumer.document not in revised:
                    raise InputError(
                        "Every required consumer needs an unchanged interface"
                    )
                if (
                    initial[consumer.document].requires
                    != revised[consumer.document].requires
                ):
                    raise InputError(
                        "Module relocation cannot weaken required consumer obligations"
                    )
            if type(deployment) is not TheoryCombination:
                raise InputError("Select one explicit TheoryCombination deployment")
            if type(deployment.label) is not str:
                raise InputError("Deployment labels require ordinary strings")
            bound_text(deployment.label, operation)
            roots = capture_items(deployment.components, str, operation)
            bridges = capture_items(deployment.bridges, str, operation)
            for label in (*roots, *bridges):
                bound_text(label, operation)
            if not roots or len({*roots, *bridges}) != len(roots) + len(bridges):
                raise InputError("Select distinct deployment roots and bridges")
            selected_deployment = TheoryCombination(deployment.label, roots, bridges)
            classes = _classes(named_classes, operation)
            assert classes is not None
            qualify_backend(backend, operation)
            backend.artifacts.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(
                prefix="module-refactoring-", dir=backend.artifacts
            ) as job:
                directory = Path(job)
                inputs, request = stage_native_documents(
                    captured.assembly.source,
                    captured.assembly.imports,
                    directory,
                    operation,
                )
                indices = {
                    source.effective_url: index for index, source in enumerate(inputs)
                }
                wire, import_wire, guards, pairs = encode_changes(
                    changes,
                    imports,
                    {} if protected is None else protected,
                    indices,
                    captured.owned,
                    operation,
                )
                transfers = capture_namespaces(namespace_transfers, pairs, operation)
                allowance = operation.remaining("states")
                if not allowance or not operation.remaining("issues"):
                    raise LimitExceeded(
                        "Module changes have no remaining native inventory allowance"
                    )
                request.update(
                    operation="module_changes",
                    owned=[indices[value] for value in captured.owned],
                    moves=wire,
                    imports=import_wire,
                    protected=guards,
                    profiles=[],
                    max_bytes=operation.limits.max_bytes,
                    max_nodes=allowance,
                    max_issues=operation.remaining("issues"),
                    max_resources=operation.limits.max_resources,
                    projection_depth=max(1, min(64, operation.limits.max_depth // 4)),
                )
                data = invoke_worker(backend, request, directory, operation, heap_mb)
                capability = qualify_capability(data, backend, operation)
                before.append(capability.report)
                decoder = ProjectionDecoder(operation)
                _losses(data.get("changes"), decoder, issues)
                if data.get("failure") is not None:
                    issues.append(
                        Issue(
                            "owl.module_change_precondition",
                            "An owned module change precondition failed",
                            actual=decoder.text(data["failure"]),
                        )
                    )
                elif data.get("qualified") is not True:
                    if data.get("status") == "exhausted":
                        raise LimitExceeded(
                            "Native module construction exhausted its allowance"
                        )
                    raise ResourceError("Native module construction is unqualified")
                else:
                    before_scopes = _scopes(
                        data.get("before_scopes"), inputs, operation
                    )
                    after_scopes = _scopes(data.get("after_scopes"), inputs, operation)
                    before_rdf = _rdf_views(
                        data.get("before_views"),
                        directory / "before-rdf",
                        inputs,
                        operation,
                        issues,
                        before,
                    )
                    after_rdf = _rdf_views(
                        data.get("after_views"),
                        directory / "after-rdf",
                        inputs,
                        operation,
                        issues,
                        after,
                    )
                    originals = {
                        source.effective_url: source for source in captured.originals
                    }
                    revised_sources = dict(originals)
                    native_modules = {}
                    for value in decoder.records(data.get("modules")):
                        module = object_value(
                            value, location="checked module candidate"
                        )
                        index = result_counter(
                            module.get("document"), operation, len(inputs) - 1
                        )
                        identity = inputs[index].effective_url
                        if identity not in captured.owned or identity in native_modules:
                            raise ResourceError(
                                "Native candidate ownership differs from selected "
                                "modules"
                            )
                        reserve(operation, 4)
                        raw = _read(
                            directory / f"candidate-{index}.owl",
                            module.get("bytes"),
                            operation,
                        )
                        source = inputs[index]
                        revised_sources[identity] = prepare_owl_document(
                            raw,
                            format="functional",
                            document_iri=source.document_iri,
                            effective_url=source.effective_url,
                            budget=operation,
                        )
                        native_modules[identity] = module
                    if set(native_modules) != captured.owned:
                        raise ResourceError("Native candidates omit an owned module")
                    edits = {}
                    for value in decoder.records(data.get("edits")):
                        edit = object_value(value, location="native module delta")
                        index = result_counter(
                            edit.get("document"), operation, len(inputs) - 1
                        )
                        identity = inputs[index].effective_url
                        if identity not in captured.owned or identity in edits:
                            raise ResourceError("Native edits have ambiguous owners")
                        additions = tuple(
                            decoder.text(text)
                            for text in decoder.records(edit.get("additions"))
                        )
                        removals = tuple(
                            decoder.text(text)
                            for text in decoder.records(edit.get("removals"))
                        )
                        reserve(operation)
                        edits[identity] = ChangedModule(
                            originals[identity],
                            revised_sources[identity],
                            source_base(originals[identity]),
                            additions,
                            removals,
                        )
                    if set(edits) != captured.owned:
                        raise ResourceError("Native edits omit an owned module")
                    _consumer_checks(
                        backend,
                        captured,
                        originals,
                        before_rdf,
                        before_scopes,
                        original_interfaces,
                        selected_consumers,
                        False,
                        operation,
                        heap_mb,
                        before,
                        issues,
                        checked_consumers,
                    )
                    _deployment(
                        selected_deployment,
                        captured,
                        originals,
                        before_scopes,
                        selected_consumers,
                        classes,
                        backend,
                        operation,
                        heap_mb,
                        observations,
                        before,
                        issues,
                    )
                    _consumer_checks(
                        backend,
                        captured,
                        revised_sources,
                        after_rdf,
                        after_scopes,
                        updated_interfaces,
                        selected_consumers,
                        True,
                        operation,
                        heap_mb,
                        after,
                        issues,
                        checked_consumers,
                    )
                    _deployment(
                        selected_deployment,
                        captured,
                        revised_sources,
                        after_scopes,
                        selected_consumers,
                        classes,
                        backend,
                        operation,
                        heap_mb,
                        observations,
                        after,
                        issues,
                    )
                    complete = all(report.complete for report in (*before, *after))
                    if (
                        complete
                        and not issues
                        and all(report.verified for report in (*before, *after))
                    ):
                        reserve(operation, len(edits) + len(revised_sources) + 4)
                        modules = tuple(edits.values())
                        candidate = ModuleSet(
                            tuple(revised_sources.values()), updated_interfaces
                        )
    except LimitExceeded as error:
        complete = False
        for report in (*before, *after):
            issues.retain_completed(report)
        issues.exhausted(str(error), location="owl.module_refactoring")
    except (InputError, OSError) as error:
        complete = False
        try:
            unknown = isinstance(error, ResourceError | OSError)
            issues.append(
                Issue(
                    "owl.module_refactoring_unverified"
                    if unknown
                    else "owl.module_refactoring_input_error",
                    str(error),
                    kind="unverified" if unknown else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped), location="owl.module_refactoring")
    # The native observation is retained before its later entailment stage.
    # Terminal handoff must include paid truth from that incomplete observation.
    for completed in chain(before, after):
        issues.retain_completed(completed)
    for observation in observations:
        issues.retain_completed(observation.consistency.report)
        issues.retain_completed(observation.consistency.consistency_report)
        if observation.entailments is not None:
            issues.retain_completed(observation.entailments.report)
    report = Report(
        "owl.module_refactoring",
        int(candidate is not None),
        tuple(issues),
        requested=1,
        complete=complete,
        children=(*before, *after),
        regime="exact-owned-axioms-and-actual-consumer-deployment-obligations",
    )
    return ModuleRefactorCandidate(
        candidate,
        modules,
        original_interfaces,
        updated_interfaces,
        transfers,
        tuple(before),
        tuple(after),
        tuple(observations),
        capability,
        tuple(checked_consumers),
        selected_deployment,
        report,
    )
