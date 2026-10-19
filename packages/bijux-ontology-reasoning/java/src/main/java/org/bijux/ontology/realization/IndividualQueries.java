package org.bijux.ontology.realization;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.util.*;
import org.bijux.ontology.backend.QualifiedReasoning;
import org.bijux.ontology.backend.WorkerProtocol;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.model.parameters.Imports;
import org.semanticweb.owlapi.reasoner.*;

/** Independent named-individual queries over the already classified exact theory. */
public final class IndividualQueries {
    private IndividualQueries() {}
    private static final class RetentionLimit extends RuntimeException {}
    private static final class Inventory {
        final int maximum, nodes, cap;
        int members, visited;
        Inventory(JsonNode request) throws IOException {
            maximum=WorkerProtocol.positive(request.path("max_results"));
            nodes=WorkerProtocol.positive(request.path("max_nodes"));
            cap=WorkerProtocol.positive(request.path("max_bytes"));
        }
        void visit() { if (visited++ >= nodes) throw new RetentionLimit(); }
        void member() { visit(); if (members++ >= maximum) throw new RetentionLimit(); }
    }

    private static IRI iri(JsonNode value, Inventory inventory) throws IOException {
        IRI iri=IRI.create(WorkerProtocol.text(value,inventory.cap));
        if (!iri.isAbsolute()) throw new IOException("Query IRI is not absolute");
        return iri;
    }

    private static void present(OWLEntity entity, QualifiedReasoning input) throws IOException {
        if (!entity.isBuiltIn() && !input.ontology.containsEntityInSignature(entity,Imports.INCLUDED))
            throw new IOException("Query entity lacks its exact selected signature role");
    }

    private static Map<String,Object> node(Node<? extends OWLEntity> source,
            List<Map<String,Object>> destination, Inventory inventory) throws IOException {
        inventory.visit();
        Map<String,Object> result=new LinkedHashMap<>();
        List<Map<String,Object>> members=new ArrayList<>();
        result.put("members",members);result.put("complete",false);destination.add(result);
        for (OWLEntity entity:source.getEntities()) {
            inventory.member();
            String name=entity.getIRI().toString();
            if (name.length()>inventory.cap) throw new WorkerProtocol.OutputLimit();
            Map<String,Object> value=new LinkedHashMap<>();
            value.put("kind","entity");value.put("role",entity.isOWLClass()?"class":"named_individual");
            value.put("iri",name);members.add(value);
        }
        result.put("complete",true);
        return result;
    }

    private static void nodes(NodeSet<? extends OWLEntity> source,
            List<Map<String,Object>> destination, Inventory inventory) throws IOException {
        for (Node<? extends OWLEntity> group:source.getNodes()) node(group,destination,inventory);
    }

    /** The data getter is not complete; only this explicit finite fragment is enumerated. */
    private static Set<OWLLiteral> literals(QualifiedReasoning input, Inventory inventory)
            throws IOException {
        Set<OWLLiteral> values=new LinkedHashSet<>();
        for (OWLOntology ontology:input.ontology.getImportsClosure()) {
            inventory.visit();
            for (OWLLogicalAxiom axiom:ontology.getLogicalAxioms()) {
                inventory.visit();
                if (axiom instanceof SWRLRule || axiom instanceof OWLDatatypeDefinitionAxiom)
                    throw new IOException("Data values require the finite positive assertion fragment");
                if (axiom.getDataPropertiesInSignature().isEmpty()) continue;
                for (OWLDataProperty property:axiom.getDataPropertiesInSignature()) {
                    inventory.visit();
                    if (property.isOWLTopDataProperty())
                        throw new IOException("Universal data-property values are not finite");
                }
                if (axiom instanceof OWLDataPropertyAssertionAxiom) {
                    OWLDataPropertyAssertionAxiom assertion=(OWLDataPropertyAssertionAxiom)axiom;
                    if (!assertion.getSubject().isNamed())
                        throw new IOException("Anonymous data assertion subjects are outside the selected fragment");
                    OWLLiteral value=assertion.getObject();
                    if (value.getLiteral().length()>inventory.cap
                            || value.getDatatype().getIRI().toString().length()>inventory.cap
                            || value.getLang().length()>inventory.cap) throw new WorkerProtocol.OutputLimit();
                    inventory.visit();values.add(value);
                } else if (!(axiom instanceof OWLSubDataPropertyOfAxiom)
                        && !(axiom instanceof OWLEquivalentDataPropertiesAxiom))
                    throw new IOException("A data-bearing axiom is outside the finite assertion fragment");
            }
        }
        return values;
    }

    private static Map<String,Object> literal(OWLLiteral value) {
        Map<String,Object> result=new LinkedHashMap<>();
        result.put("kind","literal");result.put("lexical",value.getLiteral());
        result.put("datatype",value.getDatatype().getIRI().toString());
        result.put("language",value.hasLang()?value.getLang():null);
        return result;
    }

    /** Never substitute incomplete property getters or manufacture existential names. */
    public static Map<String,Object> evaluatePrepared(JsonNode request,
            QualifiedReasoning input, Map<String,Object> result) throws Exception {
        List<Map<String,Object>> answers=new ArrayList<>();result.put("individual_queries",answers);
        boolean consistent=input.reasoner.isConsistent();result.put("consistent",consistent);
        if (!consistent) return result;
        Inventory inventory=new Inventory(request);
        JsonNode queries=request.path("queries");
        if (!queries.isArray() || queries.size()<1 || queries.size()>inventory.nodes)
            throw new IOException("A finite nonempty query inventory is required");
        if (input.reasoner.getFreshEntityPolicy()!=FreshEntityPolicy.DISALLOW)
            throw new IOException("Fresh entity policy differs from the qualified profile");
        OWLDataFactory factory=input.ontology.getOWLOntologyManager().getOWLDataFactory();
        int index=0;
        for (JsonNode query:queries) {
            Map<String,Object> answer=new LinkedHashMap<>();
            List<Map<String,Object>> groups=new ArrayList<>(), values=new ArrayList<>();
            answer.put("index",index++);answer.put("groups",groups);answer.put("values",values);
            answer.put("complete",false);answer.put("exhausted",false);answer.put("error",null);
            answer.put("node_policy",input.reasoner.getIndividualNodeSetPolicy().toString());answers.add(answer);
            try {
                inventory.visit();
                String family=WorkerProtocol.text(query.path("family"),32);
                IRI focus=iri(query.path("focus").path("iri"),inventory);
                String role=WorkerProtocol.text(query.path("focus").path("role"),32);
                if (family.equals("instances")) {
                    if (!role.equals("class") || !query.path("direct").isBoolean())
                        throw new IOException("Instances require a named class and explicit depth");
                    OWLClass cls=factory.getOWLClass(focus);present(cls,input);
                    nodes(input.reasoner.getInstances(cls,query.path("direct").booleanValue()),groups,inventory);
                } else {
                    if (!role.equals("named_individual")) throw new IOException("A named individual is required");
                    OWLNamedIndividual individual=factory.getOWLNamedIndividual(focus);present(individual,input);
                    switch (family) {
                        case "types":
                            if (!query.path("direct").isBoolean()) throw new IOException("Types require explicit depth");
                            nodes(input.reasoner.getTypes(individual,query.path("direct").booleanValue()),groups,inventory);
                            break;
                        case "same": node(input.reasoner.getSameIndividuals(individual),groups,inventory);break;
                        case "different": nodes(input.reasoner.getDifferentIndividuals(individual),groups,inventory);break;
                        case "object_values": {
                            JsonNode property=query.path("property");
                            if (!"object_property".equals(WorkerProtocol.text(property.path("kind"),32))
                                    || !property.path("inverse").isBoolean())
                                throw new IOException("An explicit object property is required");
                            OWLObjectProperty named=factory.getOWLObjectProperty(iri(property.path("iri"),inventory));present(named,input);
                            OWLObjectPropertyExpression expression=property.path("inverse").booleanValue()?named.getInverseProperty():named;
                            nodes(input.reasoner.getObjectPropertyValues(individual,expression),groups,inventory);break;
                        }
                        case "data_values": {
                            JsonNode property=query.path("property");
                            if (!"data_property".equals(WorkerProtocol.text(property.path("kind"),32)))
                                throw new IOException("An explicit data property is required");
                            OWLDataProperty named=factory.getOWLDataProperty(iri(property.path("iri"),inventory));present(named,input);
                            if (named.isOWLTopDataProperty()) throw new IOException("Universal data values are not finite");
                            if (!input.reasoner.isEntailmentCheckingSupported(AxiomType.DATA_PROPERTY_ASSERTION))
                                throw new IOException("Native data assertion entailment is unsupported");
                            for (OWLLiteral value:literals(input,inventory)) {
                                inventory.visit();
                                if (input.reasoner.isEntailed(factory.getOWLDataPropertyAssertionAxiom(named,individual,value))) {
                                    inventory.member();values.add(literal(value));
                                }
                            }
                            break;
                        }
                        default: throw new IOException("Unsupported individual query family");
                    }
                }
                answer.put("complete",true);
            } catch (RetentionLimit | TimeOutException | WorkerProtocol.OutputLimit exhausted) {
                answer.put("error",exhausted.getClass().getSimpleName());answer.put("exhausted",true);
                result.put("status","exhausted");break;
            } catch (OutOfMemoryError | StackOverflowError exhausted) {
                answer.put("error",exhausted.getClass().getSimpleName());answer.put("exhausted",true);
                result.put("status","exhausted");break;
            } catch (Exception unsupported) {
                answer.put("error",unsupported.getClass().getSimpleName());
            }
        }
        return result;
    }
}
