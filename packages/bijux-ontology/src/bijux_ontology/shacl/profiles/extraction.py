"""Bounded selected Core shape closure without invented constraints or targets."""

import threading
from collections import deque
from collections.abc import Sequence
from typing import cast

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SH, XSD
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.rdf.terms import bound_node, bound_text, literal_datatype
from bijux_ontology.reports import Issue, Report
from bijux_ontology.shacl.execution.planning import _select_roots
from bijux_ontology.shacl.paths import decode_path

from .resources import _capture, _owned
from .types import ProfileCase, ShapeDependency, ShapeProfile, ShapeTerm, Triple

_SHAPE_REFS = frozenset((SH.node, SH.property, SH["not"], SH.qualifiedValueShape))
_LOGICAL = frozenset((SH["and"], SH["or"], SH.xone))
_LISTS = frozenset((SH["in"], SH.languageIn, SH.ignoredProperties))
_PATHS = frozenset(
    (SH.inversePath, SH.zeroOrMorePath, SH.oneOrMorePath, SH.zeroOrOnePath)
)
_TARGETS = frozenset(
    (SH.targetClass, SH.targetNode, SH.targetSubjectsOf, SH.targetObjectsOf)
)
_DYNAMIC = frozenset(
    (
        SH.sparql,
        SH.select,
        SH.ask,
        SH.rule,
        SH.js,
        SH.jsFunctionName,
        SH.jsLibrary,
        SH.jsLibraryURL,
        SH.expression,
        SH.target,
        SH.validator,
        SH.nodeValidator,
        SH.propertyValidator,
        OWL.imports,
    )
)


def extract_profile(
    shapes: PreparedSource,
    roots: Sequence[str | URIRef | BNode],
    *,
    ontology: PreparedSource | None = None,
    cases: Sequence[ProfileCase] = (),
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> ShapeProfile:
    """Extract explicit dependencies and qualify only the supplied selected cases.

    Core shape references, logical lists, path/list structure, prefix declarations
    and qualified-disjoint sibling contexts are retained. Independent targets
    are removed just as in the existing selected-root validator. Dynamic query,
    import and custom component obligations stay unknown. No inference or remote
    lookup is attempted. A structural artifact remains inspectable independently
    of optional backend availability and requested behavior coverage.
    """
    from .comparison import compare_profile

    if (
        not isinstance(shapes, PreparedSource)
        or ontology is not None
        and not isinstance(ontology, PreparedSource)
    ):
        raise InputError("Shape profiles require prepared shape/ontology resources")
    if cancel is not None and not isinstance(cancel, threading.Event):
        raise InputError("Shape profile cancellation requires threading.Event")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    selected: tuple[ShapeTerm, ...] = ()
    dependencies: list[ShapeDependency] = []
    source = owned_ontology = None
    authored = shapes
    cases_owned: tuple[ProfileCase, ...] = ()
    checked = 0
    try:
        for name in (graph_name, shapes.graph_name):
            if name is not None:
                bound_node(name, operation) if isinstance(
                    name, BNode | URIRef
                ) else bound_text(name, operation)
        if shapes.union or shapes.literal_fidelity not in (
            "source_terms",
            "supplied_terms",
        ):
            raise InputError("Profile extraction needs exact authored graph terms")
        if isinstance(graph_name, BNode) and not any(
            node is graph_name for node in shapes._graph_names(budget=operation)
        ):
            raise InputError("Blank graph selection lacks exact snapshot ownership")
        graph = shapes._read(graph_name, budget=operation)
        all_triples: list[Triple] = []
        owned_blanks: set[int] = set()
        for triple in graph:
            operation.consume("work")
            operation.consume("states")
            operation.consume("triples")
            for node in triple:
                bound_node(node, operation)
                if isinstance(node, BNode):
                    owned_blanks.add(id(node))
            all_triples.append(triple)
            if triple[1] == OWL.imports:
                issues.append(
                    Issue(
                        "shacl.profile_dependency_unverified",
                        "Authored shape imports are outside the explicit local closure",
                        kind="unverified",
                    )
                )
        requested = _capture(roots, operation)
        chosen: list[ShapeTerm] = []
        for root in requested:
            if isinstance(root, BNode):
                bound_node(root, operation)
                if id(root) not in owned_blanks:
                    raise InputError(
                        "Selected blank shape is not owned by the supplied graph"
                    )
                chosen_root: ShapeTerm = root
            elif isinstance(root, str):
                bound_text(root, operation)
                chosen_root = URIRef(absolute_iri(root))
            else:
                raise InputError("Shape roots require IRI or owned blank terms")
            operation.consume("work", len(chosen) + 1)
            if chosen_root not in chosen:
                operation.consume("states")
                chosen.append(chosen_root)
        selected = tuple(chosen)
        if not selected:
            raise InputError("Shape profiles require explicit nonempty roots")
        cases_owned = cast(tuple[ProfileCase, ...], _capture(cases, operation))
        if any(not isinstance(case, ProfileCase) for case in cases_owned):
            raise InputError("Shape profiles require typed ProfileCase samples")
        names: set[str] = set()
        for case in cases_owned:
            bound_text(case.name, operation)
            if case.name in names:
                raise InputError("Shape profile case names must be distinct")
            names.add(case.name)
        authored = shapes
        if graph_name is not None and shapes.graph_name is None:
            authored = _owned(
                shapes, graph, all_triples, operation, selection=graph_name
            )
        for prefix, namespace in graph.namespaces():
            operation.consume("work")
            operation.consume("states")
            bound_text(prefix, operation)
            bound_node(namespace, operation)
        operation.consume("work", 8 * len(all_triples))
        operation.consume("states", 3 * len(all_triples))
        operation.consume("triples", len(all_triples))
        graph = _select_roots(graph, selected, "targets")
        custom_parameters: set[Node] = set()
        for component in graph.subjects(RDF.type, SH.ConstraintComponent):
            operation.consume("work")
            for parameter in graph.objects(component, SH.parameter):
                operation.consume("work")
                for path in graph.objects(parameter, SH.path):
                    operation.consume("work")
                    operation.consume("states")
                    custom_parameters.add(path)
        pending: deque[tuple[Node, str]] = deque((root, "shape") for root in selected)
        seen: set[tuple[Node, str]] = set()
        retained: set[Triple] = set()
        while pending:
            node, role = pending.popleft()
            operation.consume("work")
            if not isinstance(node, URIRef | BNode):
                raise InputError("Profile dependency must be a resource")
            if (node, role) in seen:
                continue
            operation.consume("states")
            seen.add((node, role))
            outgoing = []
            for triple in graph.triples((node, None, None)):
                operation.consume("work")
                operation.consume("states")
                outgoing.append(triple)
            if not outgoing and node != RDF.nil:
                issues.append(
                    Issue(
                        "shacl.profile_dependency_unverified",
                        "An authored dependency definition is missing",
                        kind="unverified",
                    )
                )
            qualified_disjoint = False
            for subject, predicate, value in outgoing:
                triple = subject, predicate, value
                if role == "shape" and predicate in _TARGETS and node not in selected:
                    continue
                operation.consume("states")
                retained.add(triple)
                if role == "shape" and predicate == SH.qualifiedValueShapesDisjoint:
                    if (
                        not isinstance(value, Literal)
                        or literal_datatype(value) != XSD.boolean
                        or not check_literal(value, budget=operation).verified
                    ):
                        raise InputError(
                            "Qualified disjointness requires a valid boolean"
                        )
                    qualified_disjoint = qualified_disjoint or str(value) in (
                        "true",
                        "1",
                    )
                if predicate in _DYNAMIC or predicate in custom_parameters:
                    issues.append(
                        Issue(
                            "shacl.profile_dependency_unverified",
                            "A dynamic dependency is outside finite Core extraction",
                            kind="unverified",
                        )
                    )
                if role == "shape" and predicate in _SHAPE_REFS:
                    if not isinstance(value, URIRef | BNode):
                        raise InputError("Shape dependency requires an RDF resource")
                    operation.consume("states")
                    operation.consume("bytes", 128)
                    dependencies.append(
                        ShapeDependency(node, cast(URIRef, predicate), value, (triple,))
                    )
                    pending.append((value, "shape"))
                elif role == "shape" and predicate in _LOGICAL:
                    members = tuple(
                        iter_rdf_list(
                            graph,
                            value,
                            max_items=operation.limits.max_states,
                            budget=operation,
                        )
                    )
                    witness = [triple]
                    cell = value
                    for member in members:
                        first = cell, RDF.first, member
                        rest = next(graph.objects(cell, RDF.rest))
                        witness.extend((first, (cell, RDF.rest, rest)))
                        operation.consume("states", len(witness))
                        operation.consume("bytes", 64 + 24 * len(witness))
                        if not isinstance(member, URIRef | BNode):
                            raise InputError(
                                "Logical shape lists need resource members"
                            )
                        dependencies.append(
                            ShapeDependency(
                                node, cast(URIRef, predicate), member, tuple(witness)
                            )
                        )
                        pending.append((member, "shape"))
                        cell = rest
                    pending.append((value, "list"))
                elif predicate == SH.path or role == "path" and predicate in _PATHS:
                    if isinstance(value, BNode):
                        pending.append((value, "path"))
                elif predicate == SH.alternativePath:
                    pending.append((value, "path-list"))
                elif role in ("path", "path-list", "list") and predicate == RDF.rest:
                    if value != RDF.nil:
                        pending.append((value, role))
                elif (
                    role in ("path", "path-list")
                    and predicate == RDF.first
                    and isinstance(value, BNode)
                ):
                    pending.append((value, "path"))
                elif predicate in _LISTS:
                    tuple(
                        iter_rdf_list(
                            graph,
                            value,
                            max_items=operation.limits.max_states,
                            budget=operation,
                        )
                    )
                    pending.append((value, "list"))
                elif (
                    predicate == SH.prefixes
                    or role == "prefix"
                    and predicate == SH.declare
                ):
                    pending.append((value, "prefix"))
            if qualified_disjoint:
                for parent in graph.subjects(SH.property, node):
                    operation.consume("work")
                    operation.consume("states")
                    pending.append((parent, "shape"))
            checked += 1
        # Referenced class declarations must not become new implicit root targets.
        implicit = {RDFS.Class, OWL.Class}
        for triple in tuple(retained):
            operation.consume("work")
            if (
                triple[1] == RDF.type
                and triple[2] in implicit
                and triple[0] not in selected
            ):
                retained.remove(triple)
        source = _owned(shapes, graph, tuple(retained), operation, selection=graph_name)
        for _, predicate, path_node in retained:
            operation.consume("work")
            if predicate == SH.path:
                decoded = decode_path(
                    graph, path_node, budget=operation, raise_limits=True
                )
                if decoded.limitation is not None:
                    issues.append(
                        Issue(
                            "shacl.profile_dependency_unverified",
                            decoded.limitation,
                            kind="unverified",
                        )
                    )
        if ontology is not None:
            operation.consume("resources")
            og = ontology._read(budget=operation)
            ontology_triples: list[Triple] = []
            for triple in og:
                operation.consume("work")
                operation.consume("states")
                for term in triple:
                    bound_node(term, operation)
                if triple[1] == OWL.imports:
                    issues.append(
                        Issue(
                            "shacl.profile_dependency_unverified",
                            "Ontology imports need an explicit "
                            "closed supplied resource",
                            kind="unverified",
                        )
                    )
                ontology_triples.append(triple)
            owned_ontology = _owned(ontology, og, ontology_triples, operation)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "shacl.profile_dependency_unverified", str(error), kind="unverified"
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    structural = Report(
        "shacl-profile-structure",
        checked,
        tuple(issues),
        requested=max(1, checked),
        regime="finite-authored-shape-closure",
    )
    profile = ShapeProfile(
        source,
        owned_ontology,
        selected,
        tuple(dependencies),
        structural,
        (),
        structural,
    )
    return compare_profile(authored, profile, cases_owned, budget=operation)
