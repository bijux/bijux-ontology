package org.bijux.ontology.relationships;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.nio.file.Path;
import java.util.*;
import org.bijux.ontology.backend.QualifiedReasoning;
import org.bijux.ontology.backend.WorkerProtocol;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.model.parameters.Imports;
import org.semanticweb.owlapi.reasoner.*;

/** Actual maintained named taxonomy and property query results, never materialization. */
public final class InferredRelationships {
    private InferredRelationships() {}
    private static final class RetentionLimit extends RuntimeException {}
    private static final class Inventory {
        int used;
        final int maximum, cap;
        Inventory(int maximum, int cap) { this.maximum=maximum; this.cap=cap; }
        void member() { if (used >= maximum) throw new RetentionLimit(); used++; }
        String text(String value) throws IOException {
            if (value.length() > cap) throw new WorkerProtocol.OutputLimit();
            return value;
        }
    }
    private static Map<String,Object> value(OWLObject object, Inventory inventory) throws IOException {
        Map<String,Object> result=new LinkedHashMap<>();
        if (object instanceof OWLClass) {
            result.put("kind", "class"); result.put("iri", inventory.text(((OWLClass)object).getIRI().toString()));
        } else if (object instanceof OWLObjectPropertyExpression) {
            OWLObjectPropertyExpression property=(OWLObjectPropertyExpression)object;
            result.put("kind", "object_property"); result.put("iri", inventory.text(property.getNamedProperty().getIRI().toString())); result.put("inverse", property.isAnonymous());
        } else if (object instanceof OWLDataProperty) {
            result.put("kind", "data_property"); result.put("iri", inventory.text(((OWLDataProperty)object).getIRI().toString()));
        } else throw new IOException("Unsupported native relationship member");
        return result;
    }
    private static OWLObject focus(JsonNode query, OWLOntology ontology, int cap) throws IOException {
        OWLDataFactory factory=ontology.getOWLOntologyManager().getOWLDataFactory();
        JsonNode raw=query.path("focus");IRI iri=IRI.create(WorkerProtocol.text(raw.path("iri"), cap));
        if (!iri.isAbsolute()) throw new IOException("Nonabsolute query focus");
        OWLEntity entity;
        switch (WorkerProtocol.text(raw.path("kind"),32)) {
            case "class": entity=factory.getOWLClass(iri); break;
            case "object_property": entity=factory.getOWLObjectProperty(iri); break;
            case "data_property": entity=factory.getOWLDataProperty(iri); break;
            default: throw new IOException("Unsupported query role");
        }
        if (!entity.isBuiltIn() && !ontology.containsEntityInSignature(entity, Imports.INCLUDED)) throw new IOException("Query focus lacks selected signature");
        if (entity instanceof OWLObjectProperty) {
            if (!raw.path("inverse").isBoolean()) throw new IOException("Missing inverse flag");
            return raw.path("inverse").booleanValue() ? ((OWLObjectProperty)entity).getInverseProperty() : entity;
        }
        return entity;
    }
    private static boolean asserted(OWLOntology ontology, OWLObject focus, OWLObject target, String relation) {
        OWLDataFactory f=ontology.getOWLOntologyManager().getOWLDataFactory();OWLAxiom axiom=null;
        if (focus instanceof OWLClass && target instanceof OWLClass) {
            OWLClass a=(OWLClass)focus,b=(OWLClass)target;
            if (relation.equals("super")) axiom=f.getOWLSubClassOfAxiom(a,b);
            else if (relation.equals("sub")) axiom=f.getOWLSubClassOfAxiom(b,a);
            else if (relation.equals("equivalent") && !a.equals(b))
                return ontology.equivalentClassesAxioms(a).anyMatch(x -> x.getClassExpressions().contains(b));
        } else if (focus instanceof OWLObjectPropertyExpression) {
            OWLObjectPropertyExpression p=(OWLObjectPropertyExpression)focus;
            if (target instanceof OWLClass) {
                if (relation.equals("domain")) axiom=f.getOWLObjectPropertyDomainAxiom(p,(OWLClass)target);
                else if (relation.equals("range")) axiom=f.getOWLObjectPropertyRangeAxiom(p,(OWLClass)target);
            } else if (target instanceof OWLObjectPropertyExpression) {
                OWLObjectPropertyExpression q=(OWLObjectPropertyExpression)target;
                if (relation.equals("super")) axiom=f.getOWLSubObjectPropertyOfAxiom(p,q);
                else if (relation.equals("sub")) axiom=f.getOWLSubObjectPropertyOfAxiom(q,p);
                else if (relation.equals("inverse")) axiom=f.getOWLInverseObjectPropertiesAxiom(p,q);
                else if (relation.equals("equivalent") && !p.equals(q)) return ontology.axioms(AxiomType.EQUIVALENT_OBJECT_PROPERTIES).anyMatch(x -> x.getProperties().contains(p) && x.getProperties().contains(q));
            }
        } else if (focus instanceof OWLDataProperty) {
            OWLDataProperty p=(OWLDataProperty)focus;
            if (target instanceof OWLClass && relation.equals("domain")) axiom=f.getOWLDataPropertyDomainAxiom(p,(OWLClass)target);
            else if (target instanceof OWLDataProperty) {
                OWLDataProperty q=(OWLDataProperty)target;
                if (relation.equals("super")) axiom=f.getOWLSubDataPropertyOfAxiom(p,q);
                else if (relation.equals("sub")) axiom=f.getOWLSubDataPropertyOfAxiom(q,p);
                else if (relation.equals("equivalent") && !p.equals(q)) return ontology.axioms(AxiomType.EQUIVALENT_DATA_PROPERTIES).anyMatch(x -> x.getProperties().contains(p) && x.getProperties().contains(q));
            }
        }
        return axiom != null && ontology.containsAxiomIgnoreAnnotations(axiom);
    }
    private static Collection<? extends Node<? extends OWLObject>> query(OWLReasoner reasoner, OWLObject focus, String relation, boolean direct) throws IOException {
        if (focus instanceof OWLClass) {
            OWLClass c=(OWLClass)focus;
            switch (relation) {
                case "super": return reasoner.getSuperClasses(c,direct).getNodes();
                case "sub": return reasoner.getSubClasses(c,direct).getNodes();
                case "equivalent": return Collections.singleton(reasoner.getEquivalentClasses(c));
                default: throw new IOException("Unsupported class relation");
            }
        }
        if (focus instanceof OWLObjectPropertyExpression) {
            OWLObjectPropertyExpression p=(OWLObjectPropertyExpression)focus;
            switch (relation) {
                case "super": return reasoner.getSuperObjectProperties(p,direct).getNodes();
                case "sub": return reasoner.getSubObjectProperties(p,direct).getNodes();
                case "equivalent": return Collections.singleton(reasoner.getEquivalentObjectProperties(p));
                case "inverse": return Collections.singleton(reasoner.getInverseObjectProperties(p));
                case "domain": return reasoner.getObjectPropertyDomains(p,direct).getNodes();
                case "range": return reasoner.getObjectPropertyRanges(p,direct).getNodes();
                default: throw new IOException("Unsupported object property relation");
            }
        }
        OWLDataProperty p=(OWLDataProperty)focus;
        switch (relation) {
            case "super": return reasoner.getSuperDataProperties(p,direct).getNodes();
            case "sub": return reasoner.getSubDataProperties(p,direct).getNodes();
            case "equivalent": return Collections.singleton(reasoner.getEquivalentDataProperties(p));
            case "domain": return reasoner.getDataPropertyDomains(p,direct).getNodes();
            default: throw new IOException("Unsupported data property relation");
        }
    }
    /** Retain actual complete and partial nodes with one cumulative result allowance. */
    public static Map<String,Object> evaluate(JsonNode request, Path job, Map<String,Object> result) throws Exception {
        result.put("answers", new ArrayList<Map<String,Object>>());
        try (QualifiedReasoning input = QualifiedReasoning.open(request, job, result)) {
            if (input == null) return result;
            return evaluatePrepared(request, input, result);
        } catch (TimeOutException | WorkerProtocol.OutputLimit exhausted) { result.put("status","exhausted"); }
        catch (OutOfMemoryError | StackOverflowError exhausted) { result.put("status","exhausted"); }
        catch (Exception unsupported) { result.put("qualified",false);result.put("error",unsupported.getClass().getSimpleName()); }
        return result;
    }

    /** Evaluate finite queries on an already qualified, caller-owned unchanged theory. */
    public static Map<String,Object> evaluatePrepared(JsonNode request, QualifiedReasoning input,
            Map<String,Object> result) throws Exception {
        int cap=WorkerProtocol.positive(request.path("max_bytes")), maximum=WorkerProtocol.positive(request.path("max_nodes"));
        Inventory inventory=new Inventory(WorkerProtocol.positive(request.path("max_results")),cap);
        List<Map<String,Object>> answers=new ArrayList<>();result.put("answers",answers);
        try {
            boolean consistent=input.reasoner.isConsistent();result.put("consistent",consistent);
            if (!consistent) return result;
            JsonNode queries=request.path("queries");
            if (!queries.isArray() || queries.size()==0 || queries.size()>maximum) throw new IOException("Invalid query inventory");
            OWLOntology ontology=input.ontology;
            for (int index=0;index<queries.size();index++) {
                Map<String,Object> answer=new LinkedHashMap<>();answer.put("index",index);answer.put("complete",false);answer.put("satisfiable",null);answer.put("error",null);
                List<Map<String,Object>> groups=new ArrayList<>();answer.put("groups",groups);answers.add(answer);
                try {
                    JsonNode q=queries.get(index);
                    if (q.isNull()) throw new IOException("Unsupported host query profile");
                    OWLObject focus=focus(q,ontology,cap);String relation=WorkerProtocol.text(q.path("relation"),32);
                    boolean direct=q.path("direct").isBoolean() && q.path("direct").booleanValue();
                    if (focus instanceof OWLClass) answer.put("satisfiable",input.reasoner.isSatisfiable((OWLClass)focus));
                    for (Node<? extends OWLObject> node:query(input.reasoner,focus,relation,direct)) {
                        if (groups.size()>=maximum) throw new RetentionLimit();
                        Map<String,Object> group=new LinkedHashMap<>();List<Map<String,Object>> members=new ArrayList<>();
                        group.put("members",members);group.put("top",node.isTopNode());group.put("bottom",node.isBottomNode());group.put("complete",false);groups.add(group);
                        for (OWLObject target:node.getEntities()) {
                            inventory.member();Map<String,Object> member=new LinkedHashMap<>();member.put("value",value(target,inventory));
                            List<Integer> owners=new ArrayList<>();
                            for (OWLOntology selected:ontology.getImportsClosure()) {
                                if (asserted(selected,focus,target,relation)) {
                                    if (owners.size()>=maximum) throw new RetentionLimit();
                                    String address=selected.getOWLOntologyManager().getOntologyDocumentIRI(selected).toString();
                                    // Native qualification already bound every actual loaded document.
                                    int owner=-1;
                                    JsonNode documents=request.path("documents");
                                    for (int document=0; document<documents.size(); document++) {
                                        JsonNode record=documents.get(document);
                                        String location=request.path("input_kind").asText().equals("native") ? record.path("effective_url").asText() : Path.of(record.path("path").asText()).toUri().toString();
                                        if (address.equals(location)) {
                                            if (owner>=0) throw new IOException("Ambiguous asserted document");
                                            owner=document;
                                        }
                                    }
                                    if (owner<0) throw new IOException("Unowned asserted document");
                                    owners.add(owner);
                                }
                            }
                            member.put("asserted_in",owners);members.add(member);
                        }
                        group.put("complete",true);
                    }
                    answer.put("complete",true);
                } catch (RetentionLimit exhausted) {
                    answer.put("error","ResultLimit");result.put("status","exhausted");break;
                } catch (TimeOutException | WorkerProtocol.OutputLimit exhausted) { throw exhausted; }
                catch (Exception unsupported) { answer.put("error",unsupported.getClass().getSimpleName()); }
            }

        } catch (TimeOutException | WorkerProtocol.OutputLimit exhausted) { result.put("status","exhausted"); }
        catch (OutOfMemoryError | StackOverflowError exhausted) { result.put("status","exhausted"); }
        catch (Exception unsupported) { result.put("qualified",false);result.put("error",unsupported.getClass().getSimpleName()); }
        return result;
    }
}
