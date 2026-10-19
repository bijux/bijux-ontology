package org.bijux.ontology.axioms;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.util.*;
import org.bijux.ontology.backend.WorkerProtocol;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.vocab.OWLFacet;

/** Decode the existing finite projection model with maintained OWL factories. */
public final class AxiomConstruction {
    private final OWLDataFactory factory;
    private final int maximum, depth, cap;
    private int used;
    public AxiomConstruction(OWLDataFactory factory, int maximum, int depth, int cap) {
        this.factory = factory; this.maximum = maximum; this.depth = depth; this.cap = cap;
    }
    private String text(JsonNode value) throws IOException { return WorkerProtocol.text(value, cap); }
    private IRI iri(JsonNode value) throws IOException {
        IRI result = IRI.create(text(value));
        if (!result.isAbsolute()) throw new IOException("Constructor IRI is not absolute");
        return result;
    }
    private List<OWLObject> nodes(JsonNode values, int level) throws IOException {
        if (!values.isArray() || values.size() > maximum) throw new IOException("Invalid constructor inventory");
        List<OWLObject> result = new ArrayList<>();
        for (JsonNode value : values) result.add(node(value, level));
        return result;
    }
    private <T> List<T> typed(JsonNode values, int level, Class<T> kind) throws IOException {
        List<T> result = new ArrayList<>();
        for (OWLObject value : nodes(values, level)) {
            if (!kind.isInstance(value)) throw new IOException("Constructor role mismatch");
            result.add(kind.cast(value));
        }
        return result;
    }
    private <T> T one(JsonNode value, int level, Class<T> kind) throws IOException {
        OWLObject result = node(value, level);
        if (!kind.isInstance(result)) throw new IOException("Constructor role mismatch");
        return kind.cast(result);
    }
    private int count(JsonNode value) throws IOException {
        if (!value.isInt() || value.intValue() < 0) throw new IOException("Invalid cardinality");
        return value.intValue();
    }
    private boolean bool(JsonNode value) throws IOException {
        if (!value.isBoolean()) throw new IOException("Invalid constructor flag");
        return value.booleanValue();
    }
    private Set<OWLAnnotation> annotations(JsonNode values, int level) throws IOException {
        return new HashSet<>(typed(values, level, OWLAnnotation.class));
    }
    private OWLObject node(JsonNode value, int level) throws IOException {
        if (++used > maximum || level > depth) throw new WorkerProtocol.OutputLimit();
        if (!value.isObject()) throw new IOException("Invalid constructor node");
        int next = level + 1;
        String kind = text(value.path("kind"));
        switch (kind) {
            case "entity": {
                IRI name = iri(value.path("iri"));
                switch (text(value.path("role"))) {
                    case "class": return factory.getOWLClass(name);
                    case "object_property": return factory.getOWLObjectProperty(name);
                    case "data_property": return factory.getOWLDataProperty(name);
                    case "annotation_property": return factory.getOWLAnnotationProperty(name);
                    case "datatype": return factory.getOWLDatatype(name);
                    case "named_individual": return factory.getOWLNamedIndividual(name);
                    default: throw new IOException("Unknown constructor entity role");
                }
            }
            case "iri": return iri(value.path("iri"));
            case "literal": {
                String lexical = text(value.path("lexical"));
                IRI datatype = iri(value.path("datatype"));
                String language = value.path("language").isNull() ? null : text(value.path("language"));
                OWLLiteral literal = language == null ? factory.getOWLLiteral(lexical, factory.getOWLDatatype(datatype))
                    : factory.getOWLLiteral(lexical, language);
                if (!literal.getLiteral().equals(lexical) || !literal.getDatatype().getIRI().equals(datatype)
                        || !Objects.equals(language, literal.hasLang() ? literal.getLang() : null))
                    throw new IOException("Native literal construction would change supplied lexical metadata");
                return literal;
            }
            case "object_property": {
                OWLObjectPropertyExpression property = factory.getOWLObjectProperty(iri(value.path("iri")));
                return bool(value.path("inverse")) ? property.getInverseProperty() : property;
            }
            case "data_property": return factory.getOWLDataProperty(iri(value.path("iri")));
            case "annotation": return factory.getOWLAnnotation(factory.getOWLAnnotationProperty(iri(value.path("property"))),
                one(value.path("value"), next, OWLAnnotationValue.class), annotations(value.path("annotations"), next));
            case "facet": {
                OWLFacet facet = OWLFacet.getFacet(iri(value.path("iri")));
                if (facet == null) throw new IOException("Unknown datatype facet");
                return factory.getOWLFacetRestriction(facet, one(value.path("value"), next, OWLLiteral.class));
            }
            case "object_junction": {
                List<OWLClassExpression> values = typed(value.path("operands"), next, OWLClassExpression.class);
                if (values.size() < 2) throw new IOException("Incomplete junction");
                String operator = text(value.path("operator"));
                if (operator.equals("intersection")) return factory.getOWLObjectIntersectionOf(values);
                if (operator.equals("union")) return factory.getOWLObjectUnionOf(values);
                throw new IOException("Invalid junction operator");
            }
            case "data_junction": {
                List<OWLDataRange> values = typed(value.path("operands"), next, OWLDataRange.class);
                if (values.size() < 2) throw new IOException("Incomplete junction");
                String operator = text(value.path("operator"));
                if (operator.equals("intersection")) return factory.getOWLDataIntersectionOf(values);
                if (operator.equals("union")) return factory.getOWLDataUnionOf(values);
                throw new IOException("Invalid junction operator");
            }
            case "object_complement": return factory.getOWLObjectComplementOf(one(value.path("operand"), next, OWLClassExpression.class));
            case "data_complement": return factory.getOWLDataComplementOf(one(value.path("operand"), next, OWLDataRange.class));
            case "object_enumeration": return factory.getOWLObjectOneOf(typed(value.path("values"), next, OWLIndividual.class));
            case "data_enumeration": return factory.getOWLDataOneOf(typed(value.path("values"), next, OWLLiteral.class));
            case "datatype_restriction": return factory.getOWLDatatypeRestriction(one(value.path("datatype"), next, OWLDatatype.class), typed(value.path("facets"), next, OWLFacetRestriction.class));
            case "object_quantifier": {
                OWLObjectPropertyExpression property = one(value.path("property"), next, OWLObjectPropertyExpression.class);
                OWLClassExpression filler = one(value.path("filler"), next, OWLClassExpression.class);
                String quantifier = text(value.path("quantifier"));
                if (quantifier.equals("some")) return factory.getOWLObjectSomeValuesFrom(property, filler);
                if (quantifier.equals("all")) return factory.getOWLObjectAllValuesFrom(property, filler);
                throw new IOException("Invalid object quantifier");
            }
            case "data_quantifier": {
                OWLDataPropertyExpression property = one(value.path("property"), next, OWLDataPropertyExpression.class);
                OWLDataRange filler = one(value.path("filler"), next, OWLDataRange.class);
                String quantifier = text(value.path("quantifier"));
                if (quantifier.equals("some")) return factory.getOWLDataSomeValuesFrom(property, filler);
                if (quantifier.equals("all")) return factory.getOWLDataAllValuesFrom(property, filler);
                throw new IOException("Invalid data quantifier");
            }
            case "object_cardinality": {
                OWLObjectPropertyExpression property = one(value.path("property"), next, OWLObjectPropertyExpression.class);
                OWLClassExpression filler = one(value.path("filler"), next, OWLClassExpression.class);
                if (bool(value.path("qualified")) == filler.isOWLThing()) throw new IOException("Cardinality qualification disagrees with filler");
                int count = count(value.path("count"));
                switch (text(value.path("operator"))) {
                    case "min": return factory.getOWLObjectMinCardinality(count, property, filler);
                    case "max": return factory.getOWLObjectMaxCardinality(count, property, filler);
                    case "exact": return factory.getOWLObjectExactCardinality(count, property, filler);
                    default: throw new IOException("Invalid object cardinality operator");
                }
            }
            case "data_cardinality": {
                OWLDataPropertyExpression property = one(value.path("property"), next, OWLDataPropertyExpression.class);
                OWLDataRange filler = one(value.path("filler"), next, OWLDataRange.class);
                if (bool(value.path("qualified")) == filler.isTopDatatype()) throw new IOException("Cardinality qualification disagrees with filler");
                int count = count(value.path("count"));
                switch (text(value.path("operator"))) {
                    case "min": return factory.getOWLDataMinCardinality(count, property, filler);
                    case "max": return factory.getOWLDataMaxCardinality(count, property, filler);
                    case "exact": return factory.getOWLDataExactCardinality(count, property, filler);
                    default: throw new IOException("Invalid data cardinality operator");
                }
            }
            case "object_value": return factory.getOWLObjectHasValue(one(value.path("property"), next, OWLObjectPropertyExpression.class), one(value.path("value"), next, OWLIndividual.class));
            case "data_value": return factory.getOWLDataHasValue(one(value.path("property"), next, OWLDataPropertyExpression.class), one(value.path("value"), next, OWLLiteral.class));
            case "object_self": return factory.getOWLObjectHasSelf(one(value.path("property"), next, OWLObjectPropertyExpression.class));
            default: return axiomNode(value, next, kind);
        }
    }
    private OWLAxiom axiomNode(JsonNode value, int level, String kind) throws IOException {
        Set<OWLAnnotation> a = annotations(value.path("annotations"), level);
        switch (kind) {
            case "declaration": return factory.getOWLDeclarationAxiom(one(value.path("entity"), level, OWLEntity.class), a);
            case "subclass": return factory.getOWLSubClassOfAxiom(one(value.path("sub_class"), level, OWLClassExpression.class), one(value.path("super_class"), level, OWLClassExpression.class), a);
            case "class_relation": {
                List<OWLClassExpression> values = typed(value.path("classes"), level, OWLClassExpression.class);
                if (new HashSet<>(values).size() < 2) throw new IOException("Class relation needs distinct operands");
                switch (text(value.path("relation"))) {
                    case "equivalent": return factory.getOWLEquivalentClassesAxiom(values, a);
                    case "disjoint": return factory.getOWLDisjointClassesAxiom(values, a);
                    default: throw new IOException("Invalid class relation");
                }
            }
            case "class_assertion": return factory.getOWLClassAssertionAxiom(one(value.path("expression"), level, OWLClassExpression.class), one(value.path("individual"), level, OWLNamedIndividual.class), a);
            case "datatype_definition": return factory.getOWLDatatypeDefinitionAxiom(one(value.path("datatype"), level, OWLDatatype.class), one(value.path("definition"), level, OWLDataRange.class), a);
            case "property_domain_range": {
                OWLObject property = node(value.path("property"), level);
                String role = text(value.path("role"));
                if (property instanceof OWLObjectPropertyExpression) {
                    OWLClassExpression expression = one(value.path("expression"), level, OWLClassExpression.class);
                    if (role.equals("domain")) return factory.getOWLObjectPropertyDomainAxiom((OWLObjectPropertyExpression) property, expression, a);
                    if (role.equals("range")) return factory.getOWLObjectPropertyRangeAxiom((OWLObjectPropertyExpression) property, expression, a);
                } else if (property instanceof OWLDataPropertyExpression) {
                    if (role.equals("domain")) return factory.getOWLDataPropertyDomainAxiom((OWLDataPropertyExpression) property, one(value.path("expression"), level, OWLClassExpression.class), a);
                    if (role.equals("range")) return factory.getOWLDataPropertyRangeAxiom((OWLDataPropertyExpression) property, one(value.path("expression"), level, OWLDataRange.class), a);
                }
                throw new IOException("Invalid property endpoint role");
            }
            case "subproperty": {
                OWLObject sub = node(value.path("sub_property"), level), sup = node(value.path("super_property"), level);
                if (sub instanceof OWLObjectPropertyExpression && sup instanceof OWLObjectPropertyExpression) return factory.getOWLSubObjectPropertyOfAxiom((OWLObjectPropertyExpression) sub, (OWLObjectPropertyExpression) sup, a);
                if (sub instanceof OWLDataPropertyExpression && sup instanceof OWLDataPropertyExpression) return factory.getOWLSubDataPropertyOfAxiom((OWLDataPropertyExpression) sub, (OWLDataPropertyExpression) sup, a);
                throw new IOException("Subproperty roles disagree");
            }
            case "annotation_assertion": return factory.getOWLAnnotationAssertionAxiom(factory.getOWLAnnotationProperty(iri(value.path("property"))), one(value.path("subject"), level, IRI.class), one(value.path("value"), level, OWLAnnotationValue.class), a);
            default: throw new IOException("Unsupported constructor kind");
        }
    }
    /** Reuse the exact role-checked constructor for datatype obligations. */
    public OWLDataRange dataRange(JsonNode value) throws IOException { return one(value, 1, OWLDataRange.class); }
    public OWLAxiom axiom(JsonNode value) throws IOException { return one(value, 1, OWLAxiom.class); }
}
