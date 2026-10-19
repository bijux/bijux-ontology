package org.bijux.ontology.models;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.nio.file.Path;
import java.util.*;
import org.bijux.ontology.axioms.AxiomProjection;
import org.bijux.ontology.backend.QualifiedReasoning;
import org.bijux.ontology.backend.WorkerProtocol;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.model.parameters.Imports;
import org.semanticweb.owlapi.reasoner.TimeOutException;

/** Actual class-expression questions from finite original model occurrences. */
public final class ModelCompatibility {
    private ModelCompatibility() {}

    private static final class Questions {
        final QualifiedReasoning input;
        final OWLDataFactory factory;
        final int cap, maximum;
        int used;
        Questions(QualifiedReasoning input, int cap, int maximum) {
            this.input=input; this.factory=input.ontology.getOWLOntologyManager().getOWLDataFactory();
            this.cap=cap; this.maximum=maximum;
        }
        void tick() throws IOException { if (++used > maximum) throw new WorkerProtocol.OutputLimit(); }
        IRI iri(JsonNode value) throws IOException {
            tick(); IRI iri=IRI.create(WorkerProtocol.text(value, cap));
            if (!iri.isAbsolute()) throw new IOException("Model query IRI is not absolute");
            return iri;
        }
        void signature(OWLObject value) throws IOException {
            for (OWLEntity entity : value.getSignature()) {
                tick();
                if (!entity.isBuiltIn() && !input.ontology.containsEntityInSignature(entity, Imports.INCLUDED))
                    throw new IOException("Model entity lacks selected source signature");
            }
        }
        OWLClassExpression conjunction(Collection<OWLClassExpression> values) {
            if (values.isEmpty()) return factory.getOWLThing();
            if (values.size()==1) return values.iterator().next();
            return factory.getOWLObjectIntersectionOf(values);
        }
        OWLClassExpression types(JsonNode values) throws IOException {
            if (!values.isArray() || values.size()>maximum) throw new IOException("Invalid model type inventory");
            Set<OWLClassExpression> types=new LinkedHashSet<>();
            for(JsonNode value:values) types.add(factory.getOWLClass(iri(value)));
            OWLClassExpression result=conjunction(types); signature(result); return result;
        }
        OWLClassExpression endpoint(IRI iri, boolean reverse, boolean parent) throws Exception {
            OWLOntology ontology=input.ontology;
            List<OWLClassExpression> constraints=new ArrayList<>();
            OWLObjectProperty object=factory.getOWLObjectProperty(iri);
            OWLDataProperty data=factory.getOWLDataProperty(iri);
            if (object.isBuiltIn() || ontology.containsEntityInSignature(object,Imports.INCLUDED)) {
                OWLObjectPropertyExpression selected=reverse ? object.getInverseProperty() : object;
                Set<OWLObjectPropertyExpression> family=new LinkedHashSet<>();family.add(selected);
                family.addAll(input.reasoner.getEquivalentObjectProperties(selected).getEntities());
                family.addAll(input.reasoner.getSuperObjectProperties(selected,false).getFlattened());
                if (family.contains(factory.getOWLBottomObjectProperty())) return factory.getOWLNothing();
                for(OWLOntology module:ontology.getImportsClosure()) {
                    for(OWLObjectPropertyDomainAxiom axiom:module.getAxioms(AxiomType.OBJECT_PROPERTY_DOMAIN)) {
                        tick();
                        if(family.contains(parent ? axiom.getProperty() : axiom.getProperty().getInverseProperty())) constraints.add(axiom.getDomain());
                    }
                    for(OWLObjectPropertyRangeAxiom axiom:module.getAxioms(AxiomType.OBJECT_PROPERTY_RANGE)) {
                        tick();
                        if(family.contains(parent ? axiom.getProperty().getInverseProperty() : axiom.getProperty())) constraints.add(axiom.getRange());
                    }
                }
            } else if (data.isBuiltIn() || ontology.containsEntityInSignature(data,Imports.INCLUDED)) {
                if (reverse || !parent) throw new IOException("A data-property literal endpoint is not a resource class");
                Set<OWLDataProperty> family=new LinkedHashSet<>();family.add(data);
                family.addAll(input.reasoner.getEquivalentDataProperties(data).getEntities());
                family.addAll(input.reasoner.getSuperDataProperties(data,false).getFlattened());
                if (family.contains(factory.getOWLBottomDataProperty())) return factory.getOWLNothing();
                for(OWLOntology module:ontology.getImportsClosure())
                    for(OWLDataPropertyDomainAxiom axiom:module.getAxioms(AxiomType.DATA_PROPERTY_DOMAIN)) {
                        tick();if(family.contains(axiom.getProperty())) constraints.add(axiom.getDomain());
                    }
            } else throw new IOException("Property has no selected logical object/data role");
            if(constraints.size()>maximum) throw new WorkerProtocol.OutputLimit();
            return conjunction(constraints);
        }
        OWLClassExpression actual(JsonNode query) throws Exception {
            List<OWLClassExpression> parts=new ArrayList<>();parts.add(types(query.path("types")));
            JsonNode assumptions=query.path("assumptions");
            if(!assumptions.isArray() || assumptions.size()>maximum) throw new IOException("Invalid finite emission state");
            for(JsonNode value:assumptions) {
                tick();if(!value.isArray() || value.size()!=2 || !value.get(1).isBoolean()) throw new IOException("Invalid emission field endpoint");
                parts.add(endpoint(iri(value.get(0)),value.get(1).booleanValue(),true));
            }
            OWLClassExpression result=conjunction(parts); signature(result);return result;
        }
    }

    /** Consistency, source fidelity and actual finite question coverage precede acceptance. */
    public static Map<String,Object> evaluate(JsonNode request, Path job, Map<String,Object> result) throws Exception {
        int cap=WorkerProtocol.positive(request.path("max_bytes"));
        int maximum=WorkerProtocol.positive(request.path("max_nodes"));
        int depth=WorkerProtocol.positive(request.path("query_depth"));
        List<Map<String,Object>> answers=new ArrayList<>();result.put("answers",answers);
        try(QualifiedReasoning input=QualifiedReasoning.open(request,job,result)) {
            if(input==null)return result;
            boolean consistent=input.reasoner.isConsistent();result.put("consistent",consistent);
            if(!consistent)return result;
            JsonNode queries=request.path("queries");
            if(!queries.isArray() || queries.isEmpty() || queries.size()>maximum)throw new IOException("Invalid model question inventory");
            Questions questions=new Questions(input,cap,maximum);
            for(int index=0;index<queries.size();index++) {
                Map<String,Object> answer=new LinkedHashMap<>();answer.put("index",index);
                answer.put("entailed",null);answer.put("satisfiable",null);answer.put("error",null);
                answer.put("query",null);
                try {
                    questions.tick();JsonNode query=queries.get(index);
                    if(!query.path("unsupported").isNull())throw new IOException("Unsupported declaration context");
                    String kind=WorkerProtocol.text(query.path("kind"),64);
                    OWLClassExpression actual=questions.actual(query),required=questions.factory.getOWLThing();
                    if(kind.equals("required_subsumption")) {
                        if(!query.path("reverse").isBoolean())throw new IOException("Invalid field direction");
                        String endpoint=WorkerProtocol.text(query.path("endpoint"),16);
                        if(!Set.of("parent","value").contains(endpoint))throw new IOException("Invalid required endpoint");
                        required=questions.endpoint(questions.iri(query.path("predicate")),query.path("reverse").booleanValue(),endpoint.equals("parent"));
                        questions.signature(required);
                    } else if(!Set.of("possible_satisfiability","coemission_contradiction").contains(kind))throw new IOException("Unknown model question");
                    OWLSubClassOfAxiom axiom=questions.factory.getOWLSubClassOfAxiom(actual,required);
                    Map<String,Object> projected=AxiomProjection.projectChanges(List.of(axiom),maximum,depth,cap);
                    answer.put("query",projected);
                    if(!((List<?>)projected.get("unsupported")).isEmpty())throw new IOException("Unrepresented class expression");
                    if(kind.equals("required_subsumption"))answer.put("entailed",input.reasoner.isEntailed(axiom));
                    else answer.put("satisfiable",input.reasoner.isSatisfiable(actual));
                } catch(TimeOutException | WorkerProtocol.OutputLimit exhausted) {throw exhausted;}
                catch(Exception unsupported) {answer.put("error",unsupported.getClass().getSimpleName());}
                answers.add(answer);
            }
        } catch(TimeOutException | WorkerProtocol.OutputLimit exhausted) {result.put("status","exhausted");}
        catch(OutOfMemoryError | StackOverflowError exhausted) {result.put("status","exhausted");}
        catch(Exception unsupported) {result.put("qualified",false);result.put("error",unsupported.getClass().getSimpleName());}
        return result;
    }
}
