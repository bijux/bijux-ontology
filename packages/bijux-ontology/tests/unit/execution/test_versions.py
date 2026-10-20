"""Reject older numerical APIs and unqualified release variants before evaluation."""

import pytest

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.execution.versions import qualify_release


@pytest.mark.parametrize(
    "package,actual",
    (
        ("scipy", "1.14.0"),
        ("scipy", "1.17.2"),
        ("scipy", "1.17.1rc1"),
        ("scipy", "1.17.1+local"),
        ("bmt", "1.4.8"),
    ),
)
def test_unsupported_native_apis_and_release_variants(
    package: str, actual: str
) -> None:
    with pytest.raises(ResourceError, match="Unsupported " + package):
        qualify_release(package, actual)


def test_exact_native_requirement_cannot_be_an_open_constraint() -> None:
    with pytest.raises(InputError, match="three-part release"):
        qualify_release("scipy", "1.17.1", ">=1.17")
