"""Owned vocabulary inputs shared by checking, inspection and upgrade commands."""

from pathlib import Path
from typing import cast

from bijux_ontology._json import known_keys, object_value, text_value
from bijux_ontology.errors import InputError
from bijux_ontology.interfaces.cli.arguments import VocabularyInput
from bijux_ontology.interfaces.cli.inputs import protect_paths, rdf_input, source_map
from bijux_ontology.jsonld.resources import OfflineDocumentLoader
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary


def load_command_vocabulary(
    selected: VocabularyInput,
    limits: Limits,
    *,
    inputs: tuple[Path, ...],
    output: Path | None,
) -> Vocabulary:
    """Prepare explicit command resources independently of dispatch."""
    resources = source_map(selected.sources, limits)
    supplied = {}
    for iri, value in resources.items():
        if isinstance(value, Path):
            supplied[iri] = RdfSource(value, document_iri=iri)
        elif isinstance(value, dict):
            entry = object_value(value, location=iri)
            known_keys(entry, {"path", "format", "base", "effective_url"}, location=iri)
            assert selected.sources is not None
            parent = selected.sources.parent
            supplied[iri] = RdfSource(
                parent / text_value(entry.get("path"), location=iri),
                format=cast(str | None, entry.get("format")),
                document_iri=cast(str, entry.get("base", iri)),
                effective_url=cast(str | None, entry.get("effective_url")),
            )
        else:
            raise InputError(
                "RDF source maps require filenames or explicit source objects",
                location=iri,
            )
    protect_paths(
        inputs + tuple(cast(Path, item.content) for item in supplied.values()), output
    )
    lookups = tuple(
        RdfSource(path, format=selected.lookup_format) for path in selected.lookup
    )
    return load_vocabulary(
        rdf_input(
            selected.ontology, format=selected.format, base=selected.base, limits=limits
        ),
        imports=supplied,
        lookup=lookups,
        graph_name=selected.graph,
        union=selected.union,
        limits=limits,
        document_loader=OfflineDocumentLoader(
            {iri: source.content for iri, source in supplied.items()},
            max_bytes=limits.max_bytes,
        ),
    )
