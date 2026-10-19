"""Project supported path fields through the shared typed SHACL constructors."""

from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from rdflib import BNode, URIRef
from rdflib.namespace import RDF

from bijux_ontology.declarations.path_fields import PathFieldDeclaration
from bijux_ontology.declarations.types import FieldDeclaration, ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.path_traversal import finite_path_steps
from bijux_ontology.rdf.paths import RdfPath, capture_path
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.authoring.compiling import compile_shapes
from bijux_ontology.shacl.authoring.types import (
    ShaclConstraint,
    ShaclCount,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclShapeRef,
    ShaclValue,
)
from bijux_ontology.shacl.model_shapes.translation import compile_model_shapes
from bijux_ontology.shacl.model_shapes.types import ModelShapeBinding

from .types import PathFieldProjection

_ENDPOINT = URIRef("urn:bijux:output:path-endpoint")


def project_path_field(
    field: PathFieldDeclaration,
    subjects: Sequence[URIRef],
    *,
    shape: URIRef,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> PathFieldProjection:
    """Compile named subject targets and the supported logical field constraints.

    Endpoint rules reuse model translation; the placeholder property is replaced
    only in typed authoring records before final compilation. Original model
    translation limitations remain required. Intermediate prefixes can constrain
    counts, exact types, resource identity and exclusive ownership. A generic
    outgoing-description rule without declared types remains unverified. Validate
    in the same effective graph as native checking; no graph is silently joined.
    """
    if not isinstance(field, PathFieldDeclaration) or not isinstance(shape, URIRef):
        raise InputError("Expected a path field and an explicit named shape IRI")
    if not isinstance(subjects, Sequence) or isinstance(
        cast(object, subjects), str | bytes
    ):
        raise InputError("Path shape targets require a finite sequence")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Path projection limits conflict with the operation budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    children = []
    output = None
    finished = True
    checked = 0
    anchor = ""
    try:
        bound_text(field.name, operation)
        anchor = field.name
        bound_node(shape, operation)
        captured = capture_path(field.path, budget=operation)
        steps = finite_path_steps(captured, operation)
        if steps is None:
            issues.append(
                Issue(
                    "shacl.path_field_unverified",
                    "Path projection requires finite sequence and inverse steps",
                    anchor,
                    kind="unverified",
                )
            )
        else:
            count = len(subjects)
            operation.consume("states", count)
            targets = []
            for index in range(count):
                operation.consume("work")
                subject = subjects[index]
                if not isinstance(subject, URIRef):
                    raise InputError("Path shape targets require explicit named IRIs")
                bound_node(subject, operation)
                targets.append(subject)
            model = ModelDeclaration(
                "path-field",
                (),
                (
                    FieldDeclaration(
                        field.name, str(_ENDPOINT), field.values, output=field.output
                    ),
                ),
            )
            translated = compile_model_shapes(
                (model,),
                (
                    ModelShapeBinding(
                        model.identifier, shape, focus_nodes=tuple(targets)
                    ),
                ),
                budget=operation,
            )
            issues.extend(translated.report.issues)
            children.append(translated.report)
            if translated.source is not None:
                definitions: list[ShaclNodeShape | ShaclPropertyShape] = []
                operation.consume("states", len(translated.definitions))
                for definition in translated.definitions:
                    operation.consume("work")
                    if (
                        isinstance(definition, ShaclPropertyShape)
                        and isinstance(definition.path, RdfPath)
                        and definition.path.kind == "predicate"
                        and definition.path.node == _ENDPOINT
                    ):
                        definition = replace(definition, path=captured)
                    definitions.append(definition)

                def identifier() -> BNode:
                    """Reserve every additional owned definition identity."""
                    operation.consume("states")
                    operation.consume("bytes", 64)
                    return BNode()

                def retain(value: ShaclNodeShape | ShaclPropertyShape) -> ShaclShapeRef:
                    """Retain one shared definition and return its RDF reference."""
                    operation.consume("states")
                    definitions.append(value)
                    return ShaclShapeRef(value.identifier)

                references = []
                operation.consume("states", len(field.intermediates))
                for requirement in field.intermediates:
                    operation.consume("work")
                    if requirement.step >= len(steps):
                        raise InputError(
                            "Intermediate requirements must precede the endpoint"
                        )
                    properties = []
                    if requirement.types:
                        types = []
                        for iri in requirement.types:
                            bound_text(iri, operation)
                            operation.consume("states")
                            types.append(ShaclValue(has_value=URIRef(iri)))
                        properties.append(
                            retain(
                                ShaclPropertyShape(identifier(), RDF.type, tuple(types))
                            )
                        )
                    elif requirement.described:
                        issues.append(
                            Issue(
                                "shacl.path_field_unverified",
                                "Description requires the native record check",
                                anchor,
                                kind="unverified",
                            )
                        )
                    rules: list[ShaclConstraint] = [
                        ShaclValue(
                            node_kind="blank_or_iri"
                            if requirement.identity.allow_blank
                            else "iri"
                        )
                    ]
                    if requirement.identity.expected_iri is not None:
                        bound_text(requirement.identity.expected_iri, operation)
                        rules.append(
                            ShaclValue(
                                has_value=URIRef(requirement.identity.expected_iri)
                            )
                        )
                    if requirement.ownership == "exclusive":
                        predicate, inverse = steps[requirement.step - 1]
                        owner_path = (
                            predicate
                            if inverse
                            else RdfPath(
                                "inverse",
                                identifier(),
                                (RdfPath("predicate", predicate),),
                            )
                        )
                        properties.append(
                            retain(
                                ShaclPropertyShape(
                                    identifier(), owner_path, (ShaclCount(1, 1),)
                                )
                            )
                        )
                    node_ref = retain(
                        ShaclNodeShape(identifier(), tuple(properties), tuple(rules))
                    )
                    prefix = []
                    operation.consume("states", requirement.step)
                    for predicate, inverse in steps[: requirement.step]:
                        item = RdfPath("predicate", predicate)
                        prefix.append(
                            RdfPath("inverse", identifier(), (item,))
                            if inverse
                            else item
                        )
                    path = (
                        prefix[0]
                        if len(prefix) == 1
                        else RdfPath("sequence", identifier(), tuple(prefix))
                    )
                    constraints: list[ShaclConstraint] = [node_ref]
                    if (
                        requirement.minimum is not None
                        or requirement.maximum is not None
                    ):
                        constraints.append(
                            ShaclCount(requirement.minimum, requirement.maximum)
                        )
                    references.append(
                        retain(
                            ShaclPropertyShape(identifier(), path, tuple(constraints))
                        )
                    )
                for index, definition in enumerate(definitions):
                    operation.consume("work")
                    if (
                        isinstance(definition, ShaclNodeShape)
                        and definition.identifier == shape
                    ):
                        operation.consume(
                            "states", len(definition.properties) + len(references)
                        )
                        definitions[index] = replace(
                            definition, properties=(*definition.properties, *references)
                        )
                        break
                output = compile_shapes(tuple(definitions), budget=operation)
                checked = 1
    except LimitExceeded as error:
        issues.exhausted(str(error), location=anchor)
        finished = False
    report = Report(
        "path-field-shapes",
        checked,
        tuple(issues),
        children=tuple(children),
        requested=1,
        complete=finished,
        regime="explicit-shacl-core-projection",
        families=("path-field",),
    )
    return PathFieldProjection(report, output)
