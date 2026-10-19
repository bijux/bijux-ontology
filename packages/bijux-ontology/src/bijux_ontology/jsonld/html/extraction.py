"""Bounded maintained HTML extraction with exact explicit script selection."""

import json
from email.message import Message
from typing import Any, Literal
from urllib.parse import unquote, urldefrag

from bijux_ontology._json import decode_json, own_json
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.jsonld.resources import DocumentLoader, _consume_work
from bijux_ontology.jsonld.runtime import processor
from bijux_ontology.rdf.identifiers import absolute_iri

from .types import HtmlExtraction, HtmlScript


def extract_html_jsonld(
    content: str | bytes,
    document_url: str,
    *,
    script_id: str | None = None,
    profile: str | None = None,
    all_scripts: bool = True,
    html_base: Literal["honor", "ignore"] = "honor",
    document_loader: DocumentLoader | None = None,
    direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> HtmlExtraction:
    """Extract selected original scripts, then prepare their RDF entirely offline.

    The document URL's fragment selects an ID unless script_id is supplied.
    Profiles match explicit media-type profile IRIs; an absent profile never
    falls back to unrelated scripts. all_scripts=False selects the first match.
    An explicit ID must identify exactly one JSON-LD script. HTML bases are
    honored by default or ignored explicitly. No script code is evaluated.
    JSON-LD processing and context retrieval use the ordinary controlled loader.
    """
    if limits is not None and budget is not None:
        raise InputError("Supply limits or a shared budget, not both")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    scripts: list[HtmlScript] = []
    document_json = None
    base = None
    source = None
    children: list[Report] = []
    complete = False
    try:
        if type(all_scripts) is not bool or html_base not in ("honor", "ignore"):
            raise InputError("Choose boolean script selection and an HTML base policy")
        if type(document_url) is not str:
            raise InputError("HTML extraction requires an explicit document URL")
        bound_text(document_url, operation)
        absolute_iri(document_url)
        document_url, fragment = urldefrag(document_url)
        if script_id is None and fragment:
            script_id = unquote(fragment)
        for value in (script_id, profile):
            if value is not None:
                if type(value) is not str or not value:
                    raise InputError("Script ID and profile must be nonempty text")
                bound_text(value, operation)
        if profile is not None:
            absolute_iri(profile)
        if not isinstance(content, (str, bytes)):
            raise InputError("HTML extraction requires original UTF-8 text or bytes")
        if len(content) > operation.limits.max_bytes:
            raise LimitExceeded("HTML input exceeds max_bytes")
        if isinstance(content, bytes):
            operation.consume("bytes", len(content))
            operation.consume("work", len(content) + 1)
            content = content.decode("utf-8")
        else:
            bound_text(content, operation)
        jsonld = processor()
        try:
            from lxml import html  # type: ignore[import-untyped]
        except ImportError as error:
            raise ResourceError(
                "HTML extraction requires bijux-ontology[jsonld]"
            ) from error
        parser = html.HTMLParser(no_network=True, huge_tree=False)
        try:
            tree = html.document_fromstring(content, parser=parser)
        except (html.etree.LxmlError, ValueError) as error:
            raise InputError(
                "Maintained HTML parsing could not read the document"
            ) from error
        if any(error.type_name == "ERR_RESOURCE_LIMIT" for error in parser.error_log):
            raise LimitExceeded("HTML parser exhausted its document limit")
        if any(error.level_name == "FATAL" for error in parser.error_log):
            raise InputError("HTML parser could not retain the complete document")
        others: list[Any] = []
        matching_ids = 0
        position = 0
        for element in tree.iter():
            operation.consume("states")
            operation.consume("bytes", 64)
            operation.consume("work")
            for depth, _ in enumerate(element.iterancestors(), 1):
                operation.consume("work")
                if depth > operation.limits.max_depth:
                    raise LimitExceeded("HTML tree exceeds max_depth")
            if element.tag == "base" and html_base == "ignore":
                others.append(element)
            if element.tag != "script":
                continue
            position += 1
            identifier = element.get("id")
            media = Message()
            content_type = element.get("type", "")
            bound_text(content_type, operation)
            media["content-type"] = content_type
            if identifier is not None:
                bound_text(identifier, operation)
            if script_id is not None and identifier == script_id:
                matching_ids += 1
            eligible = (
                media.get_content_type() == "application/ld+json"
                and (script_id is None or identifier == script_id)
                and (
                    profile is None
                    or profile in str(media.get_param("profile", "")).split()
                )
            )
            if eligible and (all_scripts or not scripts):
                operation.consume("bytes", 64)
                # Validate actual selected text before PyLD's permissive JSON decoder.
                text = element.text or ""
                bound_text(text, operation)
                parsed = decode_json(
                    text,
                    max_bytes=operation.limits.max_bytes,
                    max_depth=operation.limits.max_depth,
                )
                _consume_work(parsed, operation)
                if not isinstance(parsed, (dict, list)):
                    raise InputError(
                        "Selected JSON-LD script requires an object or array"
                    )
                scripts.append(HtmlScript(position, identifier, element.sourceline))
                # Selection is already exact; the maintained extractor receives
                # a canonical media type, without changing script content.
                element.set("type", "application/ld+json")
            else:
                others.append(element)
        if not scripts or script_id is not None and matching_ids != 1:
            raise InputError(
                "The selection requires actual unambiguous JSON-LD scripts"
            )
        for element in others:
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
        projected = html.tostring(tree, encoding="unicode")
        bound_text(projected, operation)
        options: dict[str, object] = {"base": document_url, "extractAllScripts": True}
        try:
            document = jsonld.load_html(projected, document_url, None, options)
        except jsonld.JsonLdError as error:
            raise InputError(
                "Maintained HTML extraction could not read selected scripts"
            ) from error
        effective_base = options.get("base")
        if not isinstance(effective_base, str):
            raise InputError(
                "Maintained HTML extraction did not establish a logical base"
            )
        base = effective_base
        bound_text(base, operation)
        absolute_iri(base)
        document = own_json(
            document,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        )
        _consume_work(document, operation)
        candidate = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        bound_text(candidate, operation)
        document_json = candidate
        prepared = prepare_jsonld(
            document,
            base=base,
            document_iri=document_url,
            document_loader=document_loader,
            direction=direction,
            budget=operation,
        )
        # Keep the completed child before any later diagnostic adoption can stop.
        children.append(prepared.report)
        retain_report(prepared.report, operation, issues)
        source = prepared.source
        complete = prepared.report.complete
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, UnicodeError) as error:
        try:
            issues.append(
                Issue(
                    "context.html_extraction_unverified",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return HtmlExtraction(
        document_json,
        base,
        tuple(scripts),
        source,
        Report(
            "jsonld.html",
            int(complete),
            tuple(issues),
            requested=1,
            complete=complete,
            children=tuple(children),
            regime="selected-html-jsonld-extraction",
        ),
    )
