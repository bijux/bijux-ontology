package org.bijux.ontology.axioms;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.nio.file.Path;
import java.util.*;
import java.util.stream.Stream;
import org.bijux.ontology.backend.WorkerProtocol;
import org.bijux.ontology.documents.NativeDocuments;
import org.semanticweb.owlapi.model.*;

/** Finite typed observations of maintained objects; no inference or authoring AST. */
public final class AxiomProjection implements OWLObjectVisitorEx<Map<String,Object>> {
    private static final class UnsupportedProjection extends RuntimeException {}
    private static final class ProjectionLimit extends RuntimeException {}
    private final int maximum;
    private final int depthCap;
    private final int textCap;
    private int used;
    private int depth;

    private AxiomProjection(int maximum, int depthCap, int textCap) {
        this.maximum = maximum; this.depthCap = depthCap; this.textCap = textCap;
    }

    private String text(String value) {
        if (value.length() > textCap) throw new ProjectionLimit();
        return value;
    }

    private Map<String,Object> node(String kind) {
        if (used >= maximum) throw new ProjectionLimit();
        used++;
        Map<String,Object> result = new LinkedHashMap<>();
        result.put("kind", kind);
        return result;
    }

    private Map<String,Object> project(OWLObject value) {
        if (++depth > depthCap) { depth--; throw new ProjectionLimit(); }
        try { return value.accept(this); }
        finally { depth--; }
    }

    private List<Map<String,Object>> values(Stream<? extends OWLObject> values) {
        List<Map<String,Object>> result = new ArrayList<>();
        Iterator<? extends OWLObject> iterator = values.iterator();
        while (iterator.hasNext()) result.add(project(iterator.next()));
        return result;
    }

    private Map<String,Object> entity(OWLEntity value) {
        Map<String,Object> result = node("entity");
        String role;
        if (value instanceof OWLClass) role = "class";
        else if (value instanceof OWLObjectProperty) role = "object_property";
        else if (value instanceof OWLDataProperty) role = "data_property";
        else if (value instanceof OWLAnnotationProperty) role = "annotation_property";
        else if (value instanceof OWLDatatype) role = "datatype";
        else if (value instanceof OWLNamedIndividual) role = "named_individual";
        else throw new UnsupportedProjection();
        result.put("role", role); result.put("iri", text(value.getIRI().toString()));
        return result;
    }

    private Map<String,Object> axiom(String kind, OWLAxiom value) {
        Map<String,Object> result = node(kind);
        result.put("annotations", values(value.annotations()));
        return result;
    }

    @Override public <T> Map<String,Object> doDefault(T value) {
        throw new UnsupportedProjection();
    }
    @Override public Map<String,Object> visit(OWLClass value) { return entity(value); }
    @Override public Map<String,Object> visit(OWLDatatype value) { return entity(value); }
    @Override public Map<String,Object> visit(OWLNamedIndividual value) { return entity(value); }
    @Override public Map<String,Object> visit(OWLAnnotationProperty value) { return entity(value); }
    @Override public Map<String,Object> visit(IRI value) {
        Map<String,Object> result = node("iri"); result.put("iri", text(value.toString())); return result;
    }
    @Override public Map<String,Object> visit(OWLLiteral value) {
        Map<String,Object> result = node("literal");
        result.put("lexical", text(value.getLiteral()));
        result.put("datatype", text(value.getDatatype().getIRI().toString()));
        result.put("language", value.hasLang() ? text(value.getLang()) : null);
        return result;
    }
    @Override public Map<String,Object> visit(OWLObjectProperty value) {
        Map<String,Object> result = node("object_property");
        result.put("iri", text(value.getIRI().toString())); result.put("inverse", false); return result;
    }
    @Override public Map<String,Object> visit(OWLObjectInverseOf value) {
        Map<String,Object> result = node("object_property");
        result.put("iri", text(value.getNamedProperty().getIRI().toString()));
        result.put("inverse", true); return result;
    }
    @Override public Map<String,Object> visit(OWLDataProperty value) {
        Map<String,Object> result = node("data_property");
        result.put("iri", text(value.getIRI().toString())); return result;
    }
    @Override public Map<String,Object> visit(OWLAnnotation value) {
        Map<String,Object> result = node("annotation");
        result.put("property", text(value.getProperty().getIRI().toString()));
        result.put("value", project(value.getValue()));
        result.put("annotations", values(value.annotations())); return result;
    }
    @Override public Map<String,Object> visit(OWLFacetRestriction value) {
        Map<String,Object> result = node("facet");
        result.put("iri", text(value.getFacet().getIRI().toString()));
        result.put("value", project(value.getFacetValue())); return result;
    }
    @Override public Map<String,Object> visit(OWLObjectIntersectionOf value) {
        Map<String,Object> result = node("object_junction");
        result.put("operator", "intersection");
        result.put("operands", values(value.operands()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLObjectUnionOf value) {
        Map<String,Object> result = node("object_junction");
        result.put("operator", "union");
        result.put("operands", values(value.operands()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLObjectComplementOf value) {
        Map<String,Object> result = node("object_complement");
        result.put("operand", project(value.getOperand()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLObjectSomeValuesFrom value) {
        Map<String,Object> result = node("object_quantifier");
        result.put("quantifier", "some");
        result.put("property", project(value.getProperty()));
        result.put("filler", project(value.getFiller()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLObjectAllValuesFrom value) {
        Map<String,Object> result = node("object_quantifier");
        result.put("quantifier", "all");
        result.put("property", project(value.getProperty()));
        result.put("filler", project(value.getFiller()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLObjectMinCardinality value) {
        Map<String,Object> result = node("object_cardinality");
        result.put("operator", "min");
        result.put("count", value.getCardinality());
        result.put("property", project(value.getProperty()));
        result.put("filler", project(value.getFiller()));
        result.put("qualified", value.isQualified());
        return result;
    }
    @Override public Map<String,Object> visit(OWLObjectMaxCardinality value) {
        Map<String,Object> result = node("object_cardinality");
        result.put("operator", "max");
        result.put("count", value.getCardinality());
        result.put("property", project(value.getProperty()));
        result.put("filler", project(value.getFiller()));
        result.put("qualified", value.isQualified());
        return result;
    }
    @Override public Map<String,Object> visit(OWLObjectExactCardinality value) {
        Map<String,Object> result = node("object_cardinality");
        result.put("operator", "exact");
        result.put("count", value.getCardinality());
        result.put("property", project(value.getProperty()));
        result.put("filler", project(value.getFiller()));
        result.put("qualified", value.isQualified());
        return result;
    }
    @Override public Map<String,Object> visit(OWLObjectHasValue value) {
        Map<String,Object> result = node("object_value");
        result.put("property", project(value.getProperty()));
        result.put("value", project(value.getFiller()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLObjectOneOf value) {
        Map<String,Object> result = node("object_enumeration");
        result.put("values", values(value.individuals()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLDataIntersectionOf value) {
        Map<String,Object> result = node("data_junction");
        result.put("operator", "intersection");
        result.put("operands", values(value.operands()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLDataUnionOf value) {
        Map<String,Object> result = node("data_junction");
        result.put("operator", "union");
        result.put("operands", values(value.operands()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLDataComplementOf value) {
        Map<String,Object> result = node("data_complement");
        result.put("operand", project(value.getDataRange()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLDataSomeValuesFrom value) {
        Map<String,Object> result = node("data_quantifier");
        result.put("quantifier", "some");
        result.put("property", project(value.getProperty()));
        result.put("filler", project(value.getFiller()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLDataAllValuesFrom value) {
        Map<String,Object> result = node("data_quantifier");
        result.put("quantifier", "all");
        result.put("property", project(value.getProperty()));
        result.put("filler", project(value.getFiller()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLDataMinCardinality value) {
        Map<String,Object> result = node("data_cardinality");
        result.put("operator", "min");
        result.put("count", value.getCardinality());
        result.put("property", project(value.getProperty()));
        result.put("filler", project(value.getFiller()));
        result.put("qualified", value.isQualified());
        return result;
    }
    @Override public Map<String,Object> visit(OWLDataMaxCardinality value) {
        Map<String,Object> result = node("data_cardinality");
        result.put("operator", "max");
        result.put("count", value.getCardinality());
        result.put("property", project(value.getProperty()));
        result.put("filler", project(value.getFiller()));
        result.put("qualified", value.isQualified());
        return result;
    }
    @Override public Map<String,Object> visit(OWLDataExactCardinality value) {
        Map<String,Object> result = node("data_cardinality");
        result.put("operator", "exact");
        result.put("count", value.getCardinality());
        result.put("property", project(value.getProperty()));
        result.put("filler", project(value.getFiller()));
        result.put("qualified", value.isQualified());
        return result;
    }
    @Override public Map<String,Object> visit(OWLDataHasValue value) {
        Map<String,Object> result = node("data_value");
        result.put("property", project(value.getProperty()));
        result.put("value", project(value.getFiller()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLDataOneOf value) {
        Map<String,Object> result = node("data_enumeration");
        result.put("values", values(value.values()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLObjectHasSelf value) {
        Map<String,Object> result = node("object_self");
        result.put("property", project(value.getProperty()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLDatatypeRestriction value) {
        Map<String,Object> result = node("datatype_restriction");
        result.put("datatype", project(value.getDatatype()));
        result.put("facets", values(value.facetRestrictions()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLDeclarationAxiom value) {
        Map<String,Object> result = axiom("declaration", value);
        result.put("entity", entity(value.getEntity()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLSubClassOfAxiom value) {
        Map<String,Object> result = axiom("subclass", value);
        result.put("sub_class", project(value.getSubClass()));
        result.put("super_class", project(value.getSuperClass()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLEquivalentClassesAxiom value) {
        Map<String,Object> result = axiom("class_relation", value);
        result.put("relation", "equivalent");
        result.put("classes", values(value.classExpressions()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLDisjointClassesAxiom value) {
        Map<String,Object> result = axiom("class_relation", value);
        result.put("relation", "disjoint");
        result.put("classes", values(value.classExpressions()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLClassAssertionAxiom value) {
        Map<String,Object> result = axiom("class_assertion", value);
        result.put("individual", project(value.getIndividual()));
        result.put("expression", project(value.getClassExpression()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLDatatypeDefinitionAxiom value) {
        Map<String,Object> result = axiom("datatype_definition", value);
        result.put("datatype", project(value.getDatatype()));
        result.put("definition", project(value.getDataRange()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLObjectPropertyDomainAxiom value) {
        Map<String,Object> result = axiom("property_domain_range", value);
        result.put("role", "domain");
        result.put("property", project(value.getProperty()));
        result.put("expression", project(value.getDomain()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLObjectPropertyRangeAxiom value) {
        Map<String,Object> result = axiom("property_domain_range", value);
        result.put("role", "range");
        result.put("property", project(value.getProperty()));
        result.put("expression", project(value.getRange()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLSubObjectPropertyOfAxiom value) {
        Map<String,Object> result = axiom("subproperty", value);
        result.put("sub_property", project(value.getSubProperty()));
        result.put("super_property", project(value.getSuperProperty()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLDataPropertyDomainAxiom value) {
        Map<String,Object> result = axiom("property_domain_range", value);
        result.put("role", "domain");
        result.put("property", project(value.getProperty()));
        result.put("expression", project(value.getDomain()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLDataPropertyRangeAxiom value) {
        Map<String,Object> result = axiom("property_domain_range", value);
        result.put("role", "range");
        result.put("property", project(value.getProperty()));
        result.put("expression", project(value.getRange()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLSubDataPropertyOfAxiom value) {
        Map<String,Object> result = axiom("subproperty", value);
        result.put("sub_property", project(value.getSubProperty()));
        result.put("super_property", project(value.getSuperProperty()));
        return result;
    }
    @Override public Map<String,Object> visit(OWLAnnotationAssertionAxiom value) {
        Map<String,Object> result = axiom("annotation_assertion", value);
        result.put("property", text(value.getProperty().getIRI().toString()));
        result.put("subject", project(value.getSubject()));
        result.put("value", project(value.getValue()));
        return result;
    }

    /** Visit only actual owned ontologies; every selected axiom has one outcome. */
    public static Map<String,Object> evaluate(JsonNode request, Path job,
            Map<String,Object> result) throws Exception {
        int cap = WorkerProtocol.positive(request.path("max_bytes"));
        int maximum = WorkerProtocol.positive(request.path("max_nodes"));
        int resources = WorkerProtocol.positive(request.path("max_resources"));
        int issues = WorkerProtocol.positive(request.path("max_issues"));
        AxiomProjection visitor = new AxiomProjection(maximum,
            WorkerProtocol.positive(request.path("projection_depth")), cap);
        List<Map<String,Object>> documents = new ArrayList<>();
        List<Map<String,Object>> axioms = new ArrayList<>();
        List<Map<String,Object>> exclusions = new ArrayList<>();
        List<Map<String,Object>> changes = new ArrayList<>();
        result.put("documents", documents); result.put("axioms", axioms);
        result.put("exclusions", exclusions); result.put("changes", changes);
        result.put("qualified", false);
        try {
            String kind = WorkerProtocol.text(request.path("input_kind"), 32);
            NativeDocuments.Loaded input;
            if (kind.equals("native")) input = NativeDocuments.loadRequest(request, job);
            else if (kind.equals("rdf")) input = NativeDocuments.loadRdfRequest(request, job);
            else throw new IOException("Unsupported axiom input kind");
            result.put("input_kind", kind);
            result.put("loaders", input.loaders);
            input.qualifyOriginals(changes, cap, issues);
            Set<String> selected = null;
            JsonNode selection = request.path("axiom_types");
            if (!selection.isNull()) {
                if (!selection.isArray() || selection.size() > AxiomType.AXIOM_TYPES.size())
                    throw new IOException("Invalid axiom selection");
                selected = new HashSet<>();
                for (JsonNode value : selection) {
                    String name = WorkerProtocol.text(value, cap);
                    if (!AxiomType.isAxiomType(name) || !selected.add(name))
                        throw new IOException("Invalid axiom family");
                }
            }
            for (OWLOntology ontology : input.ontology.getOWLOntologyManager().getOntologies()) {
                if (documents.size() >= resources) throw new ProjectionLimit();
                Integer owner = input.owners.get(ontology.getOWLOntologyManager()
                    .getOntologyDocumentIRI(ontology).toString());
                if (owner == null) throw new IOException("Unowned axiom document");
                Map<String,Object> document = visitor.node("ontology");
                document.put("document", owner);
                document.put("ontology_iri", ontology.getOntologyID().getOntologyIRI()
                    .map(iri -> visitor.text(iri.toString())).orElse(null));
                document.put("version_iri", ontology.getOntologyID().getVersionIRI()
                    .map(iri -> visitor.text(iri.toString())).orElse(null));
                List<String> imports = new ArrayList<>();
                for (OWLImportsDeclaration declaration : ontology.getImportsDeclarations()) {
                    if (imports.size() >= resources) throw new ProjectionLimit();
                    visitor.used++; if (visitor.used > maximum) throw new ProjectionLimit();
                    imports.add(visitor.text(declaration.getIRI().toString()));
                }
                document.put("imports", imports);
                document.put("axioms", ontology.getAxiomCount());
                document.put("annotations", visitor.values(ontology.annotations()));
                documents.add(document);
                Map<String,Integer> omitted = new TreeMap<>();
                Iterator<OWLAxiom> iterator = ontology.axioms().iterator();
                while (iterator.hasNext()) {
                    OWLAxiom axiom = iterator.next();
                    String name = axiom.getAxiomType().getName();
                    Map<String,Object> record = visitor.node("axiom");
                    if (selected != null && !selected.contains(name)) {
                        omitted.put(name, omitted.getOrDefault(name, 0) + 1);
                        continue;
                    }
                    record.put("document", owner); record.put("type", name);
                    try { record.put("value", visitor.project(axiom)); }
                    catch (UnsupportedProjection unsupported) { record.put("unsupported", true); }
                    axioms.add(record);
                }
                for (Map.Entry<String,Integer> entry : omitted.entrySet()) {
                    Map<String,Object> exclusion = visitor.node("exclusion");
                    exclusion.put("document", owner); exclusion.put("type", entry.getKey());
                    exclusion.put("count", entry.getValue()); exclusions.add(exclusion);
                }
            }
            result.put("qualified", true);
        } catch (ProjectionLimit | WorkerProtocol.OutputLimit exhausted) {
            result.put("status", "exhausted");
        } catch (Exception unavailable) {
            Throwable cause = unavailable;
            while (cause.getCause() != null && cause.getCause() != cause) cause = cause.getCause();
            if (cause instanceof WorkerProtocol.OutputLimit) result.put("status", "exhausted");
        }
        return result;
    }
}
