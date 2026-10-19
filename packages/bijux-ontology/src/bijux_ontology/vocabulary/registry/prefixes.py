"""Explicit identifier abbreviations over supplied vocabulary prefixes."""

from collections.abc import Mapping

from bijux_ontology.errors import InputError
from bijux_ontology.rdf.identifiers import absolute_iri


def expand_curie(value: str, prefixes: Mapping[str, str]) -> str:
    """Expand a supplied prefix; use exact absolute IRIs directly through lookup."""
    if not isinstance(value, str) or ":" not in value:
        raise InputError("A CURIE needs an explicitly supplied prefix")
    prefix, suffix = value.split(":", 1)
    if prefix not in prefixes:
        raise InputError("Unknown prefix", location=prefix)
    return absolute_iri(absolute_iri(prefixes[prefix]) + suffix)


def compact_iri(iri: str, prefixes: Mapping[str, str]) -> str:
    """Compact with the longest exact namespace, then the lexical prefix."""
    absolute_iri(iri)
    candidates = []
    for prefix, namespace in prefixes.items():
        absolute_iri(namespace)
        if not isinstance(prefix, str) or ":" in prefix:
            raise InputError("Prefix labels must be strings without a colon")
        if iri.startswith(namespace):
            candidates.append((len(namespace), prefix, namespace))
    if not candidates:
        return iri
    _, prefix, namespace = min(candidates, key=lambda item: (-item[0], item[1]))
    return prefix + ":" + iri[len(namespace) :]
