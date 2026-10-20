"""Malformed public sequence boundaries produce actionable typed input failures."""

from collections.abc import Sequence
from typing import cast

import pytest
from rdflib import Graph

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.vocabulary.comparison import compare_vocabularies
from bijux_ontology.workflows.upgrades.assessment import assess_upgrade
from bijux_ontology.workflows.upgrades.uses import index_uses


def test_invalid_sequence_shapes_do_not_escape_as_python_attribute_or_type_errors() -> (
    None
):
    with pytest.raises(InputError):
        index_uses(cast(Sequence[TermDeclaration], None))
    with pytest.raises(InputError):
        compare_vocabularies(Graph(), Graph(), scopes=cast(Sequence[str], None))
    with pytest.raises(InputError):
        compare_vocabularies(Graph(), Graph(), annotation_predicates="urn:predicate")
    with pytest.raises(InputError):
        assess_upgrade(
            Graph(),
            Graph(),
            contract_reports=cast(Sequence[tuple[Report, Report]], None),
        )
