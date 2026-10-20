"""Framework extraction retains the ordinary core semantic contracts."""

from typing import Annotated

from pydantic import BaseModel, field_serializer
from rdflib.namespace import XSD

from bijux_ontology.declarations.types import ValueDeclaration as Value
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology_adapters.contracts import RdfField
from bijux_ontology_adapters.pydantic import model_declarations

EX = "https://example.org/"


def test_real_serializer_output_is_compared_with_its_explicit_contract() -> None:
    class Study(BaseModel):
        title: Annotated[
            str,
            RdfField(
                EX + "title",
                (Value("literal", str(XSD.string)),),
                emission="guaranteed",
                output_contract=True,
            ),
        ]

        @field_serializer("title")
        def title_length(self, value: str) -> int:
            return len(value)

    extracted = model_declarations({Study: (EX + "Study",)})
    document = {
        "@context": {"@vocab": EX},
        "@type": "Study",
        **Study(title="Example").model_dump(),
    }
    before = document.copy()
    report = check_emission(
        document,
        extracted.declarations,
        [ModelUsage(extracted.declarations[0].identifier)],
    )
    assert [issue.code for issue in report.issues] == ["emission.value_mismatch"]
    assert document == before
    document["title"] = "Example"
    assert check_emission(
        document,
        extracted.declarations,
        [ModelUsage(extracted.declarations[0].identifier)],
    ).verified
