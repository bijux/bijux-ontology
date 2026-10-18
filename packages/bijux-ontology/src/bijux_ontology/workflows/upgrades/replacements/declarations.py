"""
Own declaration literals and rewrite explicit semantic slots without string replacement.
"""

from dataclasses import replace

from rdflib import Literal, URIRef

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.types import ModelDeclaration, TermDeclaration
from bijux_ontology.errors import LimitExceeded
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.roles.classification import declaration_role
from bijux_ontology.rdf.roles.types import TermRole
from bijux_ontology.rdf.terms import bound_node, bound_text, literal_term
from bijux_ontology.workflows.upgrades.applications.preparation import _bound_model
from bijux_ontology.workflows.upgrades.uses import Use, UseIndex

from .selection import Selection


def _literal(term: Literal, budget: Budget) -> Literal:
    """
    Preserve original lexical metadata while owning RDFLib's mutable literal object.
    """
    bound_node(term, budget)
    return literal_term(str(term), datatype=term.datatype, language=term.language)


def own_models(
    models: tuple[ModelDeclaration, ...], budget: Budget
) -> tuple[ModelDeclaration, ...]:
    """Snapshot declaration-owned mutable RDF terms before invoking caller selectors."""
    result = []
    for model in models:
        _bound_model(model, budget)
        budget.consume("bytes", 96 * (1 + len(model.fields)))
        fields = []
        for field in model.fields:
            if not field.location:
                size = len(model.identifier) + 1 + len(field.name)
                if size > budget.limits.max_bytes:
                    raise LimitExceeded("Replacement field location exceeds max_bytes")
                budget.consume("work", size)
                size = (
                    len(model.identifier.encode("utf-8"))
                    + 1
                    + len(field.name.encode("utf-8"))
                )
                if size > budget.limits.max_bytes:
                    raise LimitExceeded("Replacement field location exceeds max_bytes")
                budget.consume("bytes", size)
            budget.consume("bytes", 128 * len(field.values))
            branches = []
            for branch in field.values:
                budget.consume("states", len(branch.samples))
                samples = tuple(_literal(sample, budget) for sample in branch.samples)
                constraints = branch.constraints
                if constraints is not None:
                    for text in (constraints.pattern, *constraints.iri_namespaces):
                        if text is not None:
                            bound_text(text, budget)
                    for number in (constraints.minimum, constraints.maximum):
                        if number is not None:
                            size = number.__sizeof__()
                            if size > budget.limits.max_bytes:
                                raise LimitExceeded(
                                    "Replacement numeric bound exceeds max_bytes"
                                )
                            budget.consume("bytes", size)
                    enum = constraints.enumeration
                    if enum is not None:

                        def owned(
                            values: tuple[URIRef | Literal, ...],
                        ) -> tuple[URIRef | Literal, ...]:
                            """
                            Bound each original membership before retaining its exact
                            owned term.
                            """
                            budget.consume("states", len(values))
                            values_out = []
                            for term in values:
                                bound_node(term, budget)
                                values_out.append(
                                    _literal(term, budget)
                                    if isinstance(term, Literal)
                                    else term
                                )
                            return tuple(values_out)

                        enum = (
                            replace(
                                enum,
                                values=owned(enum.values),
                                excluded=owned(enum.excluded),
                            )
                            if isinstance(enum, ValueEnumeration)
                            else owned(enum)
                        )
                        constraints = replace(constraints, enumeration=enum)
                role = branch.role
                if role is not None:
                    for text in (role.kind, role.type_mode, role.punning, *role.types):
                        bound_text(text, budget)
                    if role.bridge is not None:
                        bound_text(role.bridge.predicate, budget)
                        bound_text(role.bridge.source_role, budget)
                branches.append(
                    replace(branch, samples=samples, constraints=constraints)
                )
            fields.append(replace(field, values=tuple(branches)))
        groups = []
        for group in model.emission_groups:
            scenarios = []
            for scenario in group.scenarios:
                condition = scenario.condition
                if condition is not None and isinstance(condition.equals, Literal):
                    condition = replace(
                        condition, equals=_literal(condition.equals, budget)
                    )
                scenarios.append(replace(scenario, condition=condition))
            groups.append(replace(group, scenarios=tuple(scenarios)))
        budget.consume("bytes", 128)
        result.append(
            replace(model, fields=tuple(fields), emission_groups=tuple(groups))
        )
    return tuple(result)


def rewrite_declarations(
    terms: tuple[TermDeclaration, ...],
    models: tuple[ModelDeclaration, ...],
    index: UseIndex,
    selection: Selection,
) -> tuple[tuple[TermDeclaration, ...], tuple[ModelDeclaration, ...]]:
    """
    Use established field/class references and retain unsupported affected obligations.
    """
    budget = selection.budget
    uses: dict[tuple[str | None, str | None, int | None, str, str], Use] = {}
    for use in index.entries:
        budget.consume("work")
        budget.consume("states")
        uses[(use.model, use.field, use.branch, use.iri, use.role)] = use

    def rewritten(
        text: str,
        role: TermRole,
        model: str | None = None,
        field: str | None = None,
        branch: int | None = None,
    ) -> str:
        """
        Attach the original use when indexed, with explicit metadata for other slots.
        """
        budget.consume("work")
        use_role = "predicate" if role == "property" else role
        use = uses.get((model, field, branch, text, use_role))
        if use is None:
            use = Use(text, "term", "", model=model, field=field, branch=branch)
        return str(selection.rewrite(URIRef(text), (role,), use=use))

    def unsupported(term: URIRef | Literal) -> None:
        """
        Unsupported semantic slots block the candidate rather than retaining stale
        truth.
        """
        bound_node(term, budget)
        iri = term if isinstance(term, URIRef) else term.datatype
        if iri in selection.rules:
            selection.unknown(
                "An affected discriminator, literal datatype or dynamic membership "
                "requires independent requalification"
            )

    output_terms = []
    for term in terms:
        bound_text(term.iri, budget)
        bound_text(term.location, budget)
        roles = []
        for kind in term.kinds:
            bound_text(kind, budget)
            declared_role = declaration_role(URIRef(kind))
            if declared_role is not None:
                roles.append(declared_role)
        iri = URIRef(term.iri)
        roles_tuple = tuple(roles) or selection.kinds.get(iri, ())
        term_use = uses.get((None, None, None, term.iri, "term"))
        output_terms.append(
            replace(term, iri=str(selection.rewrite(iri, roles_tuple, use=term_use)))
        )
    output_models = []
    for model in models:
        model_types = tuple(
            rewritten(iri, "class", model.identifier) for iri in model.types
        )
        subject = model.subject
        if subject.expected_iri is not None:
            subject = replace(
                subject,
                expected_iri=rewritten(
                    subject.expected_iri, "individual", model.identifier
                ),
            )
        fields = []
        for field in model.fields:
            predicate = (
                field.predicate
                if field.predicate in ("@id", "@type")
                else rewritten(
                    field.predicate, "property", model.identifier, field.name
                )
            )
            branches = []
            for ordinal, branch in enumerate(field.values):
                types = tuple(
                    rewritten(iri, "class", model.identifier, field.name, ordinal)
                    for iri in branch.types
                )
                datatype = branch.datatype
                if datatype is not None:
                    unsupported(URIRef(datatype))
                for sample in branch.samples:
                    unsupported(sample)
                role = branch.role
                if role is not None:
                    role_types = tuple(
                        rewritten(iri, "class", model.identifier, field.name, ordinal)
                        for iri in role.types
                    )
                    bridge = role.bridge
                    if bridge is not None:
                        bridge = replace(
                            bridge,
                            predicate=rewritten(
                                bridge.predicate,
                                "property",
                                model.identifier,
                                field.name,
                                ordinal,
                            ),
                        )
                    role = replace(role, types=role_types, bridge=bridge)
                constraints = branch.constraints
                if constraints is not None and constraints.enumeration is not None:
                    enum = constraints.enumeration
                    if isinstance(enum, ValueEnumeration):
                        for value in (*enum.values, *enum.excluded):
                            unsupported(value)
                    else:
                        facet: TermRole = "unknown"
                        if role is not None:
                            chosen = (
                                role.bridge.source_role
                                if role.bridge is not None
                                else role.kind
                            )
                            facet = "individual" if chosen == "instance" else chosen
                        values: list[URIRef | Literal] = []
                        for value in enum:
                            if isinstance(value, URIRef):
                                values.append(
                                    URIRef(
                                        rewritten(
                                            str(value),
                                            facet,
                                            model.identifier,
                                            field.name,
                                            ordinal,
                                        )
                                    )
                                )
                            else:
                                unsupported(value)
                                values.append(value)
                        constraints = replace(constraints, enumeration=tuple(values))
                branches.append(
                    replace(branch, types=types, role=role, constraints=constraints)
                )
            fields.append(replace(field, predicate=predicate, values=tuple(branches)))
        coverage = model.coverage
        if coverage is not None:
            coverage = replace(
                coverage,
                extensions=tuple(
                    rewritten(iri, "property", model.identifier)
                    for iri in coverage.extensions
                ),
                reverse_extensions=tuple(
                    rewritten(iri, "property", model.identifier)
                    for iri in coverage.reverse_extensions
                ),
            )
        for group in model.emission_groups:
            for scenario in group.scenarios:
                if scenario.condition is not None:
                    unsupported(scenario.condition.equals)
        output_models.append(
            replace(
                model,
                types=model_types,
                fields=tuple(fields),
                subject=subject,
                coverage=coverage,
            )
        )
    # A reused child definition has one type slot, even when several parent fields
    # use it.
    registry = {model.identifier: model for model in models}
    updated = {model.identifier: model for model in output_models}
    for model in models:
        for field in model.fields:
            for ordinal, branch in enumerate(field.values):
                if branch.model is None or branch.model not in registry:
                    continue
                child = registry[branch.model]
                for old, new in zip(
                    child.types, updated[branch.model].types, strict=True
                ):
                    if (
                        rewritten(old, "class", model.identifier, field.name, ordinal)
                        != new
                    ):
                        selection.unknown(
                            "Shared child-model type uses require conflicting "
                            "replacement decisions"
                        )
    return tuple(output_terms), tuple(output_models)
