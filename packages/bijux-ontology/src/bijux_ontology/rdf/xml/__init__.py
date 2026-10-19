"""Maintained bounded XML input before native RDF/XML interpretation."""

from .preparation import prepare_xml
from .types import XMLCapabilityError, XMLEncoding, XMLInput

__all__ = ["XMLCapabilityError", "XMLEncoding", "XMLInput", "prepare_xml"]
