"""Native RDF/XML shares an actual bounded XML encoding and entity boundary."""

from io import BytesIO
from xml.parsers import expat

import pytest
from rdflib.namespace import RDF

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.rdf.xml import XMLCapabilityError, prepare_xml

ROOT = (
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
    'xmlns:p="urn:p:" xml:base="https://example.org/" xml:lang="fr-CA">'
)
BODY = (
    '<rdf:Description rdf:about="record"><p:label>café</p:label>'
    '<p:link rdf:resource="other"/></rdf:Description></rdf:RDF>'
)


def _encoded(document: str, encoding: str) -> bytes:
    """Retain concrete BOM byte order and an actual matching XML declaration."""
    header = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        if encoding == "utf-8"
        else '<?xml version="1.0" encoding="UTF-16"?>'
    )
    text = header + document
    return (
        text.encode("utf-8")
        if encoding == "utf-8"
        else (b"\xff\xfe" if encoding == "utf-16-le" else b"\xfe\xff")
        + text.encode(encoding)
    )


def _terms(payload: bytes) -> set[tuple[tuple[object, ...], ...]]:
    """Inspect exact maintained native terms rather than lexical serialization."""
    return {
        tuple(term_key(node) for node in triple)
        for triple in prepare_rdf(payload, format="xml").graph()
    }


def test_utf8_and_both_utf16_orders_preserve_native_base_language_and_namespaces() -> (
    None
):
    """Maintained UTF-16 decoding cannot change the selected RDF terms."""
    expected = _terms(_encoded(ROOT + BODY, "utf-8"))
    assert any(term[0] == ("iri", "https://example.org/record") for term in expected)
    for encoding in ("utf-16-le", "utf-16-be"):
        payload = _encoded(ROOT + BODY, encoding)
        assert _terms(payload) == expected
        qualified = prepare_xml(payload, budget=Budget())
        assert qualified.encoding == encoding and qualified.payload.startswith(b"<?xml")
        stream = BytesIO(payload)
        assert _terms(payload) == {
            tuple(term_key(n) for n in t)
            for t in prepare_rdf(stream, format="xml").graph()
        }
        assert not stream.closed
    assert _terms(b"\xef\xbb\xbf" + _encoded(ROOT + BODY, "utf-8")) == expected
    assert _terms(b"\xff\xfe" + (ROOT + BODY).encode("utf-16-le")) == expected
    for encoding in ("utf-16-le", "utf-16-be"):
        with pytest.raises(XMLCapabilityError, match="byte-order mark"):
            prepare_xml((ROOT + BODY).encode(encoding), budget=Budget())


def test_internal_entities_and_harmless_comment_tokens() -> None:
    """Real parser callbacks distinguish declarations from comment contents."""
    document = (
        '<!DOCTYPE rdf:RDF [<!ENTITY ns "urn:p:"><!ENTITY first "first">'
        '<!ENTITY text "&first; &amp; &#65;">]>'
        "<!-- harmless <!DOCTYPE and <!ENTITY -->"
        + ROOT.replace('xmlns:p="urn:p:"', 'xmlns:p="&ns;"')
        + '<rdf:Description rdf:about="record"><p:label>&text;</p:label>'
        "</rdf:Description></rdf:RDF>"
    )
    expected = _terms(_encoded(document, "utf-8"))
    assert any(term[2][1] == "first & A" for term in expected)
    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
        payload = _encoded(document, encoding)
        result = prepare_xml(payload, budget=Budget())
        assert result.internal_entities == 3 and _terms(payload) == expected
        if encoding == "utf-8":
            assert result.payload is payload


def test_xml_literal_markup_cdata_and_validated_declaration_columns_are_preserved() -> (
    None
):
    """Only the proven encoding field changes; body markup never passes a serializer."""
    document = (
        ROOT + '<rdf:Description rdf:about="record"><p:literal rdf:parseType="Literal">'
        '<p:span a="a&amp;b">UTF-16 UTF-16<![CDATA[x<y]]></p:span></p:literal>'
        "</rdf:Description></rdf:RDF>"
    )
    expected = _terms(_encoded(document, "utf-8"))
    assert any(
        term[2][-1] == str(RDF.XMLLiteral)
        and isinstance(term[2][1], str)
        and "UTF-16 UTF-16" in term[2][1]
        for term in expected
    )
    for encoding in ("utf-16-le", "utf-16-be"):
        payload = _encoded(document, encoding)
        actual = prepare_xml(payload, budget=Budget()).payload.decode("utf-8")
        original = payload.decode("utf-16")
        assert len(actual) == len(original)
        assert actual[original.index("?>") + 2 :] == document
        assert _terms(payload) == expected
    multiline = (
        "<?xml\n version='1.0'\n encoding='UTF-16'\n standalone='yes'?>\n" + ROOT + BODY
    )
    normalized = prepare_xml(
        multiline.encode("utf-16"), budget=Budget()
    ).payload.decode()
    assert len(multiline) == len(normalized)
    assert [i for i, c in enumerate(multiline) if c == "\n"] == [
        i for i, c in enumerate(normalized) if c == "\n"
    ]
    for rdf_prefix in ("rdf", "r"):
        bound = document.replace('xmlns:p="urn:p:"', 'xmlns:p="&ns;"')
        bound = (
            ('<!DOCTYPE rdf:RDF [<!ENTITY ns "urn:p:">]>' + bound)
            .replace("rdf:", rdf_prefix + ":")
            .replace("xmlns:rdf=", "xmlns:" + rdf_prefix + "=")
        )
        with pytest.raises(XMLCapabilityError, match="XMLLiteral"):
            prepare_rdf(_encoded(bound, "utf-8"), format="xml")


def test_external_parameter_and_other_dtd_constructs_never_create_a_source() -> None:
    """The finite entity profile does not enable external or defaulted XML semantics."""
    for declaration in (
        '<!ENTITY e SYSTEM "file:///unavailable-entity">',
        '<!ENTITY e SYSTEM "https://unavailable.example/entity">',
        '<!ENTITY % e " ">%e;',
        "%missing;",
        '<!ATTLIST rdf:RDF xml:lang CDATA "de">',
        "<!ELEMENT rdf:RDF ANY>",
        '<!NOTATION file SYSTEM "file:///unavailable">',
        '<!ENTITY e "<p:injected/>">',
        '<!ENTITY e "first"><!ENTITY e "second">',
    ):
        payload = ("<!DOCTYPE rdf:RDF [" + declaration + "]>" + ROOT + BODY).encode()
        with pytest.raises(XMLCapabilityError):
            prepare_rdf(payload, format="xml")
    external_subset = (
        '<!DOCTYPE rdf:RDF SYSTEM "https://unavailable.example/dtd">' + ROOT + BODY
    ).encode()
    with pytest.raises(XMLCapabilityError, match="external document type"):
        prepare_rdf(external_subset, format="xml")
    latin = ('<?xml version="1.0" encoding="ISO-8859-1"?>' + ROOT + BODY).encode(
        "latin-1"
    )
    with pytest.raises(XMLCapabilityError, match="encoding"):
        prepare_rdf(latin, format="xml")
    assert _terms(_encoded(ROOT + BODY, "utf-8"))


def test_expansion_depth_and_shared_source_caps_stop_without_partial_results() -> None:
    """Small raw input cannot bypass the selected expanded cap via a shared budget."""
    declarations = '<!ENTITY e "' + "a" * 100 + '">'
    document = (
        "<!DOCTYPE rdf:RDF ["
        + declarations
        + "]>"
        + ROOT
        + '<rdf:Description rdf:about="record"><p:label>'
        + "&e;" * 10
        + "</p:label></rdf:Description></rdf:RDF>"
    )
    payload = document.encode()
    assert len(payload) < 500
    with pytest.raises(LimitExceeded):
        prepare_rdf(payload, format="xml", max_bytes=500, budget=Budget())
    attribute = document.replace(
        "<p:label>" + "&e;" * 10 + "</p:label>",
        '<p:link rdf:resource="' + "&e;" * 10 + '"/>',
    )
    with pytest.raises(LimitExceeded):
        prepare_rdf(attribute.encode(), format="xml", max_bytes=500, budget=Budget())
    recursive = (
        '<!DOCTYPE rdf:RDF [<!ENTITY e "&e;">]>'
        + ROOT
        + '<rdf:Description rdf:about="record"><p:label>&e;</p:label>'
        "</rdf:Description></rdf:RDF>"
    ).encode()
    with pytest.raises(ResourceError) as recursion:
        prepare_rdf(recursive, format="xml")
    assert isinstance(recursion.value.__cause__, expat.ExpatError)
    amplification = (
        '<!DOCTYPE r [<!ENTITY a "'
        + "x" * 100
        + '">'
        + "".join(
            "<!ENTITY " + chr(98 + i) + ' "' + ("&" + chr(97 + i) + ";") * 10 + '">'
            for i in range(5)
        )
        + ']><r a="&f;"/>'
    ).encode()
    with pytest.raises(LimitExceeded, match="native limits") as amplified:
        prepare_xml(amplification, budget=Budget())
    assert isinstance(amplified.value.__cause__, expat.ExpatError)
    with pytest.raises(LimitExceeded, match="max_depth"):
        prepare_rdf(
            _encoded(ROOT + BODY, "utf-8"), format="xml", limits=Limits(max_depth=1)
        )
    with pytest.raises(LimitExceeded):
        prepare_rdf(
            _encoded(ROOT + BODY, "utf-16-le"),
            format="xml",
            limits=Limits(max_total_bytes=1000),
        )


def test_xml_and_later_rdf_failures_preserve_original_source() -> None:
    """XML errors refer to original input; unavailable RDF spans stay absent."""
    source = RdfSource(
        (ROOT + "\n<unclosed>").encode(), "xml", "https://example.org/broken.rdf"
    )
    with pytest.raises(ResourceError) as invalid:
        prepare_rdf(source)
    assert (
        invalid.value.failure is not None
        and invalid.value.failure.document_iri == "https://example.org/broken.rdf"
    )
    assert isinstance(invalid.value.__cause__, expat.ExpatError)
    assert invalid.value.__cause__.lineno == 2
    failure = invalid.value.failure
    assert failure is not None and failure.span is not None
    assert (failure.span.start_line, failure.span.start_column) == (
        invalid.value.__cause__.lineno,
        invalid.value.__cause__.offset + 1,
    )
    assert failure.span.end_line is None and failure.span.end_column is None
    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
        with pytest.raises(ResourceError) as malformed:
            prepare_rdf(
                RdfSource(
                    _encoded(ROOT + "\n<unclosed>é😀", encoding), "xml", "urn:xml"
                )
            )
        cause = malformed.value.__cause__
        evidence = malformed.value.failure
        assert isinstance(cause, expat.ExpatError)
        assert evidence is not None and evidence.span is not None
        assert evidence.span.start_line == cause.lineno
        assert evidence.span.start_column == cause.offset + 1
    bad = (
        ROOT + '<rdf:Description rdf:about="record"><p:label>é😀</p:label>'
        '<p:bad rdf:resource="urn:o" rdf:nodeID="n"/></rdf:Description></rdf:RDF>'
    )
    from bijux_ontology.interfaces.execution.protocol import report_from_data
    from bijux_ontology.reports import Issue, Location, Report

    columns = []
    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
        payload = (ROOT + "<p:bad>é😀</rdf:RDF>").encode(encoding)
        if encoding != "utf-8":
            payload = (
                b"\xff\xfe" if encoding == "utf-16-le" else b"\xfe\xff"
            ) + payload
        with pytest.raises(ResourceError) as unicode_error:
            prepare_rdf(RdfSource(payload, "xml", "urn:xml"))
        error = unicode_error.value
        assert error.failure is not None and error.failure.span is not None
        span = error.failure.span
        assert span.column_unit == (
            "unicode_code_points" if encoding == "utf-8" else "utf16_code_units"
        )
        assert span.start_column is not None
        columns.append(span.start_column)
        report = Report(
            "xml",
            issues=(
                Issue(
                    "input.invalid",
                    "Malformed XML",
                    kind="input_error",
                    details=Location.from_error(error),
                ),
            ),
        )
        assert report_from_data(report.to_dict()) == report
    assert columns[1] == columns[2] == columns[0] + 1
    failures = []
    for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
        with pytest.raises(ResourceError) as failed:
            prepare_rdf(
                RdfSource(
                    _encoded(bad, encoding),
                    "xml",
                    "https://example.org/rdf-grammar.rdf",
                )
            )
        assert failed.value.failure is not None
        failures.append(failed.value.failure.span)
        assert (
            failed.value.failure.document_iri == "https://example.org/rdf-grammar.rdf"
        )
    assert failures == [None, None, None]
    with pytest.raises(InputError):
        prepare_xml(b"<a/>", budget=Budget(), max_bytes=0)
