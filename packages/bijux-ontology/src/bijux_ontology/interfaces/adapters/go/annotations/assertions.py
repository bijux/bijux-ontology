"""Interpret GAF, GPAD and GPI field roles over their admitted original rows."""

import re
from typing import cast

from bijux_ontology.diagnostics.errors import (
    InputError,
)
from bijux_ontology.execution.budgets import Operation

from .syntax import (
    PREFIX,
    RELATION,
    calendar_date,
    check_properties,
    group_items,
    identifier,
    logical_groups,
    source_error,
)
from .types import (
    GoAnnotation,
    GoEvidence,
    GoExtension,
    GoGeneProduct,
)


def interpret_annotation(
    source: str, number: int, fields: tuple[str, ...], kind: str, operation: Operation
) -> GoAnnotation:
    """Decode only supported field roles, without merging evidence by subject."""

    def cell(index: int, required: bool = True) -> str:
        """Read a native column and locate any missing required value."""
        value = fields[index]
        if required and not value:
            raise source_error(
                "A required field is empty",
                source,
                number,
                1 + sum(len(v) + 1 for v in fields[:index]),
            )
        return value

    if kind == "gaf":
        database = cell(0)
        if not PREFIX.fullmatch(database):
            raise InputError("GAF database requires an explicit prefix")
        subject = identifier(database + ":" + cell(1), strict=False)
        qualifier = group_items(cell(3), operation, required=True, identifiers=False)
        relation_tokens = tuple(value for value in qualifier if value != "NOT")
        if (
            len(relation_tokens) != 1
            or len(qualifier) != len(set(qualifier))
            or len(qualifier) > 2
            or not RELATION.fullmatch(relation_tokens[0])
        ):
            raise InputError("GAF requires one relation, optionally qualified by NOT")
        term = identifier(cell(4), strict=False)
        if not term.startswith("GO:") or cell(8) not in ("F", "P", "C"):
            raise InputError("GAF requires a GO term and supported aspect")
        if not re.fullmatch(r"[A-Z]+", cell(6), re.ASCII):
            raise InputError("GAF evidence requires an original evidence-code token")
        taxa = group_items(cell(12), operation, required=True, strict=False)
        if len(taxa) not in (1, 2) or any(
            not item.startswith("taxon:") or not item[6:].isdigit() for item in taxa
        ):
            raise InputError(
                "GAF requires entity taxon and at most one interacting taxon"
            )
        calendar_date(cell(13), compact=True)
        cell(2)
        cell(11)
        form = identifier(fields[16], strict=False) if fields[16] else None
        evidence = GoEvidence(
            cell(6),
            "gaf_code",
            group_items(cell(5), operation, required=True, strict=False),
            tuple(
                tuple(str(x) for x in group)
                for group in logical_groups(fields[7], operation, strict=False)
            ),
        )
        extensions = logical_groups(
            fields[15], operation, extension=True, curie_relation=False, strict=False
        )
        return GoAnnotation(
            source,
            number,
            "gaf",
            subject,
            form,
            relation_tokens[0],
            term,
            "NOT" in qualifier,
            evidence,
            taxa[0],
            taxa[1:],
            cast(tuple[tuple[GoExtension, ...], ...], extensions),
            fields[13],
            cell(14),
            fields,
        )
    subject = identifier(cell(0))
    if fields[1] not in ("", "NOT"):
        raise InputError("GPAD negation must be empty or NOT")
    relation, term, eco = (
        identifier(cell(2)),
        identifier(cell(3)),
        identifier(cell(5)),
    )
    if not term.startswith("GO:") or not eco.startswith("ECO:"):
        raise InputError("GPAD requires GO and ECO identifiers in their own roles")
    taxa = group_items(fields[7], operation)
    if any(
        not value.startswith("NCBITaxon:") or not value[10:].isdigit() for value in taxa
    ):
        raise InputError("GPAD interacting taxa require NCBITaxon identifiers")
    calendar_date(cell(8))
    check_properties(fields[11], operation)
    extensions = logical_groups(fields[10], operation, extension=True)
    evidence = GoEvidence(
        eco,
        "eco",
        group_items(cell(4), operation, required=True),
        tuple(
            tuple(str(x) for x in group)
            for group in logical_groups(fields[6], operation)
        ),
    )
    return GoAnnotation(
        source,
        number,
        "gpad",
        subject,
        None,
        relation,
        term,
        fields[1] == "NOT",
        evidence,
        None,
        taxa,
        tuple(
            tuple(x for x in group if isinstance(x, GoExtension))
            for group in extensions
        ),
        fields[8],
        cell(9),
        fields,
    )


def interpret_gene_product(
    source: str, number: int, fields: tuple[str, ...], operation: Operation
) -> GoGeneProduct:
    """Interpret one original GPI row without claiming companion population coverage."""
    for position in (0, 4, 5):
        group_items(fields[position], operation, required=True)

    if (
        not fields[1]
        or " " in fields[1]
        or "|" in fields[1]
        or "|" in fields[2]
        or not fields[2]
        or "|" in fields[0]
        or "|" in fields[5]
        or not fields[5].startswith("NCBITaxon:")
        or not fields[5][10:].isdigit()
    ):
        raise InputError("GPI requires one subject, symbol, name and entity taxon")

    for position in (3, 6, 7, 8, 9):
        group_items(fields[position], operation, identifiers=position != 3)

    check_properties(fields[10], operation)

    operation.consume("states", 4)

    operation.consume("bytes", 256)

    return GoGeneProduct(source, number, fields[0], fields[5], fields)
