"""Reconstruct Core RDF and expose the exact omissions of simple entry labels."""

from rdflib import BNode, Dataset, Graph, URIRef
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID
from rdflib.namespace import RDF, RDFS

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.patches.applying import _materialize
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node

from .reading import reserve
from .types import ONTOLEX as O
from .types import (
    LabelProjection,
    LexicalOmission,
    Lexicon,
    LexiconExport,
    Resource,
    Statement,
)


def _budget(limits: Limits | None, budget: Budget | None) -> Budget:
    """Validate caller accounting before reading owned model metadata."""
    if (
        limits is not None
        and not isinstance(limits, Limits)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Lexical export requires Limits and Budget objects")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Lexical export limits conflict with the shared budget")
    return budget if budget is not None else Budget(limits)


def _statements(lexicon: Lexicon, budget: Budget) -> set[Statement]:
    """Render actual immutable Core records, retaining resource and form roles."""
    result: set[Statement] = set()

    def add(triple: Statement) -> None:
        budget.consume("work")
        for node in triple:
            bound_node(node, budget)
        reserve(budget)
        result.add(triple)

    for entry in lexicon.entries:
        for kind in entry.types:
            add((entry.identifier, RDF.type, kind))
        for triple in entry.extensions:
            add(triple)
        for link in entry.forms:
            form = link.form
            add((entry.identifier, link.predicate, form.identifier))
            for kind in form.types:
                add((form.identifier, RDF.type, kind))
            for value in form.written:
                add((form.identifier, O.writtenRep, value))
            for triple in form.extensions:
                add(triple)
        for sense in entry.senses:
            add((entry.identifier, O.sense, sense.identifier))
            for kind in sense.types:
                add((sense.identifier, RDF.type, kind))
            for reference in sense.references:
                add((sense.identifier, O.reference, reference.target))
            for triple in sense.extensions:
                add(triple)
    return result


def _contexts(
    source: PreparedSource, budget: Budget
) -> tuple[tuple[Resource | None, Graph], ...]:
    """Capture every original physical graph, including empty and unselected ones."""
    contexts = []
    for graph in source._owned_contexts(budget):
        reserve(budget)
        name = (
            None if graph.identifier == DATASET_DEFAULT_GRAPH_ID else graph.identifier
        )
        if name is not None and not isinstance(name, URIRef | BNode):
            raise InputError("Lexical graph names require resource terms")
        contexts.append((name, graph))
    return tuple(contexts)


def _fresh(output: Dataset, original: PreparedSource, budget: Budget) -> PreparedSource:
    """Preserve the original pin through established fresh blank correspondence.

    The private carrier retains all reconstructed backing graphs. Existing patch
    materialization separates their anonymous identities in one global mapping;
    an original blank graph selector then resolves to its actual fresh graph.
    """
    selection = original.graph_name
    if selection is not None:
        original._read(budget=budget)
    carrier = PreparedSource._from_owned(
        output,
        is_dataset=original.is_dataset,
        document_iri=original.document_iri,
        effective_url=original.effective_url,
        literal_fidelity=original.literal_fidelity,
        graph_fidelity=original.graph_fidelity,
        max_triples=budget.limits.max_triples,
    )
    captured = capture_source(carrier, "lexicon:", budget)
    bindings: dict[str, BNode] = {}
    result = _materialize(captured.witness, original, budget, bindings=bindings)
    if isinstance(selection, BNode):
        selection = bindings[captured.reference(selection).token]
    result.graph_name = selection
    return result


def write_lexicon(
    lexicon: Lexicon, *, limits: Limits | None = None, budget: Budget | None = None
) -> LexiconExport:
    """Produce fresh RDF from typed Core records plus all retained source assertions.

    The selected modeled statements are reconstructed, rather than exported as a
    label view. Unmodeled assertions, graph inventory and cross-graph blank sharing
    remain intact. A fresh owned snapshot separates exported mutable containers.
    """
    operation = _budget(limits, budget)
    issues = IssueCollector(operation)
    source = None
    complete = False
    children: tuple[Report, ...] = ()
    try:
        if type(lexicon) is not Lexicon:
            raise InputError("Export requires an owned Lexicon")
        children = (lexicon.report,)
        retain_report(lexicon.report, operation, issues)
        if not lexicon.report.verified or lexicon.source is None:
            issues.append(
                Issue(
                    "ontolex.source_unverified",
                    "Incomplete lexical interpretation cannot qualify a Core export",
                    kind="unverified",
                )
            )
        else:
            if lexicon.graph is not None:
                bound_node(lexicon.graph, operation)
            modeled = _statements(lexicon, operation)
            output = Dataset()
            selected = (
                output.default_graph
                if lexicon.graph is None
                else output.graph(lexicon.graph)
            )
            for name, graph in _contexts(lexicon.source, operation):
                target = output.default_graph if name is None else output.graph(name)
                for triple in graph:
                    operation.consume("work")
                    operation.consume("triples")
                    for node in triple:
                        bound_node(node, operation)
                    reserve(operation)
                    if name != lexicon.graph or triple not in modeled:
                        target.add(triple)
            for triple in modeled:
                operation.consume("work")
                operation.consume("triples")
                reserve(operation)
                selected.add(triple)
            source = _fresh(output, lexicon.source, operation)
            complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(Issue("ontolex.input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return LexiconExport(
        source,
        Report(
            "adapter.ontolex.export",
            int(source is not None),
            tuple(issues),
            requested=1,
            complete=complete,
            children=children,
            regime="source-retaining-core-rdf",
        ),
    )


def project_labels(
    lexicon: Lexicon,
    *,
    predicate: URIRef = RDFS.label,
    canonical_only: bool = True,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> LabelProjection:
    """Project literals onto entry IDs with explicit structural loss.

    The source remains available through the returned lexicon. Every original
    quad missing from this one-graph label output is retained as an omission;
    available output never certifies preservation of form or sense structure.
    """
    operation = _budget(limits, budget)
    issues = IssueCollector(operation)
    omitted: list[LexicalOmission] = []
    source = None
    children: tuple[Report, ...] = ()
    complete = False
    try:
        if type(lexicon) is not Lexicon or lexicon.source is None:
            raise InputError("Projection requires an owned source Lexicon")
        if not isinstance(predicate, URIRef) or type(canonical_only) is not bool:
            raise InputError("Select an IRI predicate and explicit form policy")
        bound_node(predicate, operation)
        children = (lexicon.report,)
        retain_report(lexicon.report, operation, issues)
        if not lexicon.report.verified:
            issues.append(
                Issue(
                    "ontolex.source_unverified",
                    "Source lexical coverage is incomplete",
                    kind="unverified",
                )
            )
        else:
            labels = Graph()
            for entry in lexicon.entries:
                for link in entry.forms:
                    operation.consume("work")
                    if canonical_only and link.predicate != O.canonicalForm:
                        continue
                    for written in link.form.written:
                        bound_node(entry.identifier, operation)
                        bound_node(written, operation)
                        operation.consume("triples")
                        reserve(operation)
                        labels.add((entry.identifier, predicate, written))
            for name, graph in _contexts(lexicon.source, operation):
                for triple in graph:
                    operation.consume("work")
                    for node in triple:
                        bound_node(node, operation)
                    if name is not None or triple not in labels:
                        reserve(operation)
                        omitted.append(LexicalOmission(name, triple))
            if omitted:
                issues.append(
                    Issue(
                        "ontolex.label_structure_omitted",
                        "Simple entry labels omit original form, sense, reference "
                        "or other source assertions",
                        kind="unverified",
                    )
                )
            source = prepare_rdf(labels, budget=operation)
            complete = not omitted
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(Issue("ontolex.input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return LabelProjection(
        lexicon,
        source,
        tuple(omitted),
        Report(
            "adapter.ontolex.label_projection",
            int(source is not None),
            tuple(issues),
            requested=1,
            complete=complete,
            children=children,
            regime="explicit-lossy-entry-labels",
        ),
    )
