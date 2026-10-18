"""Compile a finite explicit SKOS label profile using existing typed SHACL APIs."""

import hashlib

from rdflib import Literal, URIRef
from rdflib.namespace import RDF, SKOS, XSD

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.shacl.authoring.compiling import compile_shapes
from bijux_ontology.shacl.authoring.parameters import ShaclParameter
from bijux_ontology.shacl.authoring.types import (
    ShaclLogical,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclShapeRef,
    ShaclValue,
)
from bijux_ontology.shacl.components.compiling import compile_components
from bijux_ontology.shacl.components.types import (
    ComponentUse,
    ComponentValidator,
    ShaclComponent,
)

from .types import SkosProfile, SkosProfileShapes

_SUPPORTED = frozenset(("lexical-labels", "label-integrity", "language-coverage"))
_ASSUMPTIONS = (
    "Explicit named targets match native concepts selection; no class discovery.",
    "Both validators read the same physical asserted graph; ontology inference is off.",
    "Only selected plain SKOS predicates are checked; XL rules are not inferred.",
    "lexical-labels is an explicit application policy, not generic SKOS invalidity.",
)
_IDENTITY = (
    "(sameTerm(?left, ?right) || (isLiteral(?left) && isLiteral(?right) && "
    f"datatype(?left) = <{XSD.string}> && datatype(?right) = <{XSD.string}> && "
    "STR(?left) = STR(?right)))"
)
_INTEGRITY = f"""ASK {{
 FILTER($integrityEnabled)
 FILTER NOT EXISTS {{
  $this <{SKOS.prefLabel}> ?left, ?right .
  FILTER(isLiteral(?left) && isLiteral(?right) &&
   LCASE(LANG(?left)) = LCASE(LANG(?right)) && !{_IDENTITY})
 }}
 FILTER NOT EXISTS {{
  $this <{SKOS.prefLabel}> ?left ; <{SKOS.altLabel}> ?right .
  FILTER({_IDENTITY})
 }}
 FILTER NOT EXISTS {{
  $this <{SKOS.prefLabel}> ?left ; <{SKOS.hiddenLabel}> ?right .
  FILTER({_IDENTITY})
 }}
 FILTER NOT EXISTS {{
  $this <{SKOS.altLabel}> ?left ; <{SKOS.hiddenLabel}> ?right .
  FILTER({_IDENTITY})
 }}
}}"""


def compile_skos_profile(
    profile: SkosProfile,
    *,
    trusted_sparql: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> SkosProfileShapes:
    """Export selected label obligations without widening native policy or scope.

    The complete finite profile selects all three supported rules, at least one
    exact required language, named targets and explicit SPARQL trust. Other rule
    combinations still retain useful supported Core definitions beside required
    untranslated evidence. Language matching uses exact case-insensitive tags;
    fallback tags may satisfy each requested language and other labels remain
    allowed. Actual execution belongs to the existing maintained SHACL APIs.
    """
    if not isinstance(profile, SkosProfile) or type(trusted_sparql) is not bool:
        raise InputError("SKOS compilation requires a typed profile and boolean trust")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("SKOS profile limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    definitions: list[ShaclNodeShape | ShaclPropertyShape] = []
    translated: list[str] = []
    untranslated: list[str] = []
    children: list[Report] = []
    source = None
    qualified = None
    assumptions: list[str] = []
    components = None
    requested = len(profile.rules)
    try:
        bound_node(profile.shape, operation)
        absolute_iri(profile.shape)
        for values in (
            profile.concepts,
            profile.languages,
            profile.rules,
            profile.language_fallback,
        ):
            if type(values) is not tuple:
                raise InputError(
                    "SKOS profile selections require immutable finite tuples"
                )
            operation.consume("states", len(values))
        for node in profile.concepts:
            if not isinstance(node, URIRef):
                raise InputError("SKOS targets require named IRI terms")
            bound_node(node, operation)
            absolute_iri(node)
        for text in (
            *profile.rules,
            *profile.languages,
            *profile.language_fallback,
        ):
            bound_text(text, operation)
        for tag in (*profile.languages, *profile.language_fallback):
            language_tag(tag)
        operation.consume("states", len(profile.rules))
        selected = set(profile.rules)
        if len(selected) != len(profile.rules) or len(set(profile.concepts)) != len(
            profile.concepts
        ):
            raise InputError("SKOS policy rules and targets must be distinct")
        operation.consume("states")
        qualified = profile
        for assumption in _ASSUMPTIONS:
            bound_text(assumption, operation)
            operation.consume("states")
            assumptions.append(assumption)
        if not profile.concepts or not profile.rules:
            issues.append(
                Issue(
                    "shacl.skos_scope_untranslated",
                    "An explicit nonempty target and rule inventory is required",
                    kind="unverified",
                )
            )
        root_text = str(profile.shape)
        operation.consume("work", len(root_text) + 1)
        encoded_root = root_text.encode("utf-8")
        root_bytes = len(encoded_root)
        operation.consume("bytes", root_bytes + 64)
        parameter_namespace = (
            "urn:bijux:ontology:skos-profile:"
            + hashlib.sha256(encoded_root).hexdigest()
        )

        def iri(suffix: str) -> URIRef:
            """Reserve derived identity storage before concatenating exact IRIs."""
            size = root_bytes + len(suffix) + 1
            if size > operation.limits.max_bytes:
                raise LimitExceeded("SKOS shape identity exceeds max_bytes")
            operation.consume("work", len(root_text) + len(suffix) + 2)
            operation.consume("bytes", size)
            return URIRef(root_text + "/" + suffix)

        def parameter_iri(suffix: str) -> URIRef:
            """Keep parameter local names valid for every absolute root shape IRI."""
            size = len(parameter_namespace) + len(suffix) + 1
            if size > operation.limits.max_bytes:
                raise LimitExceeded("SKOS parameter identity exceeds max_bytes")
            operation.consume("work", size + 1)
            operation.consume("bytes", size)
            return URIRef(parameter_namespace + "/" + suffix)

        def unknown(rule: str, reason: str) -> None:
            """Retain one bounded untranslated obligation before returning inventory."""
            operation.consume("states")
            issues.append(
                Issue("shacl.skos_rule_untranslated", reason, rule, kind="unverified")
            )
            untranslated.append(rule)

        xl_scope = "xl" in selected
        lexical = "lexical-labels" in selected and not xl_scope
        references = []
        if lexical:
            operation.consume("states", 2)
            string, tagged = iri("string"), iri("language-string")
            definitions.extend(
                (
                    ShaclNodeShape(
                        string, constraints=(ShaclValue(datatype=str(XSD.string)),)
                    ),
                    ShaclNodeShape(
                        tagged, constraints=(ShaclValue(datatype=str(RDF.langString)),)
                    ),
                )
            )
            for index, predicate in enumerate(
                (SKOS.prefLabel, SKOS.altLabel, SKOS.hiddenLabel)
            ):
                operation.consume("states", 2)
                prop = iri("label-" + str(index))
                definitions.append(
                    ShaclPropertyShape(
                        prop,
                        predicate,
                        (
                            ShaclLogical(
                                "or", (ShaclShapeRef(string), ShaclShapeRef(tagged))
                            ),
                        ),
                    )
                )
                references.append(ShaclShapeRef(prop))
            operation.consume("states")
            translated.append("lexical-labels")
        operation.consume("states")
        definitions.append(
            ShaclNodeShape(
                profile.shape, tuple(references), target_nodes=profile.concepts
            )
        )
        catalog = []
        uses = []
        for rule in profile.rules:
            operation.consume("work")
            if rule not in _SUPPORTED:
                unknown(
                    rule,
                    "The requested SKOS rule has no supported faithful translation",
                )
            elif xl_scope:
                unknown(
                    rule,
                    "Selected XL implications change this label policy's value scope",
                )
            elif rule != "lexical-labels" and not lexical:
                unknown(
                    rule,
                    "This translation requires explicit lexical-labels policy",
                )
            elif rule != "lexical-labels" and not trusted_sparql:
                unknown(
                    rule,
                    "Exact label semantics require explicitly trusted ASK components",
                )
        if "language-coverage" not in selected or not profile.languages:
            issues.append(
                Issue(
                    "shacl.skos_scope_untranslated",
                    "Resource-presence uncertainty requires selected "
                    "nonempty language coverage in this finite profile",
                    kind="unverified",
                )
            )
        if lexical and trusted_sparql:
            if "label-integrity" in selected:
                operation.consume("states", 3)
                component, parameter = (
                    iri("LabelIntegrity"),
                    parameter_iri("integrityEnabled"),
                )
                catalog.append(
                    ShaclComponent(
                        str(component),
                        (ShaclParameter(str(parameter), datatype=str(XSD.boolean)),),
                        (ComponentValidator(ask=_INTEGRITY),),
                    )
                )
                uses.append(
                    ComponentUse(
                        profile.shape,
                        str(component),
                        ((str(parameter), Literal(True)),),
                    )
                )
                translated.append("label-integrity")
            if "language-coverage" in selected and profile.languages:
                operation.consume("states", len(profile.language_fallback) + 3)
                component, parameter = (
                    iri("RequiredLanguage"),
                    parameter_iri("requiredLanguage"),
                )
                parameters = [ShaclParameter(str(parameter), datatype=str(XSD.string))]
                fallbacks = []
                expressions = ["LCASE(STR($requiredLanguage))"]
                for index, tag in enumerate(profile.language_fallback):
                    operation.consume("states", 3)
                    fallback = parameter_iri("fallback" + str(index))
                    parameters.append(
                        ShaclParameter(str(fallback), datatype=str(XSD.string))
                    )
                    fallbacks.append((str(fallback), Literal(tag)))
                    expressions.append("LCASE(STR($fallback" + str(index) + "))")
                size = sum(len(text) for text in expressions) + 2 * len(expressions)
                prefix = f"ASK {{ $this <{SKOS.prefLabel}> ?label . "
                middle = "FILTER(isLiteral(?label) && LCASE(LANG(?label)) IN ("
                ending = ")) }"
                size += len(prefix) + len(middle) + len(ending)
                if size > operation.limits.max_bytes:
                    raise LimitExceeded("SKOS language query exceeds max_bytes")
                operation.consume("work", size)
                operation.consume("bytes", size)
                choices = ", ".join(expressions)
                query = prefix + middle + choices + ending
                bound_text(query, operation)
                catalog.append(
                    ShaclComponent(
                        str(component),
                        tuple(parameters),
                        (ComponentValidator(ask=query),),
                    )
                )
                for index, tag in enumerate(profile.languages):
                    operation.consume("states", 3)
                    target = (
                        profile.shape if index == 0 else iri("language-" + str(index))
                    )
                    if index:
                        definitions.append(
                            ShaclNodeShape(target, target_nodes=profile.concepts)
                        )
                    uses.append(
                        ComponentUse(
                            target,
                            str(component),
                            ((str(parameter), Literal(tag)), *fallbacks),
                        )
                    )
                translated.append("language-coverage")
            elif "language-coverage" in selected:
                unknown("language-coverage", "No required language was selected")
        source = compile_shapes(tuple(definitions), budget=operation)
        if catalog:
            operation.consume("states")
            components = compile_components(
                source, tuple(catalog), tuple(uses), budget=operation
            )
            retain_report(components.report, operation, issues)
            children.append(components.report)
            source = components.source
    except LimitExceeded as error:
        source = None
        issues.exhausted(str(error))
    report = Report(
        "skos-profile-translation",
        len(translated),
        tuple(issues),
        requested=max(1, requested),
        complete=source is not None
        and not issues
        and all(child.verified for child in children),
        children=tuple(children),
        regime="explicit-named-plain-skos-label-policy",
    )
    return SkosProfileShapes(
        qualified,
        tuple(definitions),
        source,
        components,
        tuple(translated),
        tuple(untranslated),
        tuple(assumptions),
        report,
    )
