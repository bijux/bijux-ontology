"""Typed SHACL projection of explicitly declared logical RDF path fields."""

from bijux_ontology.shacl.path_fields.projection import project_path_field
from bijux_ontology.shacl.path_fields.types import PathFieldProjection

__all__ = ["PathFieldProjection", "project_path_field"]
