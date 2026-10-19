package org.bijux.ontology.queries;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.util.*;
import org.bijux.ontology.axioms.AxiomConstruction;
import org.bijux.ontology.backend.QualifiedReasoning;
import org.bijux.ontology.backend.WorkerProtocol;
import org.semanticweb.owlapi.model.*;
import org.semanticweb.owlapi.model.parameters.Imports;
import org.semanticweb.owlapi.reasoner.*;

/** Actual typed expression retrieval over the caller's existing classified theory. */
public final class ClassQueries {
    private ClassQueries() {}
    private static final class RetentionLimit extends RuntimeException {}
    private static final class Inventory {
        final int maximum, nodes, cap;
        int used, groups;
        Inventory(int maximum, int nodes, int cap) {
            this.maximum=maximum; this.nodes=nodes; this.cap=cap;
        }
        void member() { if (used >= maximum) throw new RetentionLimit(); used++; }
        void group() { if (groups >= nodes) throw new RetentionLimit(); groups++; }
    }

    /** Store one native node before enumerating it so stopped membership stays partial. */
    private static Map<String,Object> begin(Node<OWLClass> node, Inventory inventory) {
        inventory.group();
        Map<String,Object> result=new LinkedHashMap<>();
        result.put("members",new ArrayList<Map<String,Object>>());
        result.put("top",node.isTopNode()); result.put("bottom",node.isBottomNode());
        result.put("complete",false);
        return result;
    }

    @SuppressWarnings("unchecked")
    private static void members(Node<OWLClass> node, Map<String,Object> result,
            Inventory inventory) throws IOException {
        List<Map<String,Object>> values=(List<Map<String,Object>>)result.get("members");
        for (OWLClass member:node.getEntities()) {
            inventory.member();
            String iri=member.getIRI().toString();
            if (iri.length() > inventory.cap) throw new WorkerProtocol.OutputLimit();
            Map<String,Object> value=new LinkedHashMap<>();
            value.put("kind","entity"); value.put("role","class"); value.put("iri",iri);
            values.add(value);
        }
        result.put("complete",true);
    }

    /** No temporary ontology mutation, named-entity guessing or second reasoner is used. */
    public static Map<String,Object> evaluatePrepared(JsonNode request,
            QualifiedReasoning input, Map<String,Object> result) throws Exception {
        Map<String,Object> answer=new LinkedHashMap<>();
        List<Map<String,Object>> groups=new ArrayList<>();
        answer.put("groups",groups); answer.put("equivalents",null);
        answer.put("satisfiable",null); answer.put("complete",false); answer.put("error",null);
        result.put("class_query",answer);
        try {
            boolean consistent=input.reasoner.isConsistent(); result.put("consistent",consistent);
            if (!consistent) return result;
            int cap=WorkerProtocol.positive(request.path("max_bytes"));
            int nodes=WorkerProtocol.positive(request.path("max_nodes"));
            Inventory inventory=new Inventory(WorkerProtocol.positive(request.path("max_results")),nodes,cap);
            if (!request.path("direct").isBoolean()) throw new IOException("Missing explicit hierarchy depth");
            JsonNode axioms=request.path("axioms");
            if (!axioms.isArray() || axioms.size()!=1) throw new IOException("One typed class expression is required");
            AxiomConstruction factory=new AxiomConstruction(input.ontology.getOWLOntologyManager().getOWLDataFactory(),
                nodes,WorkerProtocol.positive(request.path("query_depth")),cap);
            OWLAxiom axiom=factory.axiom(axioms.get(0));
            if (!(axiom instanceof OWLSubClassOfAxiom)) throw new IOException("Class expression construction has another role");
            OWLClassExpression expression=((OWLSubClassOfAxiom)axiom).getSubClass();
            if (input.reasoner.getFreshEntityPolicy()!=FreshEntityPolicy.DISALLOW)
                throw new IOException("Fresh-entity policy differs from the qualified profile");
            for (OWLEntity entity:expression.getSignature())
                if (!entity.isBuiltIn() && !input.ontology.containsEntityInSignature(entity,Imports.INCLUDED))
                    throw new IOException("Class query entity lacks its selected signature role");
            answer.put("satisfiable",input.reasoner.isSatisfiable(expression));
            Node<OWLClass> equivalent=input.reasoner.getEquivalentClasses(expression);
            Map<String,Object> equivalents=begin(equivalent,inventory);
            answer.put("equivalents",equivalents);
            members(equivalent,equivalents,inventory);
            for (Node<OWLClass> node:input.reasoner.getSubClasses(expression,request.path("direct").booleanValue()).getNodes()) {
                Map<String,Object> group=begin(node,inventory);groups.add(group);
                members(node,group,inventory);
            }
            answer.put("complete",true);
        } catch (RetentionLimit exhausted) {
            answer.put("error","ResultLimit"); result.put("status","exhausted");
        } catch (TimeOutException | WorkerProtocol.OutputLimit exhausted) {
            answer.put("error",exhausted.getClass().getSimpleName()); result.put("status","exhausted");
        } catch (OutOfMemoryError | StackOverflowError exhausted) {
            answer.put("error",exhausted.getClass().getSimpleName()); result.put("status","exhausted");
        } catch (Exception unsupported) {
            answer.put("error",unsupported.getClass().getSimpleName());
        }
        return result;
    }
}
